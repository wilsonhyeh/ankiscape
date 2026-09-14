# evolved/auth.py - Username normalization + managed-account client helpers.
"""Supabase normally authenticates with email/password. Username login is
preserved via a narrowly scoped Edge Function: normalize username, resolve its
private Auth identity server-side, call managed password auth, generic failure
on missing user/bad password. Never expose username-to-email lookup.

Live tokens stay in MemorySession. ProfileSession can mirror them to the OS
credential vault when remembered sign-in is enabled. Passwords and tokens never
enter collection config, journals or logs.
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
    """Live tokens; an optional change callback owns secure persistence."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    on_change: object = None
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
        if callable(self.on_change):
            self.on_change(self)

    def clear(self) -> None:
        with self._lock:
            self.access_token = None
            self.refresh_token = None
            self.user_id = None
            self.username = None
        if callable(self.on_change):
            self.on_change(self)

    def set_guarded(self, *, expect_refresh_token, expect_user_id,
                    access_token: str, refresh_token: str, user_id: str,
                    username: Optional[str] = None) -> bool:
        """Install rotated tokens only when the session still belongs to the
        snapshot the caller refreshed for. Comparison and mutation happen
        under the session lock; a logout or newer login wins."""
        with self._lock:
            if self.refresh_token != expect_refresh_token:
                return False
            if expect_user_id is not None and self.user_id != expect_user_id:
                return False
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.user_id = user_id
            self.username = username
        if callable(self.on_change):
            self.on_change(self)
        return True

    def clear_guarded(self, *, expect_refresh_token,
                      expect_user_id) -> bool:
        """Clear only when the session still matches the refreshed snapshot."""
        with self._lock:
            if self.refresh_token != expect_refresh_token:
                return False
            if expect_user_id is not None and self.user_id != expect_user_id:
                return False
            self.access_token = None
            self.refresh_token = None
            self.user_id = None
            self.username = None
        if callable(self.on_change):
            self.on_change(self)
        return True

    @property
    def logged_in(self) -> bool:
        with self._lock:
            return bool(self.access_token and self.user_id)
