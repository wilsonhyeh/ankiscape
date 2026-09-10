# evolved/ui/bank_view.py - RuneScape-style Bank grid (read-only).
"""Item slots with quantity overlays, search + skill filters, and a
selected-item detail panel showing name and producing/consuming skills.
No list-view toggle. Empty bank explains how to gather; an empty search result
offers Clear filters.
"""
from __future__ import annotations

from typing import Any, Dict


def build_bank_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                        QPushButton, QScrollArea, QVBoxLayout, QWidget)
    from . import OBJECT_NAMES
    from .menu_model import bank_entries, bank_skills_for
    from .theme import DEFAULT_SCALE
    from .widgets import (ItemSlot, StonePanel, body_label, clear_layout,
                          display_label, muted_label)

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["bank_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    controls = QHBoxLayout()
    search = QLineEdit()
    search.setObjectName(OBJECT_NAMES["bank_filter"])
    search.setPlaceholderText("Search items…")
    skill_filter = QComboBox()
    skill_filter.setObjectName(OBJECT_NAMES["bank_skill_filter"])
    skill_filter.addItems(["all", "mining", "woodcutting", "smithing",
                           "crafting", "fishing", "cooking"])
    clear = QPushButton("Clear filters")
    clear.setObjectName("ankiscape-bank-clear")
    clear.setVisible(False)
    controls.addWidget(QLabel("Search:"))
    controls.addWidget(search, 1)
    controls.addWidget(QLabel("Skill:"))
    controls.addWidget(skill_filter)
    controls.addWidget(clear)
    layout.addLayout(controls)

    body = QHBoxLayout()
    grid_panel = StonePanel()
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
    detail_name.setObjectName("ankiscape-bank-item-name")
    detail.body.addWidget(detail_name)
    detail_qty = body_label("")
    detail_qty.setObjectName("ankiscape-bank-item-qty")
    detail.body.addWidget(detail_qty)
    detail_rel = body_label("", wrap=True)
    detail_rel.setObjectName("ankiscape-bank-item-relations")
    detail.body.addWidget(detail_rel)
    detail_note = muted_label("Items are collectibles here — the Bank is "
                              "read-only.", wrap=True)
    detail.body.addWidget(detail_note)
    empty_hint = body_label("", wrap=True)
    empty_hint.setObjectName("ankiscape-bank-empty")
    detail.body.addWidget(empty_hint)
    detail.body.addStretch(1)
    body.addWidget(detail, 2)
    layout.addLayout(body, 1)

    state: Dict[str, Any] = {"selected": ""}

    def _refresh():
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        inventory = projection.get("inventory", {}) or {}
        skill = skill_filter.currentText()
        rows = bank_entries(rules, inventory, query=search.text(),
                            skill="" if skill == "all" else skill)
        clear_layout(grid)
        columns = 6
        for index, row in enumerate(rows):
            slot = ItemSlot(scale=DEFAULT_SCALE)
            slot.set_item(row["display"], row["qty"],
                          selected=(row["display"] == state["selected"]))
            slot.clicked = _select
            grid.addWidget(slot, index // columns, index % columns)
        has_any = any(int(v) > 0 for v in inventory.values())
        if not rows:
            if not has_any:
                empty_hint.setText(
                    "The Bank is empty. Select a gathering skill and press "
                    "Train — every successful review adds items here.")
            else:
                empty_hint.setText("No items match these filters.")
            detail_name.setText("Nothing selected")
            detail_qty.setText("")
            detail_rel.setText("")
            clear.setVisible(has_any)
        else:
            empty_hint.setText("")
            clear.setVisible(bool(search.text()) or skill != "all")
            if state["selected"] not in {r["display"] for r in rows}:
                state["selected"] = rows[0]["display"]
            _refresh_detail(rules, inventory)

    def _refresh_detail(rules, inventory):
        display = state["selected"]
        rel = bank_skills_for(rules, display)
        detail_name.setText(display)
        detail_qty.setText(f"Quantity: {int(inventory.get(display, 0)):,}")
        produced = ", ".join(s.title() for s in rel["produced_by"]) or "Unknown source"
        consumed = ", ".join(s.title() for s in rel["consumed_by"]) or "Not used by a recipe"
        detail_rel.setText(f"Produced by: {produced}\nConsumed by: {consumed}")

    def _select(display: str):
        state["selected"] = display
        _refresh()

    def _clear_filters():
        search.setText("")
        skill_filter.setCurrentIndex(0)
        _refresh()

    search.textChanged.connect(lambda _t: _refresh())
    skill_filter.currentTextChanged.connect(lambda _t: _refresh())
    clear.clicked.connect(_clear_filters)

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _top_left():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
    except Exception:
        return 0
