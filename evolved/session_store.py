# evolved/session_store.py - Prod/dev endpoint + session ownership (Qt-free).
"""Owns the two things the product layer needs but must not scatter:

  - Endpoint resolution: dev loopback (local Supabase stack on alternate
    ports) vs. production Supabase URL. The public anon key may ship; the
    service-role/SMTP/admin keys never do. Production values live in ONE
    place (`evolved/prod_config.py`, generated at build time from
    environment — never committed with secrets) so a misconfigured build
    fails closed (no endpoint) rather than pointing at a wrong project.
  - The MemorySession singleton per profile generation: login/logout bind
    and clear it; the sync service reads it; profile close clears it even
    offline. Passwords/tokens never enter collection config, journals, or
    logs (enforced by construction: only tokens/user_id live here).

Refresh serialization: concurrent 401s must produce ONE refresh attempt.
The session carries a lock; `refresh_once()` runs the supplied refresh
callable at most once across threads (others wait and read the result).
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .auth import MemorySession
from .net import Endpoint

# Local-stack defaults (alternate ports; never Fleuron's project-ref stack).
DEV_BASE_URL = "http://127.0.0.1:55321"
DEV_ALLOW_HTTP_LOOPBACK = True


@dataclass(frozen=True)
class ProdConfig:
    base_url: str = ""
    anon_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.anon_key)


def _load_prod_config() -> ProdConfig:
    """Build-time generated module wins; env override is a fallback for
    dev loops. Neither path ever carries privileged keys."""
    try:
        from . import prod_config as _pc  # type: ignore
        base = str(getattr(_pc, "SUPABASE_URL", "") or "")
        key = str(getattr(_pc, "SUPABASE_ANON_KEY", "") or "")
        if base or key:
            return ProdConfig(base_url=base, anon_key=key)
    except ImportError:
        pass
    return ProdConfig(
        base_url=os.environ.get("ANKISCAPE_SUPABASE_URL", ""),
        anon_key=os.environ.get("ANKISCAPE_SUPABASE_ANON_KEY", ""))


def resolve_endpoint(*, dev: bool, anon_key: str = "",
                     prod: Optional[ProdConfig] = None) -> Optional[Endpoint]:
    """Return the Endpoint to use, or None when unconfigured (fail closed).

    Dev mode needs the local anon key (read from `supabase status` at
    runtime by the caller, never stored here). Prod mode needs the
    build-time ProdConfig; an unconfigured build gets None and every
    network op reports offline/unconfigured instead of hitting a wrong
    project.
    """
    if dev:
        if not anon_key:
            return None
        return Endpoint(base_url=DEV_BASE_URL, project_key=anon_key,
                        allow_http_loopback=DEV_ALLOW_HTTP_LOOPBACK)
    cfg = prod if prod is not None else _load_prod_config()
    if not cfg.configured:
        return None
    return Endpoint(base_url=cfg.base_url, project_key=cfg.anon_key)


class ProfileSession:
    """MemorySession + refresh lock + generation binding for one profile."""

    def __init__(self, *, generation: int):
        self.generation = generation
        self.session = MemorySession()
        self._refresh_lock = threading.Lock()
        self._refresh_fn: Optional[Callable[[MemorySession], bool]] = None

    def bind_refresh(self, fn: Callable[[MemorySession], bool]) -> None:
        self._refresh_fn = fn

    @property
    def user_id(self) -> Optional[str]:
        return self.session.user_id

    @property
    def logged_in(self) -> bool:
        return self.session.logged_in

    def clear(self) -> None:
        self.session.clear()

    def refresh_once(self) -> bool:
        """Run the bound refresh at most once across threads.

        First thread runs the callable; concurrent threads WAIT for it and
        then report ITS result (not a fresh attempt). Waiters that arrive
        after completion acquire the free lock and run their own attempt —
        that is correct (a later 401 deserves a later refresh).
        """
        if self._refresh_fn is None:
            return False
        if self._refresh_lock.acquire(blocking=False):
            try:
                try:
                    self._last_refresh_ok = bool(self._refresh_fn(self.session))
                except Exception:
                    self._last_refresh_ok = False
                return self._last_refresh_ok
            finally:
                self._refresh_lock.release()
        # Contended: wait for the holder, then report its outcome.
        with self._refresh_lock:
            return bool(getattr(self, "_last_refresh_ok", False))

    # SyncService transport compatibility: the service calls
    # session.refresh() on 401; route it through the serialized path.
    def refresh(self) -> bool:
        return self.refresh_once()
