# evolved/journal.py - Durable local journal (contract B, stdlib sqlite3).
"""Add-on-owned SQLite at <profile>/ankiscape-evolved/<game_uuid>/game.sqlite3.
Never touches Anki tables. Writes are transactional and serialized; backups use
SQLite's backup API. Persist an operation before showing a durable award; if
persistence fails, keep reviewing but report that the game needs recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

JOURNAL_SCHEMA_VERSION = 1
SYNC_POINTER_MAX_BYTES = 8 * 1024

_SCHEMA_V1 = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  op_id TEXT PRIMARY KEY,
  game_uuid TEXT NOT NULL,
  device_id TEXT NOT NULL,
  device_seq INTEGER NOT NULL,
  lamport INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  acked INTEGER NOT NULL DEFAULT 0,
  UNIQUE(device_id, device_seq)
);
CREATE INDEX IF NOT EXISTS idx_ops_game_canonical
  ON operations(game_uuid, lamport, device_id, device_seq, op_id);
CREATE TABLE IF NOT EXISTS review_observations (
  review_key TEXT PRIMARY KEY,
  revlog_id INTEGER NOT NULL,
  card_id INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  first_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_revlog ON review_observations(revlog_id);
CREATE TABLE IF NOT EXISTS outbox (
  op_id TEXT PRIMARY KEY REFERENCES operations(op_id) ON DELETE CASCADE,
  enqueued_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS server_checkpoints (
  game_uuid TEXT PRIMARY KEY,
  server_cursor TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projection_checkpoints (
  game_uuid TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30.0, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'")
        fresh = cur.fetchone() is None
        self._conn.executescript(_SCHEMA_V1)
        row = self._conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                               (str(JOURNAL_SCHEMA_VERSION),))
        else:
            try:
                v = int(row["value"])
            except (ValueError, TypeError):
                raise RuntimeError("journal schema_version damaged; export/recover required")
            if v > JOURNAL_SCHEMA_VERSION:
                raise RuntimeError(f"unsupported future journal schema v{v}; refusing to overwrite")
            if v < JOURNAL_SCHEMA_VERSION:
                self._migrate(v)

    def _migrate(self, from_v: int) -> None:
        # v1 is the first version; future migrations chain here.
        self._conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'",
                           (str(JOURNAL_SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except Exception:
                pass
            self._conn.close()

    def allocate_seq(self, device_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(device_seq),0) AS m FROM operations WHERE device_id=?",
                (device_id,)).fetchone()
            return int(row["m"]) + 1

    def append_operation(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """Persist op transactionally; returns stored row. Raises on conflict."""
        payload = op.get("payload", {}) or {}
        ph = payload_hash(payload)
        created = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO operations(op_id, game_uuid, device_id, device_seq, lamport,"
                    " kind, payload_json, payload_hash, created_at, acked)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (op["op_id"], op["game_uuid"], op["device_id"], int(op["device_seq"]),
                     int(op["lamport"]), op["kind"], canonical_json(payload).decode("utf-8"),
                     ph, created))
                self._conn.execute("INSERT OR IGNORE INTO outbox(op_id, enqueued_at) VALUES(?,?)",
                                   (op["op_id"], created))
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return dict(op, payload_hash=ph)

    def mark_acked(self, op_ids: List[str]) -> None:
        if not op_ids:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for oid in op_ids:
                    self._conn.execute("UPDATE operations SET acked=1 WHERE op_id=?", (oid,))
                    self._conn.execute("DELETE FROM outbox WHERE op_id=?", (oid,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def pending_operations(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.* FROM operations o JOIN outbox q ON q.op_id=o.op_id"
                " ORDER BY o.lamport, o.device_id, o.device_seq, o.op_id LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def observe_review(self, review_key: str, revlog_id: int, card_id: int, fingerprint: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO review_observations(review_key, revlog_id, card_id,"
                " fingerprint, first_seen) VALUES(?,?,?,?,?)",
                (review_key, int(revlog_id), int(card_id), fingerprint, int(time.time())))

    def get_server_cursor(self, game_uuid: str) -> str:
        row = self._conn.execute(
            "SELECT server_cursor FROM server_checkpoints WHERE game_uuid=?",
            (game_uuid,)).fetchone()
        return str(row["server_cursor"]) if row else ""

    def set_server_cursor(self, game_uuid: str, cursor: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO server_checkpoints(game_uuid, server_cursor, revision, updated_at)"
                " VALUES(?,?,0,?) ON CONFLICT(game_uuid) DO UPDATE SET server_cursor=excluded.server_cursor,"
                " updated_at=excluded.updated_at",
                (game_uuid, str(cursor), int(time.time())))

    def quarantine(self, game_uuid: str, op_ids: List[str]) -> None:
        # Permanent invalid ops leave the outbox but stay in operations for
        # diagnostics; they are never retried forever.
        _ = game_uuid
        with self._lock:
            for oid in op_ids:
                self._conn.execute("DELETE FROM outbox WHERE op_id=?", (oid,))

    def export_game(self, game_uuid: str) -> Dict[str, Any]:
        """Full operation + observation dump for Export Game Backup."""
        import json as _json

        with self._lock:
            ops = self._conn.execute(
                "SELECT op_id, game_uuid, device_id, device_seq, lamport, kind,"
                " payload_json FROM operations WHERE game_uuid=? ORDER BY lamport,"
                " device_id, device_seq, op_id", (game_uuid,)).fetchall()
            obs = self._conn.execute(
                "SELECT review_key, revlog_id, card_id, fingerprint FROM"
                " review_observations").fetchall()
        return {"game_uuid": game_uuid,
                "operations": [{"op_id": r["op_id"], "game_uuid": r["game_uuid"],
                                "device_id": r["device_id"],
                                "device_seq": r["device_seq"], "lamport": r["lamport"],
                                "kind": r["kind"],
                                "payload": _json.loads(r["payload_json"])} for r in ops],
                "observations": [dict(r) for r in obs]}

    def import_game(self, game_uuid: str, data: Dict[str, Any]) -> Dict[str, int]:
        """Restore from a validated backup. Existing op_ids are kept (no dupes)."""
        ops = data.get("operations", [])
        obs = data.get("observations", [])
        added_ops = 0
        added_obs = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for op in ops:
                    if op.get("game_uuid") != game_uuid:
                        continue
                    payload = op.get("payload", {}) or {}
                    try:
                        cur = self._conn.execute(
                            "INSERT OR IGNORE INTO operations(op_id, game_uuid, device_id,"
                            " device_seq, lamport, kind, payload_json, payload_hash,"
                            " created_at, acked) VALUES(?,?,?,?,?,?,?,?,?,0)",
                            (op["op_id"], game_uuid, op["device_id"],
                             int(op["device_seq"]), int(op["lamport"]), op["kind"],
                             canonical_json(payload).decode("utf-8"),
                             payload_hash(payload), int(time.time())))
                        added_ops += cur.rowcount
                        self._conn.execute(
                            "INSERT OR IGNORE INTO outbox(op_id, enqueued_at) VALUES(?,?)",
                            (op["op_id"], int(time.time())))
                    except (KeyError, ValueError, TypeError):
                        continue
                for ob in obs:
                    try:
                        cur = self._conn.execute(
                            "INSERT OR IGNORE INTO review_observations(review_key, revlog_id,"
                            " card_id, fingerprint, first_seen) VALUES(?,?,?,?,?)",
                            (ob["review_key"], int(ob["revlog_id"]), int(ob["card_id"]),
                             ob["fingerprint"], int(time.time())))
                        added_obs += cur.rowcount
                    except (KeyError, ValueError, TypeError):
                        continue
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"operations": added_ops, "observations": added_obs}

    def backup_to(self, dest_path: str) -> None:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        dest = sqlite3.connect(dest_path)
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()


def journal_dir_for_profile(profile_dir: str, game_uuid: str) -> str:
    return os.path.join(profile_dir, "ankiscape-evolved", game_uuid)


def journal_path_for_profile(profile_dir: str, game_uuid: str) -> str:
    return os.path.join(journal_dir_for_profile(profile_dir, game_uuid), "game.sqlite3")


def build_sync_pointer(*, version: int = 1, game_uuid: str, activated_at: int,
                       snapshot_revision: int, preset_skill: str,
                       preset_effective_ts: int) -> Dict[str, Any]:
    """Small synced cache/bootstrap pointer (<=8 KiB serialized), not authority."""
    pointer = {
        "version": version,
        "game_uuid": game_uuid,
        "activated_at": int(activated_at),
        "snapshot_revision": int(snapshot_revision),
        "preset": {"skill": preset_skill, "effective_ts": int(preset_effective_ts)},
    }
    blob = canonical_json(pointer)
    if len(blob) > SYNC_POINTER_MAX_BYTES:
        raise ValueError("sync pointer exceeds 8 KiB bound")
    return pointer
