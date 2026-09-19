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

JOURNAL_SCHEMA_VERSION = 3
SYNC_POINTER_MAX_BYTES = 8 * 1024
# Interactive accepted-answer writes must never sit behind a long lock: 25 ms
# busy budget, then fail fast so the caller can show recovery instead of
# freezing the review. Background/bulk writes keep the longer budget.
INTERACTIVE_BUSY_MS = 25
DEFAULT_BUSY_MS = 30000

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

# v2: O(1) known-claim index for the incremental fast path. Derived from
# operations; rebuildable, never authoritative.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS known_review_keys (
  review_key TEXT PRIMARY KEY,
  lamport INTEGER NOT NULL,
  device_id TEXT NOT NULL,
  device_seq INTEGER NOT NULL,
  op_id TEXT NOT NULL,
  first_seen INTEGER NOT NULL
);
"""

# v3: rejected operations leave the outbox but are counted separately so the
# UI can say "N changes couldn't sync" instead of pretending everything is
# synced or showing them as still pending.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS rejected_operations (
  op_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL DEFAULT '',
  rejected_at INTEGER NOT NULL
);
"""


class JournalProtocolError(Exception):
    """A remote page violated the sync protocol. Nothing from that page is
    committed and the server cursor does not advance."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = str(code)[:80]
        self.detail = str(detail)[:200]


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
        self._conn.executescript(_SCHEMA_V2)
        self._conn.executescript(_SCHEMA_V3)
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
                self._migrate(v, fresh)

    def _migrate(self, from_v: int, fresh: bool = False) -> None:
        # v2 adds the known-claim index; backfill it once from operations in
        # canonical order so the first row per key is the earliest claim.
        if from_v < 2 and not fresh:
            self._conn.executescript(_SCHEMA_V2)
            now = int(time.time())
            seen = set()
            rows = self._conn.execute(
                "SELECT json_extract(payload_json, '$.review_key') AS rk,"
                " json_extract(payload_json, '$.target_review_key') AS trk,"
                " lamport, device_id, device_seq, op_id FROM operations"
                " ORDER BY lamport, device_id, device_seq, op_id").fetchall()
            for row in rows:
                for key in (row["rk"], row["trk"]):
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO known_review_keys(review_key,"
                        " lamport, device_id, device_seq, op_id, first_seen)"
                        " VALUES(?,?,?,?,?,?)",
                        (str(key), int(row["lamport"]), str(row["device_id"]),
                         int(row["device_seq"]), str(row["op_id"]), now))
        if from_v < 3:
            self._conn.executescript(_SCHEMA_V3)
        self._conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'",
                           (str(JOURNAL_SCHEMA_VERSION),))

    def _register_known_keys(self, op: Dict[str, Any]) -> None:
        """Call inside an open write transaction only."""
        now = int(time.time())
        payload = op.get("payload", {}) or {}
        for field in ("review_key", "target_review_key"):
            value = payload.get(field)
            if value:
                self._conn.execute(
                    "INSERT OR IGNORE INTO known_review_keys(review_key, lamport,"
                    " device_id, device_seq, op_id, first_seen)"
                    " VALUES(?,?,?,?,?,?)",
                    (str(value), int(op.get("lamport", 0)),
                     str(op.get("device_id", "")), int(op.get("device_seq", 0)),
                     str(op.get("op_id", "")), now))

    def is_known_review_key(self, review_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM known_review_keys WHERE review_key=? LIMIT 1",
                (str(review_key),)).fetchone()
        return row is not None

    def is_known_review_key_before(self, review_key: str, watermark) -> bool:
        """True when a claim for review_key exists at or before the canonical
        watermark (i.e. was already folded into the checkpoint)."""
        values = (int(watermark[0]), str(watermark[1]), int(watermark[2]),
                  str(watermark[3]))
        with self._lock:
            row = self._conn.execute(
                "SELECT lamport, device_id, device_seq, op_id FROM"
                " known_review_keys WHERE review_key=?", (str(review_key),)).fetchone()
        if row is None:
            return False
        record = (int(row["lamport"]), str(row["device_id"]),
                  int(row["device_seq"]), str(row["op_id"]))
        return record <= values

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

    def record_review(self, op: Dict[str, Any], *, review_key: str, revlog_id: int,
                      card_id: int, fingerprint: str) -> Dict[str, Any]:
        """One atomic transaction: allocate device sequence, persist the
        operation, enqueue the outbox entry and record the review
        observation together. A duplicate identical operation returns its
        existing committed identity; a changed payload for a reused op_id is
        a conflict. Interactive busy budget is capped (25 ms) so a busy
        database fails fast instead of stalling the review."""
        payload = op.get("payload", {}) or {}
        ph = payload_hash(payload)
        blob = canonical_json(payload).decode("utf-8")
        now = int(time.time())
        op_id = str(op.get("op_id", ""))
        with self._lock:
            self._conn.execute(f"PRAGMA busy_timeout={INTERACTIVE_BUSY_MS}")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT op_id, payload_hash, device_id, device_seq, lamport"
                    " FROM operations WHERE op_id=?", (op_id,)).fetchone()
                if existing is not None:
                    self._conn.execute("ROLLBACK")
                    if existing["payload_hash"] == ph:
                        return {"ok": True, "duplicate": True, "op_id": op_id,
                                "device_id": existing["device_id"],
                                "device_seq": int(existing["device_seq"]),
                                "lamport": int(existing["lamport"])}
                    return {"ok": False, "conflict": True, "op_id": op_id,
                            "error": "op_id_reused_changed_payload"}
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(device_seq),0) AS m FROM operations"
                    " WHERE device_id=?", (op["device_id"],)).fetchone()
                seq = int(row["m"]) + 1
                self._conn.execute(
                    "INSERT INTO operations(op_id, game_uuid, device_id, device_seq, lamport,"
                    " kind, payload_json, payload_hash, created_at, acked)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (op_id, op["game_uuid"], op["device_id"], seq,
                     int(op["lamport"]), op["kind"], blob, ph, now))
                self._conn.execute(
                    "INSERT OR IGNORE INTO outbox(op_id, enqueued_at) VALUES(?,?)",
                    (op_id, now))
                self._conn.execute(
                    "INSERT OR IGNORE INTO review_observations(review_key, revlog_id,"
                    " card_id, fingerprint, first_seen) VALUES(?,?,?,?,?)",
                    (str(review_key), int(revlog_id), int(card_id),
                     str(fingerprint), now))
                self._register_known_keys(op)
                self._conn.execute("COMMIT")
                return {"ok": True, "duplicate": False, "op_id": op_id,
                        "device_id": op["device_id"], "device_seq": seq,
                        "lamport": int(op["lamport"]), "payload_hash": ph}
            except sqlite3.OperationalError as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                return {"ok": False, "busy": True, "op_id": op_id,
                        "error": f"interactive_write_failed:{exc}"}
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                self._conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_MS}")

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
                self._register_known_keys(op)
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return dict(op, payload_hash=ph)

    def append_operation_auto_seq(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """Allocate the device sequence and persist op+outbox in one
        transaction (off the interactive path; callers need not pre-allocate)."""
        payload = op.get("payload", {}) or {}
        ph = payload_hash(payload)
        blob = canonical_json(payload).decode("utf-8")
        created = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(device_seq),0) AS m FROM operations"
                    " WHERE device_id=?", (op["device_id"],)).fetchone()
                seq = int(row["m"]) + 1
                self._conn.execute(
                    "INSERT INTO operations(op_id, game_uuid, device_id, device_seq, lamport,"
                    " kind, payload_json, payload_hash, created_at, acked)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (op["op_id"], op["game_uuid"], op["device_id"], seq,
                     int(op["lamport"]), op["kind"], blob, ph, created))
                self._conn.execute(
                    "INSERT OR IGNORE INTO outbox(op_id, enqueued_at) VALUES(?,?)",
                    (op["op_id"], created))
                self._register_known_keys(op)
                self._conn.execute("COMMIT")
                return dict(op, device_seq=seq, payload_hash=ph)
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

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

    def retire_outbox(self) -> int:
        """S4/R1: drain an UNBOUND journal's outbox; never touch operations.

        Applies only to a journal that has no `account_binding`: a bound
        journal belongs to the account game and is never drained, so a
        re-login can never delete the account game's pending work. On an
        unbound (offline) journal the outbox rows are deleted, `operations`
        rows are retained unchanged, `acked` is never set, and the
        `outbox_retired` marker is recorded. Returns the number of outbox
        rows removed; idempotent.
        """
        with self._lock:
            binding = self._conn.execute(
                "SELECT value FROM metadata WHERE key='account_binding'"
            ).fetchone()
            if binding is not None and str(binding["value"] or "").strip():
                return 0
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute("DELETE FROM outbox")
                removed = int(cur.rowcount or 0)
                self._conn.execute(
                    "INSERT INTO metadata(key, value) VALUES('outbox_retired','1')"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return removed

    def pending_operations(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.* FROM operations o JOIN outbox q ON q.op_id=o.op_id"
                " ORDER BY o.lamport, o.device_id, o.device_seq, o.op_id LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_pending_operations(self) -> int:
        """Outbox size for status display. Count only: the JOIN+ORDER BY
        form sorts the whole operations table (measured ~250 ms at 100k on
        the review path); COUNT over the outbox primary key is ~8 ms and is
        the only thing the pending label needs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM outbox").fetchone()
        return int(row["n"]) if row is not None else 0

    def observe_review(self, review_key: str, revlog_id: int, card_id: int, fingerprint: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO review_observations(review_key, revlog_id, card_id,"
                " fingerprint, first_seen) VALUES(?,?,?,?,?)",
                (review_key, int(revlog_id), int(card_id), fingerprint, int(time.time())))

    # ---------------------------------------------------------------- reads
    # Every access to the operations table goes through these methods. No
    # caller outside Journal touches `_conn`; the connection stays behind
    # one lock policy.

    @staticmethod
    def _row_to_op(row) -> Dict[str, Any]:
        import json as _json

        return {"op_id": row["op_id"], "game_uuid": row["game_uuid"],
                "device_id": row["device_id"], "device_seq": int(row["device_seq"]),
                "lamport": int(row["lamport"]), "kind": row["kind"],
                "payload": _json.loads(row["payload_json"])}

    def all_operations(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT op_id, game_uuid, device_id, device_seq, lamport, kind,"
                " payload_json FROM operations ORDER BY lamport, device_id,"
                " device_seq, op_id").fetchall()
        return [self._row_to_op(r) for r in rows]

    def operations_after(self, watermark) -> List[Dict[str, Any]]:
        """Canonical ops strictly after a (lamport, device_id, device_seq,
        op_id) watermark, in canonical order."""
        values = (int(watermark[0]), str(watermark[1]), int(watermark[2]),
                  str(watermark[3]))
        with self._lock:
            rows = self._conn.execute(
                "SELECT op_id, game_uuid, device_id, device_seq, lamport, kind,"
                " payload_json FROM operations"
                " WHERE (lamport, device_id, device_seq, op_id) > (?,?,?,?)"
                " ORDER BY lamport, device_id, device_seq, op_id", values).fetchall()
        return [self._row_to_op(r) for r in rows]

    def count_operations_through(self, watermark) -> int:
        values = (int(watermark[0]), str(watermark[1]), int(watermark[2]),
                  str(watermark[3]))
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM operations"
                " WHERE (lamport, device_id, device_seq, op_id) <= (?,?,?,?)",
                values).fetchone()
        return int(row["n"])

    def operation_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM operations").fetchone()
        return int(row["n"])

    def max_lamport(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(lamport),0) AS m FROM operations").fetchone()
        return int(row["m"])

    def operation_ids(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT op_id FROM operations ORDER BY op_id").fetchall()
        return [str(r["op_id"]) for r in rows]

    def find_operation_payload(self, op_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM operations WHERE op_id=?",
                (str(op_id),)).fetchone()
        if row is None:
            return None
        import json as _json

        try:
            return _json.loads(row["payload_json"])
        except ValueError:
            return None

    def review_observations(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT review_key, revlog_id, card_id, fingerprint FROM"
                " review_observations ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def latest_review_disposition(self, review_key: str) -> str:
        """'retracted', 'active', or 'unknown' (no retraction history)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind FROM operations WHERE kind IN"
                " ('review_retract', 'review_restore') AND"
                " json_extract(payload_json, '$.target_review_key') = ?"
                " ORDER BY lamport DESC, device_id, device_seq LIMIT 1",
                (str(review_key),)).fetchall()
        if not rows:
            return "active"
        return "retracted" if rows[0]["kind"] == "review_retract" else "active"

    def get_metadata(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key=?", (str(key),)).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)))

    # ----------------------------------------------------------- checkpoints

    def load_checkpoint(self, game_uuid: str) -> Optional[Dict[str, Any]]:
        """Derived projection cache. Disposable: corruption/version mismatch
        must fall back to a full replay, never fail the game."""
        with self._lock:
            row = self._conn.execute(
                "SELECT revision, state_json FROM projection_checkpoints"
                " WHERE game_uuid=?", (str(game_uuid),)).fetchone()
        if row is None:
            return None
        import json as _json

        try:
            loaded = _json.loads(row["state_json"])
        except ValueError:
            return None
        if not isinstance(loaded, dict):
            return None
        from .reducer import prepare_checkpoint

        return prepare_checkpoint(loaded)

    def save_checkpoint(self, game_uuid: str, revision: int,
                        checkpoint: Dict[str, Any]) -> None:
        """Serialize first, then one short write transaction."""
        from .reducer import checkpoint_for_storage, prepare_checkpoint

        prepare_checkpoint(checkpoint)
        blob = json.dumps(checkpoint_for_storage(checkpoint),
                          separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO projection_checkpoints(game_uuid, revision,"
                    " state_json, updated_at) VALUES(?,?,?,?)"
                    " ON CONFLICT(game_uuid) DO UPDATE SET"
                    " revision=excluded.revision, state_json=excluded.state_json,"
                    " updated_at=excluded.updated_at",
                    (str(game_uuid), int(revision), blob, int(time.time())))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_checkpoint(self, game_uuid: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM projection_checkpoints WHERE game_uuid=?",
                (str(game_uuid),))

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

    def quarantine(self, game_uuid: str, op_ids: List[str],
                   reason: str = "") -> None:
        # Permanent invalid ops leave the outbox but stay in operations for
        # diagnostics; they are never retried forever. They are counted in
        # rejected_operations so "all synced" stays truthful.
        _ = game_uuid
        now = int(time.time())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for oid in op_ids:
                    self._conn.execute(
                        "INSERT INTO rejected_operations(op_id, reason,"
                        " rejected_at) VALUES(?,?,?)"
                        " ON CONFLICT(op_id) DO UPDATE SET"
                        " reason=excluded.reason, rejected_at=excluded.rejected_at",
                        (str(oid), str(reason)[:120], now))
                    self._conn.execute("DELETE FROM outbox WHERE op_id=?", (oid,))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def count_rejected_operations(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rejected_operations").fetchone()
        return int(row["n"]) if row is not None else 0

    def ingest_remote_page(self, game_uuid: str, page: Any, *,
                           cursor: str = "") -> Dict[str, Any]:
        """Persist one downloaded page and its cursor in ONE transaction.

        Validates every row before committing anything: game/operation ids,
        sequence/lamport shapes, payload type and payload hashes. Exact
        duplicates are idempotent no-ops. An id reuse with changed content, a
        malformed row or a non-advancing cursor with more pages raises
        JournalProtocolError and commits NOTHING (the cursor must not move).
        Remote rows never enter the upload outbox.

        `page` may be either the transport dict
        {"operations": [...], "next_cursor": ..., "has_more": ...} or an
        explicit operations list. `cursor` overrides the page's next cursor
        when provided.
        """
        if isinstance(page, dict):
            raw_ops = page.get("operations", [])
            next_cursor = cursor or str(page.get("next_cursor", "") or "")
            has_more = bool(page.get("has_more"))
        else:
            raw_ops = page or []
            next_cursor = str(cursor or "")
            has_more = False
        if not isinstance(raw_ops, list):
            raise JournalProtocolError("malformed_page", "operations not a list")

        prepared: List[Dict[str, Any]] = []
        for index, row in enumerate(raw_ops):
            if not isinstance(row, dict):
                raise JournalProtocolError("malformed_row", f"row {index}")
            op_id = str(row.get("op_id", "") or "")
            kind = str(row.get("kind", "") or "")
            payload = row.get("payload")
            if not op_id or len(op_id) > 64:
                raise JournalProtocolError("malformed_row", f"op_id row {index}")
            if not kind or len(kind) > 40:
                raise JournalProtocolError("malformed_row", f"kind row {index}")
            if not isinstance(payload, dict):
                raise JournalProtocolError("malformed_row",
                                           f"payload row {index}")
            row_game = str(row.get("game_uuid", "") or game_uuid)
            if row_game != str(game_uuid):
                raise JournalProtocolError("foreign_game", f"row {index}")
            try:
                device_seq = int(row.get("device_seq", 0) or 0)
                lamport = int(row.get("lamport", 0) or 0)
            except (TypeError, ValueError):
                raise JournalProtocolError("malformed_row",
                                           f"sequence row {index}")
            if device_seq < 0 or lamport < 0:
                raise JournalProtocolError("malformed_row",
                                           f"negative sequence row {index}")
            prepared.append({
                "op_id": op_id, "game_uuid": str(game_uuid),
                "device_id": str(row.get("device_id", "remote") or "remote"),
                "device_seq": device_seq, "lamport": lamport, "kind": kind,
                "payload": payload, "payload_hash": payload_hash(payload)})

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                stored = 0
                duplicates = 0
                for op in prepared:
                    existing = self._conn.execute(
                        "SELECT payload_hash FROM operations WHERE op_id=?",
                        (op["op_id"],)).fetchone()
                    if existing is not None:
                        if existing["payload_hash"] == op["payload_hash"]:
                            duplicates += 1
                            continue
                        raise JournalProtocolError("id_conflict", op["op_id"])
                    try:
                        self._conn.execute(
                            "INSERT INTO operations(op_id, game_uuid, device_id,"
                            " device_seq, lamport, kind, payload_json, payload_hash,"
                            " created_at, acked) VALUES(?,?,?,?,?,?,?,?,?,1)",
                            (op["op_id"], op["game_uuid"], op["device_id"],
                             op["device_seq"], op["lamport"], op["kind"],
                             canonical_json(op["payload"]).decode("utf-8"),
                             op["payload_hash"], int(time.time())))
                    except sqlite3.IntegrityError as exc:
                        raise JournalProtocolError("sequence_conflict",
                                                   str(exc)[:120])
                    self._register_known_keys(op)
                    stored += 1
                if next_cursor:
                    current = self._current_cursor_locked(game_uuid)
                    try:
                        new_value = int(next_cursor)
                        old_value = int(current or 0)
                    except (TypeError, ValueError):
                        raise JournalProtocolError("malformed_cursor",
                                                   str(next_cursor)[:60])
                    if new_value < old_value:
                        raise JournalProtocolError("cursor_regressed",
                                                   f"{new_value} < {old_value}")
                    if has_more and new_value <= old_value:
                        raise JournalProtocolError("cursor_not_advanced",
                                                   f"{new_value}")
                    self._conn.execute(
                        "INSERT INTO server_checkpoints(game_uuid, server_cursor,"
                        " revision, updated_at) VALUES(?,?,0,?)"
                        " ON CONFLICT(game_uuid) DO UPDATE SET"
                        " server_cursor=excluded.server_cursor,"
                        " updated_at=excluded.updated_at",
                        (str(game_uuid), str(next_cursor), int(time.time())))
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return {"stored": stored, "duplicates": duplicates,
                "cursor": str(next_cursor or cursor or "")}

    def _current_cursor_locked(self, game_uuid: str) -> str:
        row = self._conn.execute(
            "SELECT server_cursor FROM server_checkpoints WHERE game_uuid=?",
            (game_uuid,)).fetchone()
        return str(row["server_cursor"]) if row else ""

    def set_sync_success(self, endpoint_project: str, ts: float) -> None:
        """Persist the last successful pass per endpoint project so the status
        survives restart without leaking credentials."""
        import json as _json

        try:
            self.set_metadata("sync_last_success", _json.dumps({
                "endpoint_project": str(endpoint_project or ""),
                "last_success": float(ts)}))
        except Exception:
            pass

    def get_sync_success(self, endpoint_project: str) -> Optional[float]:
        import json as _json

        try:
            raw = self.get_metadata("sync_last_success")
            if not raw:
                return None
            data = _json.loads(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if str(data.get("endpoint_project", "")) != str(endpoint_project or ""):
            return None
        value = data.get("last_success")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
                        self._register_known_keys(op)
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
