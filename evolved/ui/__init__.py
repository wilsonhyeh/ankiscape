# evolved/ui package - Evolved shell screens, Bank, HUD, sync status.
"""One nonmodal shell (evolved/ui/shell.py) with an icon rail and screens.
All widgets carry explicit object names for the dev test-driver. Qt imports
are lazy so pure tests run without Anki/Qt.
"""
from __future__ import annotations

TABS = ("Training", "Skills", "Bank", "Achievements", "Hiscores", "Settings")
SKILLS = ("mining", "woodcutting", "smithing", "crafting", "fishing", "cooking")

OBJECT_NAMES = {
    # Shell + screens
    "shell": "ankiscape-evolved-shell",
    "shell_content": "ankiscape-shell-content",
    "rail_button": "ankiscape-rail-button",
    "training_screen": "ankiscape-screen-training",
    "skills_screen": "ankiscape-screen-skills",
    "bank_screen": "ankiscape-screen-bank",
    "achievements_screen": "ankiscape-screen-achievements",
    "hiscores_screen": "ankiscape-screen-hiscores",
    "settings_screen": "ankiscape-screen-settings",
    "onboarding_screen": "ankiscape-screen-onboarding",
    "recap": "ankiscape-recap",
    # Legacy menu names retained so nothing silently loses a hook point.
    "menu": "ankiscape-evolved-shell",
    "skills_tab": "ankiscape-tab-skills",
    "bank_tab": "ankiscape-tab-bank",
    "achievements_tab": "ankiscape-tab-achievements",
    "hiscores_tab": "ankiscape-tab-hiscores",
    "settings_tab": "ankiscape-tab-settings",
    "hud": "ankiscape-evolved-hud",
    "sync_button": "ankiscape-sync-button",
    "preset_combo": "ankiscape-preset-combo",
    "mode_switch": "ankiscape-mode-switch",
    # Training + onboarding controls
    "train_button": "ankiscape-train-button",
    "change_training": "ankiscape-change-training",
    "return_to_anki": "ankiscape-return-to-anki",
    "resource_grid": "ankiscape-resource-grid",
    "resource_detail": "ankiscape-resource-detail",
    "skill_grid": "ankiscape-skill-grid",
    "bank_grid": "ankiscape-bank-grid",
    "bank_filter": "ankiscape-bank-filter",
    "bank_skill_filter": "ankiscape-bank-skill-filter",
    "bank_detail": "ankiscape-bank-detail",
    "achievement_list": "ankiscape-achievement-list",
    "hiscores_list": "ankiscape-hiscores-list",
    "hiscores_status": "ankiscape-hiscores-status",
    "hiscores_test_toggle": "ankiscape-hiscores-test-toggle",
    "hiscores_skill": "ankiscape-hiscores-skill",
    "hiscores_lookup": "ankiscape-hiscores-lookup",
    "hiscores_refresh": "ankiscape-hiscores-refresh",
    "onboarding_primary": "ankiscape-onboarding-primary",
    "onboarding_back": "ankiscape-onboarding-back",
    "onboarding_skill": "ankiscape-onboarding-skill",
    "onboarding_resource": "ankiscape-onboarding-resource",
    "upgrade_try": "ankiscape-upgrade-try",
    "upgrade_classic": "ankiscape-upgrade-classic",
}
