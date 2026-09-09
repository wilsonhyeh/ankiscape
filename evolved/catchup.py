# evolved/catchup.py - History reconciliation scans (contract D, Qt-free).
"""Catch-up = previously uncredited eligible review history (including mobile
reviews synced to desktop and desktop study while the add-on was inactive) —
NOT proven mobile detection; revlog carries no device provenance.

Incremental fast path (every profile load): rows newer than the persisted
frontier. Full reconciliation (after collection sync, manual, driver): bounded
newest-first scan that also finds late arrivals older than the largest
observed review id. Never a full scan per answer.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

FULL_SCAN_ROW_CAP = 20000
FAST_PATH_CHUNK = 2000


def _frontier(journal) -> int:
    try:
        row = journal._conn.execute(
            "SELECT value FROM metadata WHERE key='catchup_frontier'").fetchone()
        return int(row["value"]) if row else 0
    except Exception:
        return 0


def _save_frontier(journal, revlog_id: int) -> None:
    try:
        journal._conn.execute(
            "INSERT INTO metadata(key, value) VALUES('catchup_frontier', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(revlog_id)),))
        journal._conn.commit()
    except Exception:
        pass


def collect_history(col, *, max_id: int = 0, limit: int = FULL_SCAN_ROW_CAP,
                    since_id: int = 0) -> List[Dict[str, Any]]:
    """Read (revlog_id, card_id, ease, type, ts) rows, newest first.

    since_id>0 restricts to newer rows (fast path); max_id>0 caps the window.
    Timestamps derive from revlog ids (milliseconds).
    """
    db = col.db
    query = "SELECT id, cid, ease, type FROM revlog"
    clauses, params = [], []
    if since_id > 0:
        clauses.append("id > ?")
        params.append(int(since_id))
    if max_id > 0:
        clauses.append("id <= ?")
        params.append(int(max_id))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    try:
        rows = db.all(query, *params)
    except Exception:
        return []
    return [{"revlog_id": int(r[0]), "card_id": int(r[1]), "ease": int(r[2]),
             "revlog_type": int(r[3]), "ts": int(r[0]) // 1000} for r in rows]


def run_catchup(col, engine, journal, *, full: bool = False,
                chunk: int = FAST_PATH_CHUNK) -> Dict[str, Any]:
    """Ingest uncredited eligible history through the engine. Returns counts."""
    if full:
        history = collect_history(col, limit=FULL_SCAN_ROW_CAP)
    else:
        history = collect_history(col, since_id=_frontier(journal), limit=chunk)
    # Attach review identities BEFORE eligibility/dedup: without review_key,
    # find_uncredited cannot recognize already-credited rows and the scan
    # re-ingests them as (harmless but littering) duplicate catch-up ops.
    from .reviews import make_review_key
    game_uuid = engine.cfg.game_uuid
    for row in history:
        try:
            row["review_key"] = make_review_key(
                game_uuid, int(row["revlog_id"]), int(row["card_id"]))
        except (KeyError, ValueError, TypeError):
            continue
    # engine.scan_catchup re-checks eligibility + dedups internally and
    # chunks the intake (cancellable, transactional per award).
    out = engine.scan_catchup(history, chunk=max(1, len(history)))
    try:
        newest = max(int(r.get("revlog_id", 0)) for r in history)
        if newest > _frontier(journal):
            _save_frontier(journal, newest)
    except ValueError:
        pass
    out["full"] = full
    return out
