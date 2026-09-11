# evolved/assets.py - Display-name -> bundled asset mapping (pure, no Qt).
"""One mapping for every screen: skill tiles, item slots, navigation icons,
textures, fonts and sounds. Missing files resolve to the single shipped
fallback from evolved.icons (never a crash, never a runtime download, never
a runtime file write). Asset provenance/license lives in assets/manifest.json.
"""
from __future__ import annotations

import os
from typing import Optional

from .icons import icon_path, repo_root, resolve_icon

SKILL_ICONS = {
    "mining": "icon/mining_icon.png",
    "woodcutting": "icon/woodcutting_icon.png",
    "smithing": "icon/smithing_icon.png",
    "crafting": "icon/crafting_icon.png",
    "fishing": "icon/fishing_icon.png",
    "cooking": "icon/cooking_icon.png",
}

NAV_ICONS = {
    "training": "icon/training_icon.png",
    "skills": "icon/skills_icon.png",
    "bank": "icon/bank_icon.png",
    "achievements": "icon/achievements_icon.png",
    "hiscores": "icon/hiscores_icon.png",
    "guide": "icon/guide_icon.png",
    "settings": "icon/settings_icon.png",
}

ORE_FILES = {
    "Rune essence": "RuneEssence.png", "Clay": "Clay.png",
    "Copper ore": "Copper.png", "Tin ore": "Tin.png", "Iron ore": "Iron.png",
    "Silver ore": "Silver.png", "Coal": "Coal.png", "Gold ore": "Gold.png",
    "Mithril ore": "Mithril.png", "Adamantite ore": "Adamantite.png",
    "Runite ore": "Runite.png",
}
TREE_FILES = {name: f"{name}.png" for name in
              ("Tree", "Oak", "Willow", "Teak", "Maple", "Mahogany",
               "Yew", "Magic", "Redwood")}
BAR_FILES = {
    "Bronze bar": "bronzebar.png", "Iron bar": "ironbar.png",
    "Silver bar": "silverbar.png", "Steel bar": "steelbar.png",
    "Gold bar": "goldbar.png", "Mithril bar": "mithrilbar.png",
    "Adamantite bar": "adamantitebar.png", "Runite bar": "runitebar.png",
}
GEM_FILES = {
    "Uncut sapphire": "sapphire.png", "Uncut emerald": "emerald.png",
    "Uncut ruby": "ruby.png", "Uncut diamond": "diamond.png",
}
CRAFT_FILES = {
    "Soft clay": "Soft_clay.png", "Unfired pot": "Unfired_pot.png",
    "Pot": "Pot.png", "Pie dish": "Pie_dish.png", "Bowl": "Bowl.png",
    "Unfired pie dish": "Unfired_pie_dish.png",
    "Unfired bowl": "Unfired_bowl.png",
    "Gold ring": "Gold_ring.png", "Gold necklace": "Gold_necklace.png",
    "Unstrung symbol": "Unstrung_symbol.png",
    "Sapphire": "Sapphire.png", "Sapphire ring": "Sapphire_ring.png",
    "Sapphire necklace": "Sapphire_necklace.png", "Tiara": "Tiara.png",
    "Emerald": "Emerald.png", "Emerald ring": "Emerald_ring.png",
    "Emerald necklace": "Emerald_necklace.png", "Ruby": "Ruby.png",
    "Ruby ring": "Ruby_ring.png", "Ruby necklace": "Ruby_necklace.png",
    "Diamond": "Diamond.png", "Diamond ring": "Diamond_ring.png",
    "Diamond necklace": "Diamond_necklace.png",
}
FISH_FILES = {
    "Shrimp": "shrimp.png", "Sardine": "sardine.png", "Trout": "trout.png",
    "Tuna": "tuna.png", "Lobster": "lobster.png", "Swordfish": "swordfish.png",
    "Monkfish": "monkfish.png", "Shark": "shark.png",
    "Anglerfish": "anglerfish.png",
}
COOKED_FISH_FILES = {f"Cooked {name}": f"cooked_{path}"
                     for name, path in FISH_FILES.items()}

ITEM_FILES = (ORE_FILES | TREE_FILES | BAR_FILES | GEM_FILES | CRAFT_FILES
              | FISH_FILES | COOKED_FISH_FILES)

# Reviewed icon-slot map: every logical slot the UI renders resolves through
# here. Static slots point at bundled art (original generated icons or audited
# Wiki art); dynamic slots resolve display/skill art through the existing
# mappings. Slots never invent unseen files and are never downloaded at
# runtime. Dynamic slots (documented, resolved by argument):
#   training.material, training.result, training.gather, unlock.resource,
#   recap.item, bank.item, achievement.skill
SLOT_ICONS = {
    "training.pause": "icon/pause_icon.png",
    "bank.empty": "icon/bank_icon.png",
    "achievement.first_catch": "icon/fishing_icon.png",
    "achievement.first_cook": "icon/cooking_icon.png",
    "achievement.cooks": "icon/cooking_icon.png",
    "hiscores.rank": "icon/hiscores_icon.png",
    "hiscores.account": "icon/settings_icon.png",
    "hiscores.pending": "icon/pending_icon.png",
    "hiscores.offline": "icon/offline_icon.png",
    "support.report": "icon/report_icon.png",
}

