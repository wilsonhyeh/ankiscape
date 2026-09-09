# evolved/ui/bank.py - Read-only Bank (quantities + simple filters).
from __future__ import annotations

from typing import Dict, List


def filter_inventory(inventory: Dict[str, int], query: str = "",
                     skill: str = "") -> List[tuple]:
    """Pure helper: filter (name, qty) rows by substring + skill group."""
    from . import SKILLS  # noqa: F401 (documents six-skill scope)

    groups = {
        "mining": ("ore", "essence", "clay", "coal", "gem", "uncut"),
        "woodcutting": ("log", "tree", "oak", "willow", "teak", "maple",
                        "mahogany", "yew", "magic", "redwood"),
        "smithing": ("bar",),
        "crafting": ("pot", "dish", "bowl", "ring", "necklace", "symbol",
                     "sapphire", "emerald", "ruby", "diamond", "tiara"),
        "fishing": ("shrimp", "sardine", "trout", "tuna", "lobster",
                    "swordfish", "monkfish", "shark", "anglerfish", "fish"),
        "cooking": ("cooked",),
    }
    q = (query or "").strip().casefold()
    rows = [(k, int(v)) for k, v in inventory.items() if int(v) != 0]
    if skill in groups:
        needles = groups[skill]
        rows = [r for r in rows if any(n in r[0].casefold() for n in needles)]
    if q:
        rows = [r for r in rows if q in r[0].casefold()]
    return sorted(rows, key=lambda r: r[0].casefold())
