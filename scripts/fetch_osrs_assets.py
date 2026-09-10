#!/usr/bin/env python3
"""scripts/fetch_osrs_assets.py - Maintainer-only OSRS Wiki art downloader.

NOTHING in the add-on calls this script. Installation, build, startup and
menus use the bundled files only. Run it explicitly when the verified asset
set changes:

    python3 scripts/fetch_osrs_assets.py --download     # fetch + record
    python3 scripts/fetch_osrs_assets.py --verify       # offline hash check

Downloads are resolved through the Wiki's MediaWiki imageinfo API (encoded
file titles, redirects, URL + revision), one request at a time with at least
one second between requests, bounded retries and a 2 MiB cap per file. Every
payload is validated (real PNG, larger than 1x1, not fully transparent,
safe path) before atomically replacing the previous good file, so an
interrupted run never leaves a corrupt asset behind. Per-file provenance is
written to assets/manifest.json and fish/ATTRIBUTION.md.

Rights: the downloaded files are Jagex game media used by the OSRS Wiki with
permission; the recorded notice is preserved verbatim in the manifest and in
the offline Credits/Assets page. See assets/manifest.json -> rights_notices.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, "assets", "manifest.json")
ATTRIBUTION_PATH = os.path.join(ROOT, "fish", "ATTRIBUTION.md")
CACHE_PATH = os.path.join(ROOT, ".dev", "asset-fetch-cache.json")

API_URL = "https://oldschool.runescape.wiki/api.php"
USER_AGENT = ("AnkiScape-asset-fetch/1.0 "
              "(offline add-on asset bundling; contact: wilsonhyeh@gmail.com)")
REQUEST_TIMEOUT_S = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
MAX_ATTEMPTS = 3

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_last_request_at = 0.0


class FetchError(RuntimeError):
    pass


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _save_manifest(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)


def _throttle() -> None:
    global _last_request_at
    wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _http_get(url: str) -> bytes:
    """One throttled GET with bounded retries and Retry-After support."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                return resp.read(MAX_FILE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                time.sleep(max(retry_after, 1.5 * attempt))
                continue
            raise FetchError(f"HTTP {exc.code} for {url}") from exc
        except Exception as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(1.5 * attempt)
                continue
            raise FetchError(f"network error for {url}: {exc!r}") from exc
    raise FetchError(f"download failed for {url}: {last_error!r}")


def _api_query(titles: list[str]) -> dict:
    params = {
        "action": "query",
        "titles": "|".join(titles),
        "prop": "imageinfo|revisions",
        "iiprop": "url|size|sha1|mime",
        "rvprop": "ids|timestamp",
        "redirects": "1",
        "format": "json",
        "formatversion": "1",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    raw = _http_get(url)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise FetchError(f"API returned non-JSON: {exc!r}") from exc
    if not isinstance(data, dict) or "query" not in data:
        raise FetchError("API response missing query object")
    return data


def _resolve_titles(records: list[dict], cache: dict) -> dict:
    """Resolve manifest wiki_titles to url/size/revision. Cached per title."""
    unresolved = []
    for record in records:
        title = str(record.get("wiki_title") or "")
        cached = cache.get(title) or {}
        if cached.get("url") and not record.get("_force"):
            record["_resolved"] = cached
        else:
            unresolved.append(record)
    for start in range(0, len(unresolved), 40):
        batch = unresolved[start:start + 40]
        titles = [str(r.get("wiki_title")) for r in batch]
        data = _api_query(titles)
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):  # formatversion=1 returns a pageid map
            pages = list(pages.values())
        normalized = {n.get("to"): n.get("from")
                      for n in data.get("query", {}).get("normalized", [])}
        redirects = {r.get("to"): r.get("from")
                     for r in data.get("query", {}).get("redirects", [])}
        by_title = {}
        for page in pages:
            by_title[page.get("title")] = page
        for record in batch:
            title = str(record.get("wiki_title"))
            page = by_title.get(title)
            if page is None:
                # Follow normalize + redirect bookkeeping from the API.
                target = normalized.get(title) or redirects.get(title)
                page = by_title.get(target) if target else None
            if page is None or page.get("missing") is not None:
                raise FetchError(f"Wiki file missing: {title}")
            info = (page.get("imageinfo") or [{}])[0]
            revision = ""
            revisions = page.get("revisions") or []
            if revisions:
                revision = str(revisions[0].get("revid") or "")
            resolved = {
                "title": page.get("title"),
                "pageid": page.get("pageid"),
                "url": info.get("url"),
                "width": info.get("width"),
                "height": info.get("height"),
                "mime": info.get("mime"),
                "sha1": info.get("sha1"),
                "revision": revision,
            }
            if not resolved["url"] or not resolved["width"]:
                raise FetchError(f"Wiki metadata incomplete for {title}")
            cache[title] = resolved
            record["_resolved"] = resolved
    return cache


