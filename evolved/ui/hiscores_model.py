# evolved/ui/hiscores_model.py - Pure Hiscores presentation logic (no Qt).
"""Everything the Hiscores screen decides that is not painting: which boards
exist, levels from XP, how ties and untrained players are shown, where "you"
stand and what the next rank costs, rank movement since the last visit, and
the data behind a player card.

Input rows are the formatted rows from `menu_model.format_hiscores_rows`:
`{"rank": int|None, "username": str, "xp": "<micro as str>",
"xp_display": str, "is_demo": bool}`. XP is micro-XP throughout; nothing here
uses floats for XP.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..logic_pure import level_from_xp_micro

MICRO = 1_000_000
SKILLS: Tuple[str, ...] = ("mining", "woodcutting", "smithing", "crafting",
                           "fishing", "cooking")
OVERALL = "overall"
BOARDS: Tuple[str, ...] = (OVERALL,) + SKILLS
# The server caps a board at 100 rows. A shorter list is the whole population.
BOARD_LIMIT = 100
MEDALS = {1: "gold", 2: "silver", 3: "bronze"}
SORT_XP = "xp"
SORT_LEVEL = "level"
MOVE_MIN_AGE_S = 3600.0


def board_title(board: str) -> str:
    return "Overall" if board == OVERALL else str(board).title()


def xp_int(row_or_micro: Any) -> int:
    """Micro-XP as an int from a row, a string or an int; bad input is 0."""
    value = row_or_micro.get("xp") if isinstance(row_or_micro, dict) \
        else row_or_micro
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def format_whole_xp(micro: Any) -> str:
    """Whole XP with thousands separators. Fractions are dropped, not
    rounded, so a display never claims more than the player has."""
    return f"{xp_int(micro) // MICRO:,}"


def level_of(micro: Any, thresholds: Sequence[int]) -> int:
    if not thresholds:
        return 1
    return level_from_xp_micro(xp_int(micro), thresholds)


# ------------------------------------------------------------------ boards --

def split_trained(rows: Iterable[Dict[str, Any]]
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(trained, untrained). Zero-XP players share one giant tied rank that
    says nothing, so boards list the trained and count the rest."""
    trained, untrained = [], []
    for row in rows or []:
        (trained if xp_int(row) > 0 else untrained).append(row)
    return trained, untrained


def untrained_note(board: str, count: int) -> str:
    if count <= 0:
        return ""
    who = "1 player hasn't" if count == 1 else f"{count} players haven't"
    where = "started playing yet" if board == OVERALL \
        else f"trained {board_title(board)} yet"
    return f"{who} {where}."


def empty_board_copy(board: str, population: int) -> str:
    """Shown when nobody has XP on a board. Says whose turn it is."""
    if board == OVERALL:
        return "No one has earned XP yet. Answer some cards and be the first."
    name = board_title(board)
    if population <= 0:
        return "No players yet."
    return f"No one has trained {name} yet. Be the first on the board."


def population(rows: Sequence[Dict[str, Any]]) -> Tuple[int, bool]:
    """(player count, exact). `exact` is False when the board hit the cap."""
    n = len(rows or [])
    return n, n < BOARD_LIMIT


def ordinal(n: int) -> str:
    n = int(n)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------- standing --

def find_row(rows: Sequence[Dict[str, Any]], username: str
             ) -> Optional[Dict[str, Any]]:
    name = str(username or "").casefold()
    if not name:
        return None
    for row in rows or []:
        if str(row.get("username", "")).casefold() == name:
            return row
    return None


def standing(rows: Sequence[Dict[str, Any]], username: str,
             thresholds: Sequence[int] = ()) -> Optional[Dict[str, Any]]:
    """Where `username` stands on one board, or None when absent.

    `target` is the closest player strictly ahead (least XP that beats mine),
    which is who you actually have to pass. Players tied with you are not
    ahead of you. `gap` is the micro-XP still needed to reach the target.
    """
    me = find_row(rows, username)
    if me is None:
        return None
    mine = xp_int(me)
    ahead = [r for r in rows if xp_int(r) > mine]
    target = None
    if ahead:
        target = min(ahead, key=lambda r: (xp_int(r), int(r.get("rank") or 0)))
    tied = sum(1 for r in rows if xp_int(r) == mine and r is not me)
    count, exact = population(rows)
    return {
        "rank": me.get("rank"),
        "xp": mine,
        "level": level_of(mine, thresholds) if thresholds else None,
        "tied_with": tied,
        "trained": mine > 0,
        "target": target["username"] if target else None,
        "target_rank": target.get("rank") if target else None,
        "gap": (xp_int(target) - mine) if target else 0,
        "players": count,
        "players_exact": exact,
        "leader": target is None and mine > 0,
    }


