# evolved/backup.py - Export/Restore Game Backup (Qt-free).
"""Format/version/hash-validated game backups. These are separate from Anki's
collection backups. On missing/corrupt journal, linked accounts rebuild from
the server plus preserved outbox backup; an anonymous game requires its game
backup (never mint historical rewards from a cached XP total)."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict

from .journal import canonical_json

BACKUP_FORMAT = "ankiscape-game-backup"
BACKUP_VERSION = 1


def export_backup(game_uuid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a journal export_game() payload in a validated envelope."""
    body = canonical_json({"game_uuid": game_uuid,
                           "operations": payload.get("operations", []),
                           "observations": payload.get("observations", [])})
    return {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
            "game_uuid": game_uuid, "exported_at": int(time.time()),
            "sha256": hashlib.sha256(body).hexdigest(),
            "operations": payload.get("operations", []),
            "observations": payload.get("observations", [])}


def validate_backup(data: Dict[str, Any]) -> Dict[str, Any]:
    """Raise ValueError on format/version/hash mismatch; return data."""
    if not isinstance(data, dict):
        raise ValueError("backup must be a JSON object")
    if data.get("format") != BACKUP_FORMAT:
        raise ValueError(f"unknown backup format {data.get('format')!r}")
    if data.get("version") != BACKUP_VERSION:
        raise ValueError(f"unsupported backup version {data.get('version')!r}; refusing to overwrite")
    game_uuid = data.get("game_uuid")
    if not isinstance(game_uuid, str) or not game_uuid:
        raise ValueError("backup missing game_uuid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", game_uuid):
        raise ValueError("backup has an invalid game identifier")
    body = canonical_json({"game_uuid": game_uuid,
                           "operations": data.get("operations", []),
                           "observations": data.get("observations", [])})
    if hashlib.sha256(body).hexdigest() != data.get("sha256"):
        raise ValueError("backup hash mismatch; file is damaged")
    if not isinstance(data.get("operations"), list):
        raise ValueError("backup operations must be a list")
    return data