def _validate_png_bytes(raw: bytes, label: str) -> dict:
    """Reject HTML/error pages, non-PNG payloads and size-cap violations."""
    if len(raw) > MAX_FILE_BYTES:
        raise FetchError(f"{label}: exceeds {MAX_FILE_BYTES} byte cap")
    if not raw.startswith(_PNG_MAGIC):
        head = raw[:64].decode("utf-8", "replace").replace("\n", " ")
        raise FetchError(f"{label}: not a PNG (starts {head!r})")
    width, height = struct.unpack(">II", raw[16:24])
    if width <= 1 or height <= 1:
        raise FetchError(f"{label}: placeholder-sized {width}x{height}")
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(raw)) as image:
            image.load()
            rgba = image.convert("RGBA")
            bbox = rgba.getchannel("A").getbbox()
    except Exception as exc:
        raise FetchError(f"{label}: unreadable PNG ({exc!r})") from exc
    if bbox is None:
        raise FetchError(f"{label}: fully transparent image")
    return {"width": width, "height": height}


def _safe_target(rel: str) -> str:
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise FetchError(f"unsafe output path: {rel!r}")
    full = os.path.join(ROOT, rel)
    if not os.path.abspath(full).startswith(os.path.abspath(ROOT) + os.sep):
        raise FetchError(f"output escapes repo: {rel!r}")
    if not rel.lower().endswith(".png"):
        raise FetchError(f"managed downloads must be .png: {rel!r}")
    return full


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def download_all(records: list[dict], cache: dict, *, force: bool = False,
                 only: list[str] | None = None) -> tuple[int, int, list[str]]:
    targets = []
    for record in records:
        if record.get("action") != "download":
            continue
        if only and str(record.get("id")) not in only:
            continue
        if force:
            record["_force"] = True
        targets.append(record)
    cache = _resolve_titles(targets, cache)
    _save_cache(cache)
    fetched = skipped = 0
    failures: list[str] = []
    today = datetime.date.today().isoformat()
    for record in targets:
        label = f"{record.get('id')} ({record.get('path')})"
        try:
            target = _safe_target(str(record.get("path")))
            resolved = record["_resolved"]
            if (not force and record.get("status") == "verified"
                    and os.path.isfile(target)
                    and _sha256(open(target, "rb").read()) == record.get("sha256")):
                skipped += 1
                continue
            raw = _http_get(str(resolved["url"]))
            dims = _validate_png_bytes(raw, label)
            if dims["width"] != resolved.get("width") or \
                    dims["height"] != resolved.get("height"):
                raise FetchError(
                    f"{label}: size changed (Wiki {resolved.get('width')}x"
                    f"{resolved.get('height')} vs file {dims['width']}x"
                    f"{dims['height']})")
            tmp = target + ".tmp"
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, target)
            record.update({
                "status": "verified",
                "source_url": resolved.get("url") or "",
                "resolved_title": resolved.get("title") or "",
                "revision": resolved.get("revision") or "",
                "wiki_sha1": resolved.get("sha1") or "",
                "retrieved": today,
                "width": dims["width"], "height": dims["height"],
                "bytes": len(raw), "sha256": _sha256(raw),
                "format": "png",
            })
            fetched += 1
            print(f"fetch: {label} {dims['width']}x{dims['height']} "
                  f"{len(raw)} bytes sha256={record['sha256'][:12]}...")
        except FetchError as exc:
            failures.append(str(exc))
            print(f"fetch: FAILED {exc}", file=sys.stderr)
    return fetched, skipped, failures


