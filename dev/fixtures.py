# dev/fixtures.py - Deterministic synthetic scenario builders (dev only).
"""Builds journal operation histories + collection seeds for the isolated
playground. Same generators feed `dev.py launch --scenario X` and E2E
fixtures, so manual and automated play see identical worlds. Never touches
personal data; all output goes under .dev/.
"""
from __future__ import annotations

import random
from typing import Any, Dict, List

ORES = ["Rune essence", "Clay", "Copper ore", "Tin ore", "Iron ore",
        "Silver ore", "Coal", "Gold ore", "Mithril ore", "Adamantite ore",
        "Runite ore"]
TREES = ["Tree", "Oak", "Willow", "Teak", "Maple", "Mahogany", "Yew",
         "Magic", "Redwood"]
FISH = ["Shrimp", "Sardine", "Trout", "Tuna", "Lobster", "Swordfish",
        "Monkfish", "Shark", "Anglerfish"]
BARS = ["Bronze bar", "Iron bar", "Silver bar", "Steel bar", "Gold bar",
        "Mithril bar", "Adamantite bar", "Runite bar"]
CRAFTS = ["Soft clay", "Unfired pot", "Pot", "Gold ring", "Sapphire ring",
          "Emerald ring", "Ruby ring", "Diamond ring"]


def make_award(index: int, game_uuid: str, skill: str, resource: str,
               *, device: str = "dev-a", ts: int = 2000,
               provenance: str = "direct") -> Dict[str, Any]:
    return {"op_id": f"{index:08d}-0000-4000-8000-{index:012d}",
            "game_uuid": game_uuid, "device_id": device,
            "device_seq": index + 1, "lamport": index + 1,
            "kind": "review_award",
            "payload": {"review_key": f"rk-{index}", "review_ts": ts + index,
                        "rating": 3, "review_kind": "review",
                        "provenance": provenance, "skill": skill,
                        "resource": resource}}


def midgame_ops(game_uuid: str, seed: int = 7) -> List[Dict[str, Any]]:
    """~600 mixed awards: levels ~10-30, varied inventory, some failures."""
    rng = random.Random(seed)
    ops: List[Dict[str, Any]] = []
    plan = ([("mining", rng.choice(ORES)) for _ in range(120)]
            + [("woodcutting", rng.choice(TREES)) for _ in range(120)]
            + [("fishing", rng.choice(FISH)) for _ in range(120)]
            + [("cooking", rng.choice(FISH)) for _ in range(120)]
            + [("smithing", rng.choice(BARS)) for _ in range(60)]
            + [("crafting", rng.choice(CRAFTS)) for _ in range(60)])
    for i, (skill, resource) in enumerate(plan):
        ops.append(make_award(i, game_uuid, skill, resource))
    return ops


def endgame_ops(game_uuid: str, seed: int = 11) -> List[Dict[str, Any]]:
    """~35k awards: breadth rotation, then top-tier focus. Replay cost ~2 s.
    Woodcutting reaches Redwood territory; all gathering 40+; rich bank."""
    rng = random.Random(seed)
    picks: List[tuple] = []
    for _ in range(3000):
        picks.append(("mining", rng.choice(ORES)))
        picks.append(("woodcutting", rng.choice(TREES)))
        picks.append(("fishing", rng.choice(FISH)))
        picks.append(("cooking", rng.choice(FISH)))
    for _ in range(14000):
        picks.append(("woodcutting", rng.choice(["Redwood", "Magic", "Yew"])))
    for _ in range(3000):
        picks.append(("mining", rng.choice(["Runite ore", "Adamantite ore"])))
        picks.append(("fishing", rng.choice(["Anglerfish", "Shark"])))
        picks.append(("cooking", rng.choice(["Anglerfish", "Shark", "Monkfish"])))
    # Production tail: phase 1 banked the ingredients, so these smelts and
    # crafts mostly succeed instead of collapsing to practice XP. Weighted to
    # mid-tier recipes whose ingredients actually exist in the bank.
    for _ in range(600):
        picks.append(("smithing", rng.choice(
            ["Bronze bar", "Iron bar", "Steel bar", "Gold bar", "Mithril bar"])))
        picks.append(("crafting", rng.choice(
            ["Soft clay", "Unfired pot", "Pot", "Gold ring", "Gold necklace"])))
    return [make_award(i, game_uuid, skill, resource)
            for i, (skill, resource) in enumerate(picks)]


def classic_player_data() -> Dict[str, Any]:
    """2.0.2-shape Classic data (config_version 2) for the upgrade fixture.

    The stored levels deliberately LAG the stored exp in EVERY skill. Measured
    implied levels against `EXP_TABLE`:

        mining      23 / 50000  -> 42   (+19)
        woodcutting 17 / 20000  -> 33   (+16)
        smithing    12 /  8000  -> 25   (+13)
        crafting     9 /  5000  -> 20   (+11)

    That is not a typo in one field - it is the shape a real 2.0.2 profile
    arrives in, and it is the state that opened 26 stacked modal dialogs on the
    first award before 3.0.0 capped the level-up burst at one. Making these
    pairs self-consistent would quietly delete coverage of the upgrade path
    this fixture exists to exercise. The lag is asserted on purpose in
    tests/test_award_durability.py; do not "fix" it.
    """
    ores = {ore: 0 for ore in ORES}
    ores.update({"Rune essence": 320, "Clay": 140, "Copper ore": 90,
                 "Tin ore": 88, "Iron ore": 40, "Coal": 25})
    return {"config_version": 2, "mining_level": 23, "woodcutting_level": 17,
            "smithing_level": 12, "crafting_level": 9, "mining_exp": 50000,
            "woodcutting_exp": 20000, "smithing_exp": 8000,
            "crafting_exp": 5000, "current_craft": "", "current_ore": "Iron ore",
            "current_tree": "Oak", "current_bar": "Steel bar",
            "inventory": ores, "progress_to_next": 0,
            "completed_achievements": ["First Steps", "Novice Miner"]}
