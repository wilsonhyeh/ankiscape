# evolved/ui/onboarding.py - Guided first-run screen (resumable).
"""Welcome -> gathering skill -> level-1 resource -> how rewards work ->
Start studying. Every transition is persisted, so closing setup resumes at the
same step; no rewards accrue until setup completes."""
from __future__ import annotations

from typing import Any, Dict

from .menu_model import SKILL_LABELS


def build_onboarding_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    from . import OBJECT_NAMES
    from .theme import DEFAULT_SCALE
    from .widgets import (StonePanel, body_label, display_label, error_label,
                          icon_pixmap, muted_label)
    from ..assets import display_icon, skill_icon_path
    from ..onboarding import STEPS, level_one_resource, step_number

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["onboarding_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    progress = muted_label("")
    progress.setObjectName("ankiscape-onboarding-progress")
    layout.addWidget(progress)

    panel = StonePanel()
    icon = QLabel()
    icon.setFixedSize(48, 48)
    icon.setAlignment(_center())
    panel.body.addWidget(icon)
    title = display_label("")
    title.setObjectName("ankiscape-onboarding-title")
    panel.body.addWidget(title)
    body = body_label("", wrap=True)
    body.setObjectName("ankiscape-onboarding-body")
    panel.body.addWidget(body)
    choices = QHBoxLayout()
    panel.body.addLayout(choices)
    error = error_label("")
    error.setVisible(False)
    panel.body.addWidget(error)
    layout.addWidget(panel)

    nav = QHBoxLayout()
    back_btn = QPushButton("Back")
    back_btn.setObjectName(OBJECT_NAMES["onboarding_back"])
    primary = QPushButton("Get started")
    primary.setObjectName(OBJECT_NAMES["onboarding_primary"])
    try:
        primary.setStyleSheet(
            "QPushButton { border-color: #E3BE68; color: #E3BE68; font-weight: bold; }")
    except Exception:
        pass
    nav.addWidget(back_btn)
    classic = QPushButton("Continue Classic")
    classic.setObjectName("ankiscape-onboarding-classic")
    classic.clicked.connect(lambda: shell.call("on_mode_switch", "classic"))
    nav.addWidget(classic)
    nav.addStretch(1)
    nav.addWidget(primary)
    layout.addLayout(nav)
    layout.addStretch(1)

    state: Dict[str, Any] = {"draft": None, "step": "welcome"}

    def _draft() -> Dict[str, Any]:
        try:
            draft = shell.call("get_onboarding", default={}) or {}
        except Exception:
            draft = {}
        state["draft"] = draft
        state["step"] = str(draft.get("step", "welcome"))
        return draft

    def _persist(**fields):
        draft = dict(state["draft"] or {})
        draft.update(fields)
        shell.call("save_onboarding", draft)

    def _refresh():
        draft = _draft()
        step = state["step"]
        current, total = step_number(_from_draft(draft))
        progress.setText(f"Setup step {current} of {total}")
        for child in _choice_widgets(choices):
            choices.removeWidget(child)
            child.hide()
            child.deleteLater()
        error.setVisible(False)
        primary.setVisible(True)
        primary.setEnabled(True)
        rules = shell.call("get_rules", default={}) or {}

        if step == "welcome":
            _set_icon(None)
            title.setText("Welcome to AnkiScape Evolved")
            body.setText(
                "Anki stays your study app — the game rides along. Every "
                "Hard, Good, or Easy review attempts training in one of six skills and can produce "
                "items. This short setup picks where to start. An account is "
                "optional and never required to play.")
            primary.setText("Get started")
            primary.setEnabled(True)
            back_btn.setVisible(False)
        elif step == "skill":
            _set_icon(None)
            title.setText("Choose a gathering skill to start")
            body.setText(
                "Gathering skills produce items; production skills (Smithing, "
                "Crafting, Cooking) turn them into things. You can change this "
                "at any time.")
            for skill in ("mining", "woodcutting", "fishing"):
                choices.addWidget(_skill_button(skill))
            primary.setVisible(False)
            back_btn.setVisible(True)
        elif step == "resource":
            skill = str(draft.get("gathering_skill", "") or "mining")
            resource = str(draft.get("starting_resource", "")
                           or level_one_resource(rules, skill))
            state["draft"]["starting_resource"] = resource
            _set_icon(display_icon(resource))
            title.setText(f"Start with {resource}")
            body.setText(
                f"{resource} is the level-1 {SKILL_LABELS.get(skill, skill).lower()} "
                "resource. Reviews that you rate Hard, Good, or Easy earn about "
                f"{_base_xp_for(rules, skill, resource)} XP and one item when "
                "they succeed; Again earns nothing.")
            primary.setVisible(True)
            primary.setText("Continue")
            back_btn.setVisible(True)
        elif step == "explain":
            skill = str(draft.get("gathering_skill", "") or "mining")
            resource = str(draft.get("starting_resource", "") or "")
            _set_icon(display_icon(resource) if resource
                      else skill_icon_path(skill))
            title.setText("How reviews earn rewards")
            body.setText(
                "• Hard, Good, and Easy earn XP and a chance at items.\n"
                "• Again (rating 1) earns nothing.\n"
                "• Failed gathering attempts still earn a little XP; burned "
                "food does too.\n"
                "• Production (Smithing, Crafting, Cooking) pauses with zero "
                "reward when materials run out — you will see exactly what "
                "is missing.\n"
                "• Progress is saved on this computer immediately.")
            primary.setVisible(True)
            primary.setText("Start studying")
            back_btn.setVisible(True)
        else:
            # 'done' should never render: the shell leaves onboarding.
            title.setText("Setup complete")
            body.setText("Your training is ready.")
            primary.setText("Start studying")
            primary.setVisible(True)

    def _choice_widgets(layout):
        out = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None:
                out.append(widget)
        return out

    def _skill_button(skill: str):
        from aqt.qt import QPushButton
        button = QPushButton(SKILL_LABELS.get(skill, skill.title()))
        button.setObjectName(f"ankiscape-onboarding-skill-{skill}")
        pix = icon_pixmap(skill_icon_path(skill), 24)
        if pix is not None:
            try:
                from aqt.qt import QIcon
                button.setIcon(QIcon(pix))
            except Exception:
                pass
        button.clicked.connect(lambda _c=False, s=skill: _choose_skill(s))
        button.setToolTip(f"Start with {SKILL_LABELS.get(skill, skill)}")
        button.setAccessibleName(f"Choose {SKILL_LABELS.get(skill, skill)}")
        return button

    def _choose_skill(skill: str):
        rules = shell.call("get_rules", default={}) or {}
        resource = level_one_resource(rules, skill)
        _persist(step="skill", gathering_skill=skill,
                 starting_resource=resource)
        # Persist then advance locally through the pure model.
        shell.call("advance_onboarding")
        _refresh()

    def _primary():
        step = state["step"]
        if step == "welcome":
            shell.call("advance_onboarding")
            _refresh()
            return
        if step == "resource":
            # Explicit confirmation of the starting resource.
            shell.call("advance_onboarding")
            _refresh()
            return
        if step == "explain":
            draft = state["draft"] or {}
            result = shell.call(
                "commit_onboarding",
                draft.get("gathering_skill", ""),
                draft.get("starting_resource", ""), default={})
            if isinstance(result, dict) and not result.get("ok", True):
                error.setText(str(result.get("error", "Could not finish setup.")))
                error.setVisible(True)
                return
            shell.call("on_start_studying")
            return

    def _back():
        shell.call("back_onboarding")
        _refresh()

    primary.clicked.connect(_primary)
    back_btn.clicked.connect(_back)

    def _set_icon(path):
        if path:
            pix = icon_pixmap(path, 48)
            if pix is not None:
                icon.setPixmap(pix)
                return
        icon.setText("")

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _from_draft(draft: Dict[str, Any]):
    from types import SimpleNamespace
    return SimpleNamespace(step=str(draft.get("step", "welcome")),
                           gathering_skill=str(draft.get("gathering_skill", "")),
                           starting_resource=str(draft.get("starting_resource", "")),
                           complete=bool(draft.get("complete", False)))


def _base_xp_for(rules: Dict[str, Any], skill: str, resource: str) -> str:
    table = {"mining": (rules.get("ores", []), "base_xp"),
             "woodcutting": (rules.get("trees", []), "base_xp"),
             "fishing": (rules.get("fish", []), "fishing_base_xp")}.get(skill, ([], "base_xp"))
    entries, xp_key = table
    for entry in entries:
        if str(entry.get("display", "")) == resource:
            return str(entry.get(xp_key, entry.get("base_xp", "?")))
    return "?"


def _center():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignCenter
    except Exception:
        return 0