def rerank_by_level(rows: Sequence[Dict[str, Any]]
                    ) -> Optional[List[Dict[str, Any]]]:
    """Overall ordered by total level, ties broken by XP.

    The server ranks Overall by total XP only, so this re-ranks on the client
    from rows that already carry `level` (the total level). Players share a
    rank only when BOTH total level and XP are equal. Returns None while any
    total level is still unknown, so a half-loaded board is never presented
    as a level ranking.
    """
    rows = list(rows or [])
    if any(r.get("level") is None for r in rows):
        return None
    ordered = sorted(rows, key=lambda r: (
        -int(r["level"]), -xp_int(r), str(r.get("username", "")).casefold()))
    out: List[Dict[str, Any]] = []
    last, rank = None, 0
    for i, row in enumerate(ordered, start=1):
        key = (int(row["level"]), xp_int(row))
        if key != last:
            rank, last = i, key
        out.append(dict(row, rank=rank))
    return out


def standing_by_level(rows: Sequence[Dict[str, Any]], username: str
                      ) -> Optional[Dict[str, Any]]:
    """`standing` for a level-ranked Overall board (rows from
    `rerank_by_level`). The target is the closest player strictly ahead in
    (total level, XP) order; the gap is given in levels when their level is
    higher and in XP when the level is the same."""
    me = find_row(rows, username)
    if me is None:
        return None
    mine = (int(me["level"]), xp_int(me))

    def key(r):
        return (int(r["level"]), xp_int(r))

    ahead = [r for r in rows if key(r) > mine]
    target = min(ahead, key=lambda r: (key(r), int(r.get("rank") or 0))) \
        if ahead else None
    count, exact = population(rows)
    return {
        "mode": SORT_LEVEL,
        "rank": me.get("rank"),
        "xp": mine[1],
        "level": mine[0],
        "tied_with": sum(1 for r in rows if key(r) == mine and r is not me),
        "trained": mine[1] > 0,
        "target": target["username"] if target else None,
        "target_rank": target.get("rank") if target else None,
        "gap_levels": (int(target["level"]) - mine[0]) if target else 0,
        "gap": (xp_int(target) - mine[1]) if target else 0,
        "players": count,
        "players_exact": exact,
        "leader": target is None and mine[1] > 0,
    }


def standing_text(st: Optional[Dict[str, Any]], board: str) -> Dict[str, str]:
    """Copy for the pinned 'You' bar: headline + the next goal. The bar has a
    "YOU" tag, so the headline never restates it."""
    if st is None:
        return {"headline": "Not on this board yet.",
                "goal": "Sync after some reviews to appear.",
                "rank": ""}
    if not st["trained"]:
        what = "any XP" if board == OVERALL else f"any {board_title(board)} XP"
        return {"headline": "Not ranked yet.",
                "goal": f"Earn {what} to get on the board.", "rank": ""}
    of = f" of {st['players']}" if st["players_exact"] else ""
    tie = f" (tied with {st['tied_with']})" if st["tied_with"] else ""
    rank = f"#{st['rank']}" if st.get("rank") else "unranked"
    headline = f"Rank {rank}{of}{tie}"
    if st["leader"]:
        goal = "You lead this board. Stay ahead."
    elif st.get("mode") == SORT_LEVEL and st["gap_levels"] > 0:
        n = st["gap_levels"]
        goal = (f"{n} more total level{'s' if n != 1 else ''} to reach "
                f"{st['target']}")
    elif st.get("mode") == SORT_LEVEL:
        goal = (f"{format_whole_xp(st['gap'])} XP to pass {st['target']} "
                "(same total level)" if st["gap"] >= MICRO else
                f"Under 1 XP behind {st['target']}")
    else:
        goal = (f"{format_whole_xp(st['gap'])} XP to pass {st['target']}"
                if st["gap"] >= MICRO else
                f"Under 1 XP behind {st['target']}")
    return {"headline": headline, "goal": goal, "rank": rank}


# ---------------------------------------------------------------- movement --

