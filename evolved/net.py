# evolved/net.py - stdlib HTTP transport (contract G, Qt-free).
"""urllib on background jobs with finite timeouts. Receives immutable payloads,
returns parsed validated data; UI/collection application happens on the correct
main/operation thread. Production URLs require HTTPS; only explicitly
configured loopback dev endpoints allow HTTP. Refuses cross-host redirects
with credentials. Bounded response reads + strict JSON shape checks.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

REQUEST_TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 512 * 1024


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    project_key: str  # public anon key only; never service-role/SMTP/admin
    allow_http_loopback: bool = False


class NetError(Exception):
    def __init__(self, kind: str, detail: str = "", status: int = 0,
                 retry_after: Optional[int] = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after


def _check_url(endpoint: Endpoint, path: str) -> str:
    url = urllib.parse.urljoin(endpoint.base_url.rstrip("/") + "/", path.lstrip("/"))
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and endpoint.allow_http_loopback and parts.hostname in (
            "127.0.0.1", "localhost", "::1"):
        return url
    raise NetError("insecure_url", f"refused {parts.scheme}://{parts.hostname}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NetError("redirect_refused", f"{code} to {newurl}")


def post_json(endpoint: Endpoint, path: str, payload: Dict[str, Any],
              *, access_token: Optional[str] = None,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = _check_url(endpoint, path)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "apikey": endpoint.project_key})
    if access_token:
        req.add_header("Authorization", f"Bearer {access_token}")
    for name, value in (headers or {}).items():
        req.add_header(str(name), str(value))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise _map_http_error(exc)
    except NetError:
        raise
    except Exception as exc:
        raise NetError("connection", repr(exc))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NetError("response_too_large", f"{len(raw)} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise NetError("malformed_response", "invalid JSON")
    if not isinstance(data, dict):
        raise NetError("malformed_response", "top-level JSON must be object")
    return data


def _map_http_error(exc: urllib.error.HTTPError) -> NetError:
    try:
        raw = exc.read(MAX_RESPONSE_BYTES)
    except Exception:
        raw = b""
    detail = raw[:500].decode("utf-8", "replace")
    retry_after = None
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        retry_after = int(ra) if ra is not None else None
    except (ValueError, TypeError):
        retry_after = None
    kinds = {401: "unauthorized", 403: "forbidden", 404: "not_found",
             409: "conflict", 422: "invalid", 429: "rate_limited"}
    if exc.code in kinds:
        return NetError(kinds[exc.code], detail, status=exc.code, retry_after=retry_after)
    if 500 <= exc.code < 600:
        return NetError("server", detail, status=exc.code, retry_after=retry_after)
    return NetError("http", detail, status=exc.code)
