#!/usr/bin/env python3
"""scripts/audit_assets.py - Asset coverage, validity and package audit.

    python3 scripts/audit_assets.py --check
    python3 scripts/audit_assets.py --check --archive dist/ankiscape-3.0.0.ankiaddon

--check derives the expected asset set independently of the manifest (from
shared/rules-v1.json, evolved/assets.py, constants.py and the managed art
directories), then verifies every asset exists, decodes, is not a 1x1 or
fully transparent placeholder, matches its recorded hash, and that known
game art is never the fallback. --archive additionally checks the built ZIP
member set, exact case, per-file SHA256, decodability and package hygiene.

This script is also imported by scripts/build_addon.py so a build fails when
supported art is missing, corrupt, fallback or stale.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, "assets", "manifest.json")
FALLBACK_PATH = "icon/fallback_missing.png"
MANAGED_DIRS = ("ores", "trees", "bars", "gems", "crafteditems", "fish",
                "icon")
REQUIRED_IN_ARCHIVE = ("assets/manifest.json", "fish/ATTRIBUTION.md",
                       FALLBACK_PATH, "evolved/ui/guide.py")
FORBIDDEN_PREFIXES = ("tests/", "server/", "dev/", "dist/", "artifacts/",
                      ".dev/", ".git/", "raw/", "plans/")
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".log", ".sqlite3", ".env", ".tmp")
FORBIDDEN_NAMES = ("meta.json", "user_files", ".DS_Store")
IMAGE_KINDS = ("skill", "nav", "ore", "log", "bar", "gem", "craft",
               "fish_raw", "fish_cooked", "ui", "texture", "fallback")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return _sha256_bytes(fh.read())


def _load_manifest() -> dict:
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _require_pil():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - maintainer machine check
        raise SystemExit("audit: Pillow is required for asset validation")
    return __import__("PIL.Image", fromlist=["Image"])


def _evolved_rel(display: str):
    """Folder-qualified path for an item display from evolved/assets.py."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from evolved import assets as evolved_assets

    rel = evolved_assets.ITEM_FILES.get(display)
    if not rel:
        return None
    return f"{evolved_assets._folder_for(rel)}/{rel}"


