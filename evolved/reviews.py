# evolved/reviews.py - Eligibility, identity, catch-up and Undo (contract D).
"""Eligible: accepted ratings 2/3/4 in learning/normal review/relearning/
filtered study, on/after this game's Evolved activation. Rating 1, manual
rescheduling (ease 0), browser preview, mere reveal and cancelled/failed
scheduling do not award. Catch-up = previously uncredited eligible history,
not proven mobile detection (revlog has no device provenance).
"""
from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Tuple

ELIGIBLE_EASE = {2, 3, 4}
# Anki revlog review kinds (integers) accepted: learning=0, review=1,
# relearn=2, filtered/cram=3. Filtered-study reviews count.
ELIGIBLE_REVLOG_TYPES = {0, 1, 2, 3}


def is_eligible(*, ease: int, revlog_type: int, review_ts: int, activated_at: int,
                preview: bool = False, cancelled: bool = False) -> bool:
    if preview or cancelled:
        return False
    if ease == 0:
        return False  # manual rescheduling / no scheduling change
    if ease not in ELIGIBLE_EASE:
        return False
    if revlog_type not in ELIGIBLE_REVLOG_TYPES:
        return False
    try:
        if int(review_ts) < int(activated_at):
            return False
    except (ValueError, TypeError):
        return False
    return True


def canonical_review_identity(revlog_id: int, card_id: int) -> str:
    return f"{int(revlog_id)}:{int(card_id)}"


def make_review_key(game_uuid: str, revlog_id: int, card_id: int) -> str:
    """review_key = SHA256(game_uuid + canonical revlog/card identity), hex."""
    ident = canonical_review_identity(revlog_id, card_id)
    return hashlib.sha256(f"{game_uuid}:{ident}".encode("utf-8")).hexdigest()


def immutable_fingerprint(*, revlog_id: int, card_id: int, ease: int,
                          review_ts: int, revlog_type: int) -> str:
    """Local fingerprint of immutable fields to detect conflicting records.
    Sync sequence numbers are excluded from identity."""
    blob = f"{int(revlog_id)}|{int(card_id)}|{int(ease)}|{int(review_ts)}|{int(revlog_type)}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def find_uncredited(history: Iterable[Dict], processed_keys: set,
                    activation_ts: int) -> Tuple[List[Dict], Dict]:
    """Incremental reconciliation scan over history rows.

    Each row: {revlog_id, card_id, ease, revlog_type, ts, preview?, cancelled?}.
    Returns (eligible_uncredited_in_ts_order, stats). Handles late arrivals
    older than the largest observed review id via durable processed identity,
    not a single high-water mark. Never mutates Anki data.
    """
    rows = sorted(history, key=lambda r: (int(r.get("ts", 0)), int(r.get("revlog_id", 0))))
    out = []
    late = 0
    max_seen = 0
    for row in rows:
        rid = int(row.get("revlog_id", 0))
        max_seen = max(max_seen, rid)
        try:
            eligible = is_eligible(ease=int(row.get("ease", 0)),
                                   revlog_type=int(row.get("revlog_type", 1)),
                                   review_ts=int(row.get("ts", 0)),
                                   activated_at=activation_ts,
                                   preview=bool(row.get("preview", False)),
                                   cancelled=bool(row.get("cancelled", False)))
        except (ValueError, TypeError):
            continue
        if not eligible:
            continue
        key = row.get("review_key") or ""
        if key in processed_keys:
            continue
        out.append(row)
    stats = {"eligible_uncredited": len(out), "max_revlog_id": max_seen,
             "processed_inputs": len(processed_keys)}
    return out, stats
