# evolved/ui/achievements.py - Recognizable achievements with progress.
"""Human-readable names, requirements and progress; locked/unlocked state is
conveyed with text badges, never raw internal ids or color alone."""
from __future__ import annotations

from typing import Any, Dict


def build_achievements_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                        QWidget)
    from . import OBJECT_NAMES
    from .menu_model import achievement_live_rows
    from .theme import DEFAULT_SCALE, scaled
    from .widgets import (StonePanel, body_label, clear_layout, display_label,
                          icon_pixmap, muted_label, success_label)

    root = QWidget()
    root.setObjectName(OBJECT_NAMES["achievements_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    summary = display_label("")
    summary.setObjectName("ankiscape-achievement-summary")
    layout.addWidget(summary)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    host = QWidget()
    host_layout = QVBoxLayout(host)
    host_layout.setSpacing(6)
    scroll.setWidget(host)
    layout.addWidget(scroll, 1)

    def _refresh():
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        rows = achievement_live_rows(
            rules, projection.get("achievements", []),
            projection.get("levels", {}), projection.get("counters", {}))
        unlocked = sum(1 for r in rows if r["unlocked"])
        summary.setText(f"Achievements: {unlocked} of {len(rows)} complete")
        clear_layout(host_layout)
        for row in rows:
            host_layout.addWidget(_row(row))
        host_layout.addStretch(1)

    def _icon_for(achievement_id: str) -> str:
        from ..assets import skill_icon_path
        if achievement_id.startswith("skill_"):
            parts = achievement_id.split("_")
            if len(parts) == 3:
                return skill_icon_path(parts[2])
        if achievement_id == "first_catch":
            return skill_icon_path("fishing")
        return skill_icon_path("cooking")

    def _row(row: Dict[str, Any]):
        panel = StonePanel(raised=bool(row["unlocked"]))
        line = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(32, 32)
        pix = icon_pixmap(_icon_for(row["id"]), 32)
        if pix is not None:
            icon.setPixmap(pix)
            icon.setAccessibleName(f"{row['name']} icon")
        line.addWidget(icon)
        name = display_label(row["name"])
        name.setObjectName("ankiscape-achievement-name")
        line.addWidget(name, 1)
        badge = success_label("Unlocked") if row["unlocked"] else muted_label("Locked")
        badge.setObjectName("ankiscape-achievement-state")
        line.addWidget(badge)
        panel.body.addLayout(line)
        requirement = body_label(row["requirement"])
        requirement.setObjectName("ankiscape-achievement-requirement")
        panel.body.addWidget(requirement)
        if row["progress"]:
            progress = muted_label(f"Progress: {row['progress']}")
            progress.setObjectName("ankiscape-achievement-progress")
            panel.body.addWidget(progress)
        panel.setProperty("achievement_id", row["id"])
        panel.setAccessibleName(
            f"{row['name']}, {'unlocked' if row['unlocked'] else 'locked'}, "
            f"{row['requirement']}")
        return panel

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root