def _expected_from_sources() -> dict:
    """Derive every supported display -> relative path from product sources.

    Returns {semantic_key: {"display", "path", "kind", "consumer"}}.
    Deliberately uses rules + package mappings, NOT assets/manifest.json, so
    a forgotten manifest entry cannot hide a missing asset.
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import constants
    from evolved import assets as evolved_assets

    rules = json.load(open(os.path.join(ROOT, "shared", "rules-v1.json"),
                           encoding="utf-8"))
    expected = {}

    for table, kind in (("ores", "ore"), ("trees", "log"), ("bars", "bar"),
                        ("gems", "gem"), ("crafting", "craft")):
        for entry in rules.get(table, []):
            display = str(entry.get("display"))
            expected[f"{kind}:{display}"] = {
                "display": display, "kind": kind,
                "path": _evolved_rel(display),
                "consumer": f"rules.{table} + evolved.assets.ITEM_FILES"}
    for entry in rules.get("fish", []):
        display = str(entry.get("display"))
        cooked = f"Cooked {display}"
        expected[f"fish_raw:{display}"] = {
            "display": display, "kind": "fish_raw",
            "path": _evolved_rel(display),
            "consumer": "rules.fish + evolved.assets.FISH_FILES"}
        expected[f"fish_cooked:{cooked}"] = {
            "display": cooked, "kind": "fish_cooked",
            "path": _evolved_rel(cooked),
            "consumer": "rules.fish + evolved.assets.COOKED_FISH_FILES"}

    for skill, rel in evolved_assets.SKILL_ICONS.items():
        expected[f"skill:{skill}"] = {
            "display": skill, "kind": "skill", "path": rel,
            "consumer": "evolved.assets.SKILL_ICONS"}
    for section, rel in evolved_assets.NAV_ICONS.items():
        expected[f"nav:{section}"] = {
            "display": section, "kind": "nav", "path": rel,
            "consumer": "evolved.assets.NAV_ICONS"}
    for slot, rel in evolved_assets.SLOT_ICONS.items():
        expected[f"slot:{slot}"] = {
            "display": slot, "kind": "ui", "path": rel,
            "consumer": "evolved.assets.SLOT_ICONS"}
    if "guide" not in evolved_assets.NAV_ICONS:
        expected["nav:guide"] = {
            "display": "guide", "kind": "nav", "path": None,
            "consumer": "evolved.assets.NAV_ICONS (missing Guide mapping)"}

    # Classic consumers must point at the same files as the Evolved mappings.
    classic = {
        "ore": constants.ORE_IMAGES, "log": constants.TREE_IMAGES,
        "bar": constants.BAR_IMAGES, "gem": constants.GEM_IMAGES,
        "craft": constants.CRAFTED_ITEM_IMAGES}
    for kind, mapping in classic.items():
        for display, abs_path in mapping.items():
            rel = os.path.relpath(abs_path, ROOT).replace(os.sep, "/")
            key = f"{kind}:{display}"
            if key in expected and expected[key]["path"] != rel:
                expected[key]["consumer"] += \
                    f" + Classic mismatch ({rel})"
            elif key not in expected:
                expected[key] = {"display": display, "kind": kind,
                                 "path": rel,
                                 "consumer": "constants (Classic only)"}
    return expected


def audit_repository() -> list:
    """Independent coverage + validity checks against the working tree."""
    Image = _require_pil()
    problems = []
    try:
        manifest = _load_manifest()
    except (OSError, ValueError) as exc:
        return [f"asset manifest unreadable: {exc!r}"]
    records = [r for r in manifest.get("files", []) if isinstance(r, dict)]
    by_path = {}
    for record in records:
        path = str(record.get("path") or "")
        if not path:
            problems.append(f"manifest record without path: {record.get('id')!r}")
            continue
        if path in by_path:
            problems.append(f"duplicate manifest path: {path}")
        by_path[path] = record
        if record.get("action") not in ("download", "keep"):
            problems.append(f"{path}: bad action {record.get('action')!r}")
        if record.get("rights") not in manifest.get("rights_notices", {}):
            problems.append(f"{path}: unknown rights "
                            f"{record.get('rights')!r}")

    fallback = by_path.get(FALLBACK_PATH)
    if fallback is None:
        problems.append(f"fallback not recorded: {FALLBACK_PATH}")
        fallback_sha = ""
    else:
        full = os.path.join(ROOT, FALLBACK_PATH)
        if not os.path.isfile(full):
            problems.append(f"fallback missing on disk: {FALLBACK_PATH}")
            fallback_sha = ""
        else:
            fallback_sha = _sha256_file(full)
            if fallback.get("status") != "fallback":
                problems.append("fallback record must have status=fallback")

    for record in records:
        path = str(record.get("path") or "")
        kind = record.get("kind")
        full = os.path.join(ROOT, path)
        if path.startswith("/") or ".." in path.split("/"):
            problems.append(f"unsafe manifest path: {path}")
            continue
        if not os.path.isfile(full):
            problems.append(f"missing asset: {path} "
                            f"(status={record.get('status')})")
            continue
        raw = open(full, "rb").read()
        digest = _sha256_bytes(raw)
        if record.get("sha256") and digest != record["sha256"]:
            problems.append(f"sha256 mismatch: {path}")
        if record.get("bytes") and len(raw) != record["bytes"]:
            problems.append(f"byte size mismatch: {path}")
        if kind in ("font", "sound"):
            continue
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                image.convert("RGBA").getchannel("A").getbbox()
                width, height = image.size
                if width <= 1 or height <= 1:
                    problems.append(
                        f"placeholder-sized image: {path} ({width}x{height})")
                if image.convert("RGBA").getchannel("A").getbbox() is None:
                    problems.append(f"fully transparent image: {path}")
        except Exception as exc:
            problems.append(f"undecodable image: {path} ({exc!r})")
            continue
        if record.get("width") and width != record["width"]:
            problems.append(f"width mismatch: {path}")
        if record.get("height") and height != record["height"]:
            problems.append(f"height mismatch: {path}")
        if record.get("rights") == "jagex-wiki":
            if record.get("status") != "verified":
                problems.append(f"downloaded art not verified: {path}")
            if not record.get("source_page") or not record.get("retrieved"):
                problems.append(f"missing provenance: {path}")
            if not record.get("source_url"):
                problems.append(f"missing resolved source URL: {path}")
            if fallback_sha and digest == fallback_sha:
                problems.append(f"known art is the fallback: {path}")
        if record.get("status") == "original" and fallback_sha \
                and digest == fallback_sha:
            problems.append(f"known original asset equals fallback: {path}")

    # Directory coverage: every managed file must be a manifest record and
    # every manifest record inside a managed dir must exist in it.
    os_walked = set()
    for dirname in MANAGED_DIRS:
        base = os.path.join(ROOT, dirname)
        if not os.path.isdir(base):
            problems.append(f"managed directory missing: {dirname}")
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name == ".DS_Store" or name.lower().endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(root, name),
                                      ROOT).replace(os.sep, "/")
                os_walked.add(rel)
                if rel not in by_path:
                    problems.append(f"unmanaged file in {dirname}: {rel}")
    for path in by_path:
        top = path.split("/", 1)[0]
        if top in MANAGED_DIRS and path not in os_walked:
            problems.append(f"manifest asset not on disk: {path}")

    # Independent coverage from rules/UI: keys must map to verified records,
    # never to a missing path or the fallback.
    expected = _expected_from_sources()
    missing_keys = []
    for key, item in expected.items():
        if not item.get("path"):
            missing_keys.append(f"{key} (no path mapping: {item['consumer']})")
            continue
        record = by_path.get(item["path"])
        if record is None:
            missing_keys.append(f"{key} -> {item['path']} not in manifest")
            continue
        if record.get("status") == "fallback":
            missing_keys.append(f"{key} resolves to the fallback")
        if record.get("status") == "pending":
            missing_keys.append(f"{key} -> {item['path']} not downloaded")
    problems.extend(missing_keys)

    # Runtime resolution must return the same verified paths.
    from evolved import assets as evolved_assets
    from evolved.icons import resolve_icon
    for skill, rel in evolved_assets.SKILL_ICONS.items():
        resolved = evolved_assets.skill_icon_path(skill)
        if os.path.relpath(resolved, ROOT).replace(os.sep, "/") != rel:
            problems.append(f"skill_icon_path({skill}) resolved {resolved}")
    for section, rel in evolved_assets.NAV_ICONS.items():
        resolved = evolved_assets.nav_icon_path(section)
        if os.path.relpath(resolved, ROOT).replace(os.sep, "/") != rel:
            problems.append(f"nav_icon_path({section}) resolved {resolved}")
    for slot, rel in evolved_assets.SLOT_ICONS.items():
        resolved = evolved_assets.slot_icon_path(slot)
        if os.path.relpath(resolved, ROOT).replace(os.sep, "/") != rel:
            problems.append(f"slot_icon_path({slot}) resolved {resolved}")
    for achievement_id in ("first_catch", "first_cook", "cooks_100",
                           "cooks_1000", "skill_10_mining"):
        resolved = evolved_assets.achievement_icon_path(achievement_id)
        if not resolved:
            problems.append(f"achievement_icon_path({achievement_id}) is empty")
    if evolved_assets.achievement_icon_path("unknown_achievement") is not None:
        problems.append("unknown achievements must not resolve an icon")
    for entry in rules_displays():
        rel = _evolved_rel(entry)
        if not rel:
            continue
        resolved = evolved_assets.display_icon(entry)
        if os.path.relpath(resolved, ROOT).replace(os.sep, "/") != rel:
            problems.append(f"display_icon({entry!r}) resolved {resolved}")
    bogus = resolve_icon(kind="item", display="__audit_missing__", mapping={})
    if os.path.relpath(bogus, ROOT).replace(os.sep, "/") != FALLBACK_PATH:
        problems.append("unknown item did not resolve to the shipped fallback")
    return problems


def rules_displays() -> list:
    rules = json.load(open(os.path.join(ROOT, "shared", "rules-v1.json"),
                           encoding="utf-8"))
    displays = []
    for table in ("ores", "trees", "bars", "gems", "crafting"):
        displays += [str(e.get("display")) for e in rules.get(table, [])]
    for entry in rules.get("fish", []):
        displays.append(str(entry.get("display")))
        displays.append(f"Cooked {entry.get('display')}")
    return displays


def audit_archive(path: str, manifest: dict | None = None) -> list:
    """Validate a built .ankiaddon against the manifest and package rules."""
    Image = _require_pil()
    problems = []
    manifest = manifest or _load_manifest()
    records = [r for r in manifest.get("files", []) if isinstance(r, dict)]
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"archive unreadable: {path} ({exc!r})"]
    with archive:
        names = archive.namelist()
        name_set = set(names)
        if len(names) != len(name_set):
            problems.append("archive contains duplicate member names")
        for required in REQUIRED_IN_ARCHIVE:
            if required not in name_set:
                problems.append(f"archive missing {required}")
        for name in names:
            lower = name.lower()
            if name.startswith(FORBIDDEN_PREFIXES) or \
                    lower.endswith(FORBIDDEN_SUFFIXES) or \
                    name in FORBIDDEN_NAMES or "/__pycache__/" in name:
                problems.append(f"forbidden packaged member: {name}")
            if name.startswith("/") or ".." in name.split("/"):
                problems.append(f"unsafe packaged member: {name}")
        for record in records:
            rel = str(record.get("path") or "")
            if not rel:
                continue
            if rel not in name_set:
                problems.append(f"archive missing manifest asset: {rel}")
                continue
            raw = archive.read(rel)
            digest = _sha256_bytes(raw)
            if record.get("sha256") and digest != record["sha256"]:
                problems.append(f"archive sha256 mismatch: {rel}")
            if record.get("kind") in ("font", "sound"):
                continue
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    image.convert("RGBA").getchannel("A").getbbox()
                    width, height = image.size
                if width <= 1 or height <= 1:
                    problems.append(f"archive placeholder image: {rel}")
            except Exception as exc:
                problems.append(f"archive undecodable image: {rel} ({exc!r})")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="audit the working tree")
    parser.add_argument("--archive", default="",
                        help="also audit this .ankiaddon package")
    args = parser.parse_args(argv)
    if not args.check and not args.archive:
        parser.print_help()
        return 2
    problems = []
    if args.check:
        problems += audit_repository()
    if args.archive:
        problems += audit_archive(args.archive)
    if problems:
        print("audit: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    manifest = _load_manifest()
    records = manifest.get("files", [])
    verified = sum(1 for r in records if r.get("status") == "verified")
    originals = sum(1 for r in records if r.get("status") == "original")
    print(f"audit: OK ({len(records)} assets: {verified} verified downloads, "
          f"{originals} originals/fallback"
          + (f", archive {os.path.basename(args.archive)}" if args.archive else "")
          + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