def write_attribution(manifest: dict) -> None:
    lines = [
        "# Fish / cooked icons - attribution",
        "",
        "These images are Jagex game media, downloaded once by the maintainer",
        "via `scripts/fetch_osrs_assets.py --download`. The add-on bundles them",
        "and never downloads at runtime. Runtime file writes for missing art do",
        "not happen: unknown items show the shipped fallback",
        "`icon/fallback_missing.png`.",
        "",
        "Rights notice (verbatim from the OSRS Wiki file pages):",
        "",
        "> This is licensed media of a copyrighted video game. It is taken from",
        "> oldschool.runescape.com, and its copyright is held by Jagex Ltd. It",
        "> is used with permission.",
        "",
        "The Wiki's permission does not automatically license redistribution",
        "inside a third-party add-on; publication rights remain an unresolved",
        "release issue (see assets/manifest.json -> rights_notices).",
        "",
        "| File | Wiki source page | Revision | Retrieved | Size | SHA256 |",
        "|---|---|---|---|---|---|",
    ]
    records = [r for r in manifest.get("files", [])
               if r.get("kind") in ("fish_raw", "fish_cooked")]
    for record in sorted(records, key=lambda r: str(r.get("path"))):
        lines.append(
            f"| `{record.get('path')}` | {record.get('source_page')} | "
            f"{record.get('revision') or ''} | {record.get('retrieved') or ''} | "
            f"{record.get('width')}x{record.get('height')} "
            f"({record.get('bytes')} B) | `{record.get('sha256') or ''}` |")
    lines.append("")
    with open(ATTRIBUTION_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def verify_offline() -> int:
    """Re-check every recorded file against the manifest. Never touches the
    network: this is the same check a release gate can trust offline."""
    manifest = _load_manifest()
    problems = []
    for record in manifest.get("files", []):
        path = str(record.get("path") or "")
        full = os.path.join(ROOT, path)
        kind = record.get("kind")
        if not os.path.isfile(full):
            problems.append(f"missing file: {path}")
            continue
        raw = open(full, "rb").read()
        digest = _sha256(raw)
        if record.get("sha256") and digest != record["sha256"]:
            problems.append(f"sha256 mismatch: {path}")
        if record.get("bytes") and len(raw) != record["bytes"]:
            problems.append(f"byte size mismatch: {path}")
        if kind in ("font", "sound"):
            continue
        try:
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(raw)) as image:
                image.load()
                width, height = image.size
                fully_transparent = (
                    image.convert("RGBA").getchannel("A").getbbox() is None)
        except Exception as exc:
            problems.append(f"undecodable image: {path} ({exc!r})")
            continue
        if width <= 1 or height <= 1:
            problems.append(f"placeholder-sized image: {path} ({width}x{height})")
        if fully_transparent:
            problems.append(f"fully transparent image: {path}")
        if record.get("width") and width != record["width"]:
            problems.append(f"width mismatch: {path}")
        if record.get("height") and height != record["height"]:
            problems.append(f"height mismatch: {path}")
        if record.get("rights") == "jagex-wiki":
            if record.get("status") != "verified":
                problems.append(f"downloaded art not verified: {path}")
            if not record.get("source_page") or not record.get("retrieved"):
                problems.append(f"missing provenance: {path}")
    if problems:
        print("fetch --verify: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    count = len(manifest.get("files", []))
    print(f"fetch --verify: OK ({count} recorded files, offline)")
    return 0


def main(argv=None) -> int:
    global _manifest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true",
                        help="download manifest-listed Wiki art")
    parser.add_argument("--verify", action="store_true",
                        help="offline hash/format verification")
    parser.add_argument("--force", action="store_true",
                        help="re-download even when hashes match")
    parser.add_argument("--only", action="append", default=[],
                        help="limit --download to these manifest ids")
    args = parser.parse_args(argv)
    if args.verify and args.download:
        parser.error("choose either --download or --verify")
    if args.verify:
        return verify_offline()
    if not args.download:
        parser.print_help()
        return 2
    _manifest = _load_manifest()
    records = [r for r in _manifest.get("files", []) if isinstance(r, dict)]
    cache = _load_cache()
    try:
        fetched, skipped, failures = download_all(
            records, cache, force=args.force, only=args.only)
    finally:
        for record in records:
            record.pop("_resolved", None)
            record.pop("_force", None)
    _save_manifest(_manifest)
    write_attribution(_manifest)
    print(f"download: {fetched} fetched, {skipped} already verified, "
          f"{len(failures)} failed")
    if failures:
        for failure in failures:
            print(f"  FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
