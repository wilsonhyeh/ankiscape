# evolved/logic_pure.py - Deterministic six-skill economy (contract E).
"""Integer micro-XP (1 XP = 1,000,000). SQL uses bigint; JSON transports large
counters as validated decimal strings. Round half-up after rational factors;
Python round() is NOT the contract. Levels derive from the 99-threshold list
scaled to micro-XP, clamped to 99.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Optional, Tuple

MICRO = 1_000_000
INT64_MAX = (1 << 63) - 1

SKILLS = ("mining", "woodcutting", "smithing", "crafting", "fishing", "cooking")


def base_xp_to_micro(base_xp) -> int:
    from decimal import Decimal, InvalidOperation

    try:
        d = Decimal(str(base_xp))
    except (InvalidOperation, ValueError):
        raise ValueError(f"bad base_xp {base_xp!r}")
    if not d.is_finite() or d < 0:
        raise ValueError("base_xp must be finite >= 0")
    micro = int((d * MICRO).to_integral_value(rounding="ROUND_HALF_UP"))
    if micro > INT64_MAX:
        raise OverflowError("base_xp exceeds int64 micro range")
    return micro


def tier_multiplier(tier: int, cap_num: int = 2, cap_den: int = 1) -> Fraction:
    if tier < 1:
        raise ValueError("tier >= 1")
    num = 100 + 5 * (tier - 1)
    frac = Fraction(num, 100)
    cap = Fraction(cap_num, cap_den)
    return min(frac, cap)


def mul_half_up(micro: int, frac: Fraction) -> int:
    """Round half-up of micro * frac for non-negative micro."""
    if micro < 0:
        raise ValueError("micro must be >= 0")
    num, den = frac.numerator, frac.denominator
    # floor(micro*num/den + 1/2) for positives
    return (2 * micro * num + den) // (2 * den)


def multiplied_base_micro(base_xp, tier: int) -> int:
    return mul_half_up(base_xp_to_micro(base_xp), tier_multiplier(tier))


def quarter_half_up(micro: int) -> int:
    return mul_half_up(micro, Fraction(1, 4))


def level_from_xp_micro(xp_micro: int, thresholds) -> int:
    if xp_micro < 0:
        return 1
    level = 1
    # thresholds[i] is cumulative XP for level i+1
    for idx in range(1, min(len(thresholds), 99)):
        if xp_micro >= int(thresholds[idx]) * MICRO:
            level = idx + 1
        else:
            break
    return max(1, min(99, level))


def gathering_probability(player_level: int, resource_probability) -> Fraction:
    base = Fraction(80, 100) + Fraction(2, 100) * int(player_level)
    capped = min(base, Fraction(95, 100))
    return capped * Fraction(str(resource_probability))


def burn_probability(player_level: int) -> Fraction:
    num = 300 - 2 * int(player_level)
    if num <= 0:
        return Fraction(0, 1)
    return Fraction(num, 1000)


@dataclass(frozen=True)
class ActionResult:
    """Immutable action result: XP independent from item success (P1-5)."""

    skill: str
    xp_micro: int
    xp_display: str
    item_out: Optional[str]
    item_qty: int
    consumed: Dict[str, int]
    success: bool
    outcome: str  # success|fail_gather|burn|practice|invalid_production
    counters: Dict[str, int]


def _xp_str(micro: int) -> str:
    # Transport large counters as decimal strings; display helper keeps exact value.
    return str(int(micro))


def eval_gather(*, skill: str, resource_display: str, resource_tier: int, base_xp,
                player_level: int, success: bool, gem: Optional[Dict] = None,
                gem_ore_mult_tier: Optional[int] = None) -> ActionResult:
    """Gathering success awards full multiplied XP + item; failure awards 25%
    (min 1 XP). Gem XP receives the selected ore multiplier too."""
    mult = multiplied_base_micro(base_xp, resource_tier)
    if success:
        total = mult
        counters = {"successful_actions": 1}
        if gem is not None:
            gem_tier = gem_ore_mult_tier if gem_ore_mult_tier is not None else resource_tier
            total += multiplied_base_micro(gem["base_xp"], gem_tier)
            counters["gems"] = 1
        if total > INT64_MAX:
            raise OverflowError("xp exceeds int64")
        return ActionResult(skill, total, _xp_str(total), resource_display, 1, {}, True, "success", counters)
    fail = max(quarter_half_up(mult), MICRO)
    return ActionResult(skill, fail, _xp_str(fail), None, 0, {}, False, "fail_gather", {})


def eval_cook(*, fish_display: str, fish_tier: int, cooking_base_xp, player_level: int,
              burned: bool, has_fish: bool) -> ActionResult:
    """Burn consumes one fish, grants 25% of multiplied XP, no output. Burn is
    not a material failure. No materials gives 1 XP practice."""
    if not has_fish:
        return ActionResult("cooking", MICRO, _xp_str(MICRO), None, 0, {}, False, "practice", {})
    mult = multiplied_base_micro(cooking_base_xp, fish_tier)
    if burned:
        xp = max(quarter_half_up(mult), MICRO)
        return ActionResult("cooking", xp, _xp_str(xp), None, 0, {fish_display: 1}, False, "burn",
                            {"cooking_attempts": 1})
    return ActionResult("cooking", mult, _xp_str(mult), f"Cooked {fish_display}", 1,
                        {fish_display: 1}, True, "success",
                        {"successful_actions": 1, "cooking_attempts": 1, "successful_cooks": 1})


def eval_production(*, skill: str, output_display: str, output_tier: int, base_xp,
                    level_ok: bool, materials_ok: bool,
                    requirements: Dict[str, int]) -> ActionResult:
    """Invalidated production consumes nothing, produces nothing, 1 XP practice."""
    if not (level_ok and materials_ok):
        return ActionResult(skill, MICRO, _xp_str(MICRO), None, 0, {}, False, "practice", {})
    mult = multiplied_base_micro(base_xp, output_tier)
    return ActionResult(skill, mult, _xp_str(mult), output_display, 1, dict(requirements),
                        True, "success", {"successful_actions": 1})
