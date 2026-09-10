# evolved/ui/training.py - Training Home: current training at a glance.
"""Leads with the selected skill/resource, level progress, reward
expectations, material readiness, and pause state. Change training routes to
the Skills screen; Train commits there. No automatic tier switching.
"""
from __future__ import annotations

from typing import Any, Dict


def build_training_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    from . import OBJECT_NAMES
    from .menu_model import training_home
    from .theme import scaled, DEFAULT_SCALE
    from .widgets import (StonePanel, XpBar, body_label, display_label,
                          error_label, icon_pixmap, muted_label, success_label)
    from ..assets import display_icon

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["training_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    # --- Current training panel ---
    panel = StonePanel()
    top = QHBoxLayout()
    icon = QLabel()
    icon.setFixedSize(48, 48)
    icon.setAlignment(_center())
    top.addWidget(icon)
    titles = QVBoxLayout()
    title = display_label("")
    title.setObjectName("ankiscape-training-title")
    subtitle = muted_label("")
    subtitle.setObjectName("ankiscape-training-subtitle")
    titles.addWidget(title)
    titles.addWidget(subtitle)
    top.addLayout(titles, 1)
    change = QPushButton("Change training")
    change.setObjectName(OBJECT_NAMES["change_training"])
    change.setToolTip("Open the skill and resource picker")
    change.clicked.connect(lambda: shell.set_section("skills"))
    top.addWidget(change)
    panel.body.addLayout(top)

    xp = XpBar(panel)
    panel.body.addWidget(xp)
    xp_line = body_label("")
    xp_line.setObjectName("ankiscape-training-xp")
    panel.body.addWidget(xp_line)
    layout.addWidget(panel)

    # --- Reward expectations ---
    rewards = StonePanel()
    rewards.body.addWidget(display_label("What a review earns"))
    reward_line = body_label("", wrap=True)
    reward_line.setObjectName("ankiscape-training-rewards")
    rewards.body.addWidget(reward_line)
    material_line = body_label("", wrap=True)
    material_line.setObjectName("ankiscape-training-materials")
    rewards.body.addWidget(material_line)
    pause_line = error_label("")
    pause_line.setObjectName("ankiscape-training-pause")
    pause_line.setVisible(False)
    rewards.body.addWidget(pause_line)
    shortcut_row = QHBoxLayout()
    gather_btn = QPushButton("Gather missing materials")
    gather_btn.setObjectName("ankiscape-gather-shortcut")
    gather_btn.setVisible(False)
    shortcut_row.addWidget(gather_btn)
    shortcut_row.addStretch(1)
    rewards.body.addLayout(shortcut_row)
    layout.addWidget(rewards)

    # --- Unlock notice + session recap ---
    unlock = StonePanel(raised=True)
    unlock.setVisible(False)
    unlock_label = body_label("", wrap=True)
    unlock_label.setObjectName("ankiscape-unlock-notice")
    unlock.body.addWidget(unlock_label)
    unlock_row = QHBoxLayout()
    preview_btn = QPushButton("Preview")
    preview_btn.setObjectName("ankiscape-unlock-preview")
    dismiss_btn = QPushButton("Got it")
    dismiss_btn.setObjectName("ankiscape-unlock-dismiss")
    unlock_row.addWidget(preview_btn)
    unlock_row.addWidget(dismiss_btn)
    unlock_row.addStretch(1)
    unlock.body.addLayout(unlock_row)
    layout.addWidget(unlock)

    recap = StonePanel()
    recap.setVisible(False)
    recap_label = body_label("", wrap=True)
    recap_label.setObjectName("ankiscape-training-recap")
    recap.body.addWidget(display_label("Last study session"))
    recap.body.addWidget(recap_label)
    layout.addWidget(recap)

    # --- Return to Anki ---
    row = QHBoxLayout()
    return_btn = QPushButton("Return to Anki")
    return_btn.setObjectName(OBJECT_NAMES["return_to_anki"])
    return_btn.setToolTip("Hide this window and continue studying")
    return_btn.clicked.connect(lambda: shell.call("on_return_to_anki"))
    row.addWidget(return_btn)
    row.addStretch(1)
    layout.addLayout(row)
    layout.addStretch(1)

    state: Dict[str, Any] = {"home": None, "skill": "mining"}

    def _set_icon(path: str, size: int = 48):
        pix = icon_pixmap(path, size)
        if pix is not None:
            icon.setPixmap(pix)
        else:
            icon.setText("")

    def _refresh():
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        skill = str(shell.call("get_active_skill", default="mining") or "mining")
        selections = shell.call("get_selections", default={}) or {}
        home = training_home(rules, projection, selections, skill)
        state["home"] = home
        state["skill"] = home["skill"]
        scaled_size = scaled(48, DEFAULT_SCALE)
        _set_icon(display_icon(home["resource"]), scaled_size)
        title.setText(f"{home['skill_label']} — {home['resource']}")
        subtitle.setText(f"Level {home['level']}")
        thresholds = rules.get("thresholds", [])
        xp.set_xp(home["xp_micro"], thresholds, home["level"])
        if home["max_level"]:
            xp_line.setText("Level 99 — every review still earns XP for the score.")
        else:
            xp_line.setText(f"{home['xp_to_next']} XP to level {home['level'] + 1}")

        if home["gather"]:
            earn = (f"Success earns about {home['expected_success_xp']} XP and one "
                    f"{home['resource']}; a failed attempt earns 25% XP.")
        else:
            earn = (f"A successful {home['skill_label'].lower()} review earns about "
                    f"{home['expected_success_xp']} XP and one {home['resource']}.")
            earn += f" A {home['expected_fail_label'].lower()} earns no output."
        reward_line.setText(earn)

        if home["gather"]:
            material_line.setText("No materials needed — gathering produces items.")
        elif home["needs"]:
            parts = [f"{qty}× {name} (have {home['have'].get(name, 0)})"
                     for name, qty in home["needs"].items()]
            material_line.setText("Materials per review: " + ", ".join(parts))
        else:
            material_line.setText("No materials needed for this recipe.")

        if home["paused"]:
            if home["pause_reason"] == "materials":
                pause_line.setText(
                    "Paused: not enough materials. Reviews still count for Anki, "
                    "but earn no XP or items until you gather "
                    f"{home['missing_text']}.")
                gather_btn.setText("Gather missing materials")
                gather_btn.setVisible(True)
            else:
                pause_line.setText(
                    f"Paused: this resource needs level {home['level_requirement']} "
                    f"{home['skill_label']} and you are level {home['level']}. "
                    "Reviews earn nothing until it is valid again.")
                gather_btn.setVisible(False)
            pause_line.setVisible(True)
        else:
            pause_line.setVisible(False)
            gather_btn.setVisible(False)

        notice = shell.call("get_unlock_notice", default=None)
        if notice:
            unlock_label.setText(
                f"Unlocked: {notice.get('skill_label', '').title()} "
                f"{notice.get('display', '')} (level {notice.get('level', '?')}). "
                "Your training did not change — preview it to switch.")
            unlock.setVisible(True)
        else:
            unlock.setVisible(False)

        summary = shell.call("get_session_recap", default=None)
        if summary and summary.get("text"):
            recap_label.setText(summary["text"])
            recap.setVisible(True)
        else:
            recap.setVisible(False)

    def _preview_unlock():
        notice = shell.call("get_unlock_notice", default=None) or {}
        shell.call("preselect_skill", notice.get("skill", state["skill"]))
        shell.call("preselect_resource", notice.get("display", ""))
        shell.set_section("skills")

    def _dismiss_unlock():
        shell.call("dismiss_unlock_notice")
        _refresh()

    def _gather():
        home = state["home"] or {}
        wants = _gathering_for(home.get("skill", ""))
        shell.call("preselect_skill", wants)
        shell.set_section("skills")

    preview_btn.clicked.connect(_preview_unlock)
    dismiss_btn.clicked.connect(_dismiss_unlock)
    gather_btn.clicked.connect(_gather)

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _gathering_for(skill: str) -> str:
    skill = str(skill or "").lower()
    if skill in ("mining", "smithing", "crafting"):
        return "mining"
    if skill == "woodcutting":
        return "woodcutting"
    return "fishing"


def _center():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignCenter
    except Exception:
        return 0