def snapshot_ranks(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows or []:
        name = str(row.get("username", "")).casefold()
        rank = row.get("rank")
        if name and isinstance(rank, int):
            out[name] = rank
    return out


def movement(previous: Optional[Dict[str, Any]],
             rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    """username(casefold) -> rank places gained (positive = climbed).

    None means the player is new since the last snapshot. With no previous
    snapshot at all there is nothing to compare, so the result is empty and
    no arrows are drawn: absence of data is not a "new player" everywhere.
    """
    if not isinstance(previous, dict):
        return {}
    ranks = previous.get("ranks")
    if not isinstance(ranks, dict) or not ranks:
        return {}
    out: Dict[str, Optional[int]] = {}
    for row in rows or []:
        name = str(row.get("username", "")).casefold()
        rank = row.get("rank")
        if not name or not isinstance(rank, int):
            continue
        before = ranks.get(name)
        out[name] = None if not isinstance(before, int) else before - rank
    return out


def should_roll_snapshot(previous: Optional[Dict[str, Any]], now: float,
                         min_age: float = MOVE_MIN_AGE_S) -> bool:
    """Replace the saved snapshot only once it is old enough that "since your
    last visit" still means something on the next open."""
    if not isinstance(previous, dict):
        return True
    try:
        return (float(now) - float(previous.get("ts") or 0.0)) >= min_age
    except (TypeError, ValueError):
        return True


def movement_glyph(delta: Optional[int]) -> Tuple[str, str]:
    """(glyph, kind). Never colour-only: the glyph and count carry it."""
    if delta is None:
        return ("NEW", "new")
    if delta > 0:
        return (f"▲{delta}", "up")
    if delta < 0:
        return (f"▼{-delta}", "down")
    return ("", "flat")


# ------------------------------------------------------------ player index --

def player_index(boards: Dict[str, Sequence[Dict[str, Any]]]
                 ) -> Dict[str, Dict[str, Any]]:
    """casefold name -> {"name", "xp": {skill: micro|None}, "ranks": {board: int}}.

    A player missing from a *complete* skill board (shorter than the cap) has
    0 XP there. Missing from a capped board means unknown, stored as None so
    a level is never invented.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for board, rows in boards.items():
        for row in rows or []:
            key = str(row.get("username", "")).casefold()
            if not key:
                continue
            entry = index.setdefault(
                key, {"name": row.get("username"), "xp": {}, "ranks": {},
                      "is_demo": False})
            entry["ranks"][board] = row.get("rank")
            if row.get("is_demo") is True:
                entry["is_demo"] = True
            if board != OVERALL:
                entry["xp"][board] = xp_int(row)
    for board in SKILLS:
        rows = boards.get(board)
        if rows is None:
            continue
        _, exact = population(rows)
        for entry in index.values():
            if board not in entry["xp"]:
                entry["xp"][board] = 0 if exact else None
    return index


def total_level(xp_by_skill: Dict[str, Optional[int]],
                thresholds: Sequence[int]) -> Optional[int]:
    """Sum of the six skill levels, or None if any skill is unknown."""
    total = 0
    for skill in SKILLS:
        value = xp_by_skill.get(skill)
        if value is None:
            return None
        total += level_of(value, thresholds)
    return total


def card_rows(entry: Dict[str, Any], thresholds: Sequence[int]
              ) -> List[Dict[str, Any]]:
    """Six skill lines for a player card, in board order."""
    out = []
    for skill in SKILLS:
        xp = entry["xp"].get(skill)
        rank = entry["ranks"].get(skill)
        known = xp is not None
        out.append({
            "skill": skill,
            "title": board_title(skill),
            "xp": xp if known else None,
            "level": level_of(xp, thresholds) if known else None,
            "rank": rank if known and xp else None,
            "trained": bool(known and xp),
        })
    return out


def card_summary(entry: Dict[str, Any], thresholds: Sequence[int]
                 ) -> Dict[str, Any]:
    xp_all = entry["xp"]
    known = all(xp_all.get(s) is not None for s in SKILLS)
    total_xp = sum(xp_all.get(s) or 0 for s in SKILLS)
    return {
        "name": entry["name"],
        "is_demo": bool(entry.get("is_demo")),
        "total_level": total_level(xp_all, thresholds),
        "total_xp": total_xp if known else None,
        "overall_rank": entry["ranks"].get(OVERALL),
        "best": best_skill(entry, thresholds),
    }


def best_skill(entry: Dict[str, Any], thresholds: Sequence[int]
               ) -> Optional[str]:
    best, best_xp = None, 0
    for skill in SKILLS:
        xp = entry["xp"].get(skill) or 0
        if xp > best_xp:
            best, best_xp = skill, xp
    return best


def entry_from_profile(name: str, xp_table: Dict[str, Any],
                       is_demo: bool = False) -> Dict[str, Any]:
    """Card entry from a `public_profile` xp table (the server's whole per-skill
    XP), used for players outside the loaded boards. No ranks: unknown."""
    xp = {s: xp_int((xp_table or {}).get(s, 0)) for s in SKILLS}
    return {"name": name, "xp": xp, "ranks": {}, "is_demo": bool(is_demo)}


def overall_rows_with_levels(rows: Sequence[Dict[str, Any]],
                             index: Dict[str, Dict[str, Any]],
                             thresholds: Sequence[int]
                             ) -> List[Dict[str, Any]]:
    """Attach `level` to each row of a board: the skill level on a skill
    board is added by the caller; on Overall it is the total level, known only
    once every skill board has loaded."""
    out = []
    for row in rows or []:
        entry = index.get(str(row.get("username", "")).casefold())
        lvl = total_level(entry["xp"], thresholds) if entry else None
        out.append(dict(row, level=lvl))
    return out


def rows_with_levels(board: str, rows: Sequence[Dict[str, Any]],
                     index: Dict[str, Dict[str, Any]],
                     thresholds: Sequence[int]) -> List[Dict[str, Any]]:
    if board == OVERALL:
        return overall_rows_with_levels(rows, index, thresholds)
    return [dict(r, level=level_of(r, thresholds)) for r in (rows or [])]


def display_name(row_or_entry: Dict[str, Any]) -> str:
    """The name as shown to people: demo accounts are always labeled."""
    name = str(row_or_entry.get("username", row_or_entry.get("name", "?")))
    return f"{name}  [Demo]" if row_or_entry.get("is_demo") else name


def level_label(board: str, level: Optional[int]) -> str:
    if level is None:
        return "—"
    return f"Total {level}" if board == OVERALL else f"Lv {level}"
