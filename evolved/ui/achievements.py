# evolved/ui/achievements.py - Recognizable achievements with progress.
"""Human-readable names, requirements and progress; locked/unlocked state is
conveyed with text badges, never raw internal ids or color alone.

Rows update by stable achievement id: the panel and its widgets are created
once and only their values change, so repeated reviews never rebuild the
whole list. Hidden pages are marked dirty by the shell and refresh when shown.
"""
from __future__ import annotations

from typing import Any, Dict


def build_achievements_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                        QWidget)
    from . import OBJECT_NAMES
    from .menu_model import achievement_live_rows
    from .widgets import (StonePanel, body_label, display_label, icon_pixmap,
                          muted_label)

    root = QWidget(shell)
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
    host_layout.addStretch(1)
    scroll.setWidget(host)
    layout.addWidget(scroll, 1)

    rows_by_id: Dict[str, Dict[str, Any]] = {}
    state: Dict[str, Any] = {"dirty": True, "order": []}

    def _icon_for(achievement_id: str):
        from ..assets import achievement_icon_path
        return achievement_icon_path(achievement_id)

    def _make_row(row: Dict[str, Any]) -> Dict[str, Any]:
        panel = StonePanel(raised=bool(row["unlocked"]))
        line = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(32, 32)
        path = _icon_for(row["id"])
        pix = icon_pixmap(path, 32) if path else None
        if pix is not None:
            icon.setPixmap(pix)
            icon.setAccessibleName(f"{row['name']} icon")
        else:
            icon.setVisible(False)
        line.addWidget(icon)
        name = display_label(row["name"])
        name.setObjectName("ankiscape-achievement-name")
        line.addWidget(name, 1)
        badge = muted_label("Locked")
        badge.setObjectName("ankiscape-achievement-state")
        line.addWidget(badge)
        panel.body.addLayout(line)
        requirement = body_label(row["requirement"])
        requirement.setObjectName("ankiscape-achievement-requirement")
        panel.body.addWidget(requirement)
        progress = muted_label("")
        progress.setObjectName("ankiscape-achievement-progress")
        progress.setVisible(False)
        panel.body.addWidget(progress)
        panel.setProperty("achievement_id", row["id"])
        widgets = {"panel": panel, "badge": badge, "name": name,
                   "requirement": requirement, "progress": progress}
        _update_row(widgets, row)
        return widgets

    def _update_row(widgets: Dict[str, Any], row: Dict[str, Any]) -> None:
        try:
            widgets["panel"].setProperty("achievement_id", row["id"])
            widgets["name"].setText(row["name"])
            unlocked = bool(row["unlocked"])
            widgets["badge"].setText("Unlocked" if unlocked else "Locked")
            widgets["badge"].setObjectName(
                "ankiscape-success" if unlocked else "ankiscape-muted")
            style = widgets["badge"].style()
            if style is not None:
                style.unpolish(widgets["badge"])
                style.polish(widgets["badge"])
            widgets["requirement"].setText(row["requirement"])
            widgets["progress"].setText(
                f"Progress: {row['progress']}" if row.get("progress") else "")
            widgets["progress"].setVisible(bool(row.get("progress")))
            widgets["panel"].setAccessibleName(
                f"{row['name']}, {'unlocked' if unlocked else 'locked'}, "
                f"{row['requirement']}")
        except Exception:
            pass

    def _refresh():
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        rows = achievement_live_rows(
            rules, projection.get("achievements", []),
            projection.get("levels", {}), projection.get("counters", {}))
        unlocked = sum(1 for r in rows if r["unlocked"])
        summary.setText(f"Achievements: {unlocked} of {len(rows)} complete")
        order = [str(row["id"]) for row in rows]
        for index, row in enumerate(rows):
            aid = str(row["id"])
            widgets = rows_by_id.get(aid)
            if widgets is None:
                widgets = _make_row(row)
                rows_by_id[aid] = widgets
            else:
                _update_row(widgets, row)
            # Re-seat in order without recreating widgets (cheap, bounded).
            host_layout.removeWidget(widgets["panel"])
            host_layout.insertWidget(index, widgets["panel"])
        for aid in list(rows_by_id.keys()):
            if aid not in order:
                widgets = rows_by_id.pop(aid)
                host_layout.removeWidget(widgets["panel"])
                widgets["panel"].hide()
                widgets["panel"].deleteLater()
        state["order"] = order
        state["dirty"] = False

    def _invalidate():
        state["dirty"] = True

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = _invalidate
    root.release = lambda: None
    root.deps = deps
    root.row_count = lambda: len(rows_by_id)
    _refresh()
    return root
