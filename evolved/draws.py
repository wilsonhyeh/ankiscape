# evolved/draws.py - Deterministic SHA-256 draws (contract C).
"""For each deterministic draw, SHA-256 the UTF-8 compact JSON array
[rules_version, game_uuid, review_key, draw_label] with no spaces and the
string fields restricted to defined ASCII identifiers. First six digest bytes
are an unsigned big-endian integer r in [0, 2^48). Compare
r * denominator < numerator * 2^48 using exact integers.

Labels: action, gem_drop, gem_pick, burn. No floats, no language RNG.
"""
from __future__ import annotations

import hashlib
import json
import re
from fractions import Fraction

TWO_POW_48 = 1 << 48
_ASCII_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


def canonical_draw_input(rules_version: int, game_uuid: str, review_key: str, draw_label: str) -> bytes:
    for name, value in (("game_uuid", game_uuid), ("review_key", review_key), ("draw_label", draw_label)):
        if not isinstance(value, str) or not _ASCII_ID.match(value):
            raise ValueError(f"{name} must be ASCII [A-Za-z0-9_-]+, got {value!r}")
    if draw_label not in ("action", "gem_drop", "gem_pick", "burn"):
        raise ValueError(f"unknown draw_label {draw_label!r}")
    payload = json.dumps([rules_version, game_uuid, review_key, draw_label], separators=(",", ":"))
    return payload.encode("utf-8")


def draw_r(rules_version: int, game_uuid: str, review_key: str, draw_label: str) -> int:
    digest = hashlib.sha256(canonical_draw_input(rules_version, game_uuid, review_key, draw_label)).digest()
    return int.from_bytes(digest[:6], "big")


def draw_hits(r: int, numerator: int, denominator: int) -> bool:
    """Exact rational compare: r/2^48 < numerator/denominator."""
    if denominator <= 0 or numerator < 0:
        raise ValueError("bad probability fraction")
    if numerator == 0:
        return False
    if numerator >= denominator:
        return True
    return r * denominator < numerator * TWO_POW_48


def frac_hits(r: int, frac: Fraction) -> bool:
    return draw_hits(r, frac.numerator, frac.denominator)
