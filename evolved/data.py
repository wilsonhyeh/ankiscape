# evolved/data.py - Load shared/rules-v1.json (no Anki/Qt deps).
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict

_RULES_FILENAME = "rules-v1.json"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules_path() -> str:
    return os.path.join(_repo_root(), "shared", _RULES_FILENAME)


@lru_cache(maxsize=2)
def load_rules(path: str = "") -> Dict[str, Any]:
    p = path or rules_path()
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    _validate(data)
    return data


def _validate(data: Dict[str, Any]) -> None:
    assert data.get("protocol_version") == 1, "protocol_version must be 1"
    assert data.get("rules_version") == 1, "rules_version must be 1"
    assert isinstance(data.get("thresholds"), list) and len(data["thresholds"]) == 99
    assert len(data.get("ores", [])) == 11, "ores must define tiers 1..11"
    assert len(data.get("trees", [])) == 9, "trees must define tiers 1..9"
    assert len(data.get("fish", [])) == 9, "fish table must have 9 rows"
    # Explicit frozen tiers, 1-based and contiguous per group.
    for key, expect in (("ores", 11), ("trees", 9), ("fish", 9), ("bars", 8)):
        tiers = sorted(item["tier"] for item in data[key])
        assert tiers == list(range(1, expect + 1)), f"{key} tiers must be 1..{expect}"
    ctiers = sorted(item["tier"] for item in data["crafting"])
    assert ctiers == list(range(1, len(ctiers) + 1)), "crafting tiers frozen 1..N"
    # Soft clay must grant XP (contract E fixes legacy zero).
    soft = next(i for i in data["crafting"] if i["display"] == "Soft clay")
    assert soft["base_xp"] == 1, "Soft clay base XP must be 1"
    # Cooked shrimp must be reachable at level 1 (review finding P1-3).
    shrimp = next(f for f in data["fish"] if f["id"] == "shrimp")
    assert shrimp["cooking_level"] == 1 and shrimp["fishing_level"] == 1


def by_display(items, display: str):
    for item in items:
        if item.get("display") == display:
            return item
    return None
