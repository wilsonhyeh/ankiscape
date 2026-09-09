# evolved/auth.py - Username normalization + managed-account client helpers.
"""Supabase normally authenticates with email/password. Username login is
preserved via a narrowly scoped Edge Function: normalize username, resolve its
private Auth identity server-side, call managed password auth, generic failure
on missing user/bad password. Never expose username-to-email lookup.

Access/refresh tokens stay memory-only; restart requires login again.
Passwords/tokens never enter collection config, journals or logs.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Optional

_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
_MAX_PASSWORD_LEN = 256


def normalize_username(raw: str) -> str:
    """Trim, casefold, validate. Raises ValueError with a generic message."""
    if not isinstance(raw, str):
        raise ValueError("invalid username or password")
    norm = raw.strip().casefold()
    if not _USERNAME_RE.match(norm):
        raise ValueError("invalid username or password")
    return norm


def validate_password(raw: str) -> None:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > _MAX_PASSWORD_LEN * 4:
        raise ValueError("invalid username or password")
    if len(raw) > _MAX_PASSWORD_LEN:
        raise ValueError("invalid username or password")


@dataclass
class MemorySession:
    """Tokens live here only; cleared on logout/profile close even offline."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    _lock: threading.Lock = None  # type: ignore

    def __post_init__(self):
        object.__setattr__(self, "_lock", threading.Lock())

    def set(self, *, access_token: str, refresh_token: str, user_id: str,
            username: Optional[str] = None) -> None:
        with self._lock:
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.user_id = user_id
            self.username = username

    def clear(self) -> None:
        with self._lock:
            self.access_token = None
            self.refresh_token = None
            self.user_id = None
            self.username = None

    @property
    def logged_in(self) -> bool:
        with self._lock:
            return bool(self.access_token and self.user_id)
