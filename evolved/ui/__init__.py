# evolved/ui package - Six-skill menu, Bank, HUD, sync status (contract: interface).
"""Split into focused Qt components (never another 2,000-line module).
Tabs: Skills, Bank, Achievements, Hiscores, Settings. All widgets carry
explicit object names for the dev test-driver. Qt imports are lazy so pure
tests run without Anki/Qt.
"""
from __future__ import annotations

TABS = ("Skills", "Bank", "Achievements", "Hiscores", "Settings")
SKILLS = ("mining", "woodcutting", "smithing", "crafting", "fishing", "cooking")

OBJECT_NAMES = {
    "menu": "ankiscape-evolved-menu",
    "skills_tab": "ankiscape-tab-skills",
    "bank_tab": "ankiscape-tab-bank",
    "achievements_tab": "ankiscape-tab-achievements",
    "hiscores_tab": "ankiscape-tab-hiscores",
    "settings_tab": "ankiscape-tab-settings",
    "hud": "ankiscape-evolved-hud",
    "sync_button": "ankiscape-sync-button",
    "preset_combo": "ankiscape-preset-combo",
    "mode_switch": "ankiscape-mode-switch",
}