_MANIFEST_REVISION = {"stamp": None, "hash": ""}


def manifest_revision() -> str:
    """Content hash of assets/manifest.json, cached by (mtime, size).

    The icon cache keys include this: an asset audit/replace invalidates
    every decoded pixmap without any UI event."""
    import hashlib
    import json

    path = os.path.join(repo_root(), "assets", "manifest.json")
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
        if _MANIFEST_REVISION["stamp"] == stamp:
            return str(_MANIFEST_REVISION["hash"])
        with open(path, "rb") as fh:
            raw = fh.read()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        _MANIFEST_REVISION.update({"stamp": stamp, "hash": digest})
        return digest
    except OSError:
        return "unknown"


def slot_icon_path(slot: str, *, skill: str = "", display: str = "") -> str:
    """Resolve one reviewed icon slot to an existing bundled asset.

    Unknown slots and missing files fall back to the single shipped fallback
    (never a crash, never a runtime download)."""
    key = str(slot)
    if key in SLOT_ICONS:
        rel = SLOT_ICONS[key]
        return icon_path(rel) if _exists(rel) else resolve_icon(
            kind="slot", display=key, mapping={})
    if key == "training.gather" or key == "achievement.skill":
        return skill_icon_path(skill) if skill else resolve_icon(
            kind="slot", display=key, mapping={})
    if key in ("training.material", "training.result", "unlock.resource",
               "recap.item", "bank.item"):
        return display_icon(display) if display else resolve_icon(
            kind="slot", display=key, mapping={})
    return resolve_icon(kind="slot", display=key, mapping={})


def achievement_icon_path(achievement_id: str) -> Optional[str]:
    """Explicit icon per achievement category/id.

    Skill gates use their skill icon; fishing/cooking achievements use their
    own art. Unknown ids return None so the UI hides the icon instead of
    showing an unrelated (cooking) fallback."""
    aid = str(achievement_id or "")
    if aid.startswith("skill_"):
        parts = aid.split("_")
        if len(parts) == 3 and parts[2] in SKILL_ICONS:
            return skill_icon_path(parts[2])
        return None
    if aid == "first_catch":
        return slot_icon_path("achievement.first_catch")
    if aid in ("first_cook", "cooks_100", "cooks_1000"):
        return slot_icon_path("achievement.cooks")
    return None


def _exists(rel: str) -> bool:
    return os.path.isfile(os.path.join(repo_root(), rel))


def display_icon(display: str) -> str:
    """Existing icon path for an item display name, else placeholder."""
    rel = ITEM_FILES.get(str(display), "")
    if rel:
        folder = _folder_for(rel)
        return resolve_icon(kind=folder, display=display,
                            mapping={display: icon_path(folder, rel)})
    return resolve_icon(kind="item", display=display, mapping={})


def _folder_for(rel: str) -> str:
    if rel.startswith("RuneEssence") or rel in ORE_FILES.values():
        return "ores"
    if rel in TREE_FILES.values():
        return "trees"
    if rel in BAR_FILES.values():
        return "bars"
    if rel in GEM_FILES.values():
        return "gems"
    if rel in FISH_FILES.values() or rel in COOKED_FISH_FILES.values():
        return "fish"
    return "crafteditems"


def skill_icon_path(skill: str) -> str:
    rel = SKILL_ICONS.get(str(skill).lower(), "")
    if rel and _exists(rel):
        return icon_path(rel)
    return icon_path("icon", "stats_icon.png")


def nav_icon_path(section: str) -> str:
    rel = NAV_ICONS.get(str(section).lower(), "")
    if rel and _exists(rel):
        return icon_path(rel)
    return icon_path("icon", "stats_icon.png")


def texture_path(name: str = "stone.png") -> Optional[str]:
    rel = os.path.join("textures", name)
    return icon_path(rel) if _exists(rel) else None


def font_path() -> Optional[str]:
    rel = os.path.join("fonts", "PressStart2P-Regular.ttf")
    return icon_path(rel) if _exists(rel) else None


def sound_path(name: str) -> Optional[str]:
    rel = os.path.join("sounds", f"{name}.wav")
    return icon_path(rel) if _exists(rel) else None


def icon_asset_path(rel: str) -> Optional[str]:
    """Resolve a repo-relative asset (used by the shell for raw files)."""
    return icon_path(rel) if _exists(rel) else None
