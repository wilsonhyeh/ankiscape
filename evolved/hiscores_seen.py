# evolved/hiscores_seen.py - Local memory of the last board a player looked at.
"""Rank movement ("up 2 since your last visit") needs to remember what the
board looked like last time. That memory is a small JSON file in the Anki
profile folder: local to this computer, never uploaded, holds only public
board data (usernames and ranks), and is safe to delete at any time.

Every failure degrades to "no memory" (no arrows), never to an error.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

FILE_NAME = "ankiscape_hiscores_seen.json"
MAX_BOARDS = 16
MAX_PLAYERS = 200
VERSION = 1


def path_for(profile_folder: str) -> str:
    return os.path.join(str(profile_folder), FILE_NAME)


def _clean(data: Any) -> Dict[str, Dict[str, Any]]:
    """Keep only well-formed entries; bounded so a corrupt or hostile file
    can never grow the process."""
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(data, dict):
        return out
    for board, entry in list(data.items())[:MAX_BOARDS]:
        if not isinstance(board, str) or not isinstance(entry, dict):
            continue
        ranks = entry.get("ranks")
        try:
            ts = float(entry.get("ts"))
        except (TypeError, ValueError):
            continue
        if not isinstance(ranks, dict):
            continue
        good = {str(k): int(v) for k, v in list(ranks.items())[:MAX_PLAYERS]
                if isinstance(k, str) and isinstance(v, int)
                and not isinstance(v, bool) and v >= 1}
        out[board] = {"ts": ts, "ranks": good}
    return out


def load(profile_folder: str) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path_for(profile_folder), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return {}
    return _clean(raw.get("boards"))


def save(profile_folder: str, boards: Dict[str, Dict[str, Any]]) -> bool:
    """Atomic replace. Returns False (and leaves any old file intact) on any
    failure; callers ignore the result."""
    target = path_for(profile_folder)
    tmp = ""
    try:
        payload = {"version": VERSION, "boards": _clean(boards)}
        fd, tmp = tempfile.mkstemp(prefix=".hs-", suffix=".tmp",
                                   dir=str(profile_folder))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, target)
        return True
    except (OSError, ValueError, TypeError):
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
