# evolved/ui/skills.py - Skills grid + all-tier resource grid + Train commit.
"""Six skill tiles; opening one shows its complete resource grid (locked tiers
visible with required levels) and a detail panel. Preview never changes
training; Train commits skill+resource together, returns to Training Home.
"""
from __future__ import annotations

from typing import Any, Dict

from .menu_model import SKILL_LABELS, resource_entries, skill_level_requirement


def build_skills_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QComboBox, QGridLayout, QHBoxLayout, QLabel,
                        QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                        QWidget)
    from . import OBJECT_NAMES
    from .theme import ITEM_CELL, DEFAULT_SCALE, scaled
    from .widgets import (ItemSlot, SkillTile, StonePanel, body_label,
                          display_label, error_label, muted_label,
                          success_label)
    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["skills_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    tiles_panel = StonePanel()
    tiles_panel.body.addWidget(display_label("Choose a skill"))
    tiles_grid = QGridLayout()
    tiles_grid.setSpacing(6)
    state: Dict[str, Any] = {
        "skill": str(shell.call("get_active_skill", default="mining") or "mining"),
        "resource": "",
        "entries": [],
    }
    tiles: Dict[str, Any] = {}
    for index, skill in enumerate(("mining", "woodcutting", "smithing",
                                   "crafting", "fishing", "cooking")):
        tile = SkillTile(skill, scale=DEFAULT_SCALE)
        tile.clicked.connect(lambda _c=False, s=skill: _select_skill(s))
        tiles_grid.addWidget(tile, index // 3, index % 3)
        tiles[skill] = tile
    tiles_panel.body.addLayout(tiles_grid)
    layout.addWidget(tiles_panel)

    body = QHBoxLayout()
    body.setSpacing(8)

    grid_panel = StonePanel()
    grid_panel.body.addWidget(display_label("Resources"))
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    grid_host = QWidget()
    grid = QGridLayout(grid_host)
    grid.setSpacing(6)
    grid.setAlignment(_top_left())
    scroll.setWidget(grid_host)
    grid_panel.body.addWidget(scroll)
    body.addWidget(grid_panel, 3)

    detail = StonePanel(raised=True)
    detail_name = display_label("")
    detail_name.setObjectName("ankiscape-resource-name")
    detail.body.addWidget(detail_name)
    detail_level = body_label("")
    detail_level.setObjectName("ankiscape-resource-level")
    detail.body.addWidget(detail_level)
    detail_reward = body_label("", wrap=True)
    detail_reward.setObjectName("ankiscape-resource-reward")
    detail.body.addWidget(detail_reward)
    detail_materials = body_label("", wrap=True)
    detail_materials.setObjectName("ankiscape-resource-materials")
    detail.body.addWidget(detail_materials)
    detail_missing = error_label("")
    detail_missing.setVisible(False)
    detail.body.addWidget(detail_missing)
    detail_note = muted_label("Preview only — training changes when you press "
                              "Train.", wrap=True)
    detail.body.addWidget(detail_note)
    train = QPushButton("Train")
    train.setObjectName(OBJECT_NAMES["train_button"])
    train.setProperty("class", "primary")
    try:
        train.setStyleSheet("QPushButton { border-color: #E3BE68; color: #E3BE68; font-weight: bold; }")
    except Exception:
        pass
    train.clicked.connect(lambda: _commit())
    detail.body.addWidget(train)
    detail.body.addStretch(1)
    body.addWidget(detail, 2)
    layout.addLayout(body, 1)

    # Preselection from unlock notices / training shortcuts is consumed on
    # every refresh so an already-built screen still honors it.
    pending = None

    def _select_skill(skill: str):
        state["skill"] = skill
        state["resource"] = ""
        _refresh()

    def _select_resource(display: str):
        state["resource"] = display
        _refresh()

    def _refresh():
        pending = shell.call("take_preselect", default=None)
        if isinstance(pending, dict):
            if pending.get("skill") in tiles:
                state["skill"] = pending["skill"]
                state["resource"] = ""
            if pending.get("resource"):
                state["resource"] = pending["resource"]
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        levels = projection.get("levels", {}) or {}
        inventory = projection.get("inventory", {}) or {}
        selections = shell.call("get_selections", default={}) or {}
        skill = state["skill"]
        for name, tile in tiles.items():
            tile.set_state(int(levels.get(name, 1)),
                           int((projection.get("xp_micro", {}) or {}).get(name, 0)),
                           rules.get("thresholds", []),
                           trained=(name == skill and False))
            tile.setChecked(name == skill)
        entries = resource_entries(rules, skill, levels, inventory,
                                   state["resource"] or selections.get(skill, ""))
        state["entries"] = entries
        if not state["resource"]:
            for entry in entries:
                if entry["selected"]:
                    state["resource"] = entry["display"]
                    break
            if not state["resource"] and entries:
                state["resource"] = entries[0]["display"]
        from .widgets import clear_layout
        clear_layout(grid)
        columns = 5
        for index, entry in enumerate(entries):
            slot = ItemSlot(scale=DEFAULT_SCALE)
            slot.set_item(entry["display"], 0, selected=entry["selected"],
                          locked=entry["locked"],
                          paused=(not entry["materials_ready"]),
                          level_req=entry["level"])
            slot.clicked = _select_resource
            grid.addWidget(slot, index // columns, index % columns)
        grid.setRowStretch(grid.rowCount(), 1)
        _refresh_detail(rules, levels, inventory, entries)

    def _refresh_detail(rules, levels, inventory, entries):
        entry = next((e for e in entries
                      if e["display"] == state["resource"]), None)
        if entry is None:
            detail_name.setText("No resource")
            detail_level.setText("")
            detail_reward.setText("")
            detail_materials.setText("")
            detail_missing.setVisible(False)
            train.setEnabled(False)
            return
        current_selection = (shell.call("get_selections", default={}) or {}).get(
            state["skill"], "")
        taken = entry["display"] == current_selection
        detail_name.setText(entry["display"])
        detail_level.setText(
            f"Requires level {entry['level']} {SKILL_LABELS.get(state['skill'], '')}"
            + (" (locked)" if entry["locked"] else ""))
        if entry["gather"]:
            chance = _success_chance(rules, state["skill"], entry,
                                     int(levels.get(state["skill"], 1)))
            detail_reward.setText(
                f"Per review: {entry['base_xp']} base XP, {chance} success chance, "
                "one item on success and 25% XP on a miss.")
        else:
            detail_reward.setText(
                f"Per review: {entry['base_xp']} base XP and one "
                f"{entry['display']}.")
        if entry["needs"]:
            parts = []
            for name, qty in entry["needs"].items():
                have = int(inventory.get(name, 0))
                mark = "OK" if have >= qty else "missing"
                parts.append(f"{qty}× {name} (have {have}, {mark})")
            detail_materials.setText("Materials: " + ", ".join(parts))
        else:
            detail_materials.setText("No materials required.")
        if entry["locked"]:
            detail_missing.setText(
                f"Locked: reach level {entry['level']} "
                f"{SKILL_LABELS.get(state['skill'], '')} to train this.")
            detail_missing.setVisible(True)
        elif entry["missing_text"]:
            detail_missing.setText("Missing: " + entry["missing_text"])
            detail_missing.setVisible(True)
        else:
            detail_missing.setVisible(False)

        if taken:
            train.setText("Currently training")
            train.setEnabled(False)
        elif entry["locked"]:
            train.setText(f"Locked until level {entry['level']}")
            train.setEnabled(False)
        elif entry["missing_text"]:
            train.setText(f"Train (starts paused) — needs {entry['missing_text']}")
            train.setEnabled(True)
        else:
            train.setText("Train")
            train.setEnabled(True)

    def _commit():
        entry = next((e for e in state["entries"]
                      if e["display"] == state["resource"]), None)
        if entry is None or entry["locked"]:
            return
        result = shell.call("on_change_training", state["skill"],
                            entry["display"], default={"ok": False})
        if isinstance(result, dict) and result.get("ok") is False:
            detail_missing.setText(str(result.get("error", "Could not change training.")))
            detail_missing.setVisible(True)
            return
        shell.call("refresh_views")
        shell.set_section("training")

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _success_chance(rules, skill: str, entry: Dict[str, Any],
                    level: int) -> str:
    try:
        from ..logic_pure import gathering_probability
        prob = gathering_probability(level, entry.get("probability", 0))
        return f"{float(prob) * 100:.1f}%"
    except Exception:
        return "?"


def _top_left():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
    except Exception:
        return 0
