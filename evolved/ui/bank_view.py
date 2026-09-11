# evolved/ui/bank_view.py - RuneScape-style Bank grid (read-only).
"""Item slots with quantity overlays, search + skill filters, and a
selected-item detail panel showing name, icon and producing/consuming skills.
No list-view toggle. Empty bank explains how to gather; an empty search result
offers Clear filters. Slots update by item identity: quantities and selection
change in place, never by recreating the grid.
"""
from __future__ import annotations

from typing import Any, Dict


def build_bank_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                        QPushButton, QScrollArea, QVBoxLayout, QWidget)
    from . import OBJECT_NAMES
    from .menu_model import bank_entries, bank_skills_for
    from .theme import DEFAULT_SCALE
    from .widgets import (ItemSlot, StonePanel, body_label, display_label,
                          icon_pixmap, muted_label)

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
    detail_head = QHBoxLayout()
    detail_icon = QLabel()
    detail_icon.setFixedSize(40, 40)
    detail_head.addWidget(detail_icon)
    detail_name = display_label("")
    detail_name.setObjectName("ankiscape-bank-item-name")
    detail_head.addWidget(detail_name, 1)
    detail.body.addLayout(detail_head)
    detail_qty = body_label("")
    detail_qty.setObjectName("ankiscape-bank-item-qty")
    detail.body.addWidget(detail_qty)
    detail_rel = body_label("", wrap=True)
    detail_rel.setObjectName("ankiscape-bank-item-relations")
    detail.body.addWidget(detail_rel)
    detail_note = muted_label("Items are collectibles here — the Bank is "
                              "read-only.", wrap=True)
    detail.body.addWidget(detail_note)
    empty_art = QLabel()
    empty_art.setFixedSize(48, 48)
    empty_art.setVisible(False)
    detail.body.addWidget(empty_art)
    empty_hint = body_label("", wrap=True)
    empty_hint.setObjectName("ankiscape-bank-empty")
    detail.body.addWidget(empty_hint)
    detail.body.addStretch(1)
    body.addWidget(detail, 2)
    layout.addLayout(body, 1)

    state: Dict[str, Any] = {"selected": "", "dirty": True}
    slots_by_display: Dict[str, Any] = {}
    columns = 6

    def _empty_art_pixmap():
        from ..assets import slot_icon_path
        return icon_pixmap(slot_icon_path("bank.empty"), 48)

    def _detail_icon(display: str):
        from ..assets import display_icon
        return icon_pixmap(display_icon(display), 40)

    def _refresh():
        rules = shell.call("get_rules", default={}) or {}
        projection = shell.call("get_projection", default={}) or {}
        inventory = projection.get("inventory", {}) or {}
        skill = skill_filter.currentText()
        rows = bank_entries(rules, inventory, query=search.text(),
                            skill="" if skill == "all" else skill)
        order = [str(r["display"]) for r in rows]
        for index, row in enumerate(rows):
            display = str(row["display"])
            slot = slots_by_display.get(display)
            if slot is None:
                slot = ItemSlot(scale=DEFAULT_SCALE)
                slot.clicked = _select
                slots_by_display[display] = slot
            slot.set_item(display, row["qty"],
                          selected=(display == state["selected"]))
            grid.removeWidget(slot)
            grid.addWidget(slot, index // columns, index % columns)
        for display in list(slots_by_display.keys()):
            if display not in order:
                slot = slots_by_display.pop(display)
                grid.removeWidget(slot)
                slot.hide()
                slot.deleteLater()
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
            empty_pix = _empty_art_pixmap()
            if empty_pix is not None:
                detail_icon.setPixmap(empty_pix)
                detail_icon.setAccessibleName("Empty bank")
            else:
                detail_icon.clear()
            empty_art.setVisible(True)
            if empty_pix is not None:
                empty_art.setPixmap(empty_pix)
            else:
                empty_art.clear()
            empty_art.setAccessibleName("Empty bank")
            clear.setVisible(has_any)
        else:
            empty_hint.setText("")
            empty_art.setVisible(False)
            clear.setVisible(bool(search.text()) or skill != "all")
            if state["selected"] not in order:
                state["selected"] = order[0]
            _refresh_detail(rules, inventory)
        state["dirty"] = False

    def _refresh_detail(rules, inventory):
        display = state["selected"]
        rel = bank_skills_for(rules, display)
        detail_name.setText(display)
        pix = _detail_icon(display)
        if pix is not None:
            detail_icon.setPixmap(pix)
            detail_icon.setAccessibleName(f"{display} icon")
        else:
            detail_icon.clear()
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

    def _invalidate():
        state["dirty"] = True

    search.textChanged.connect(lambda _t: _refresh())
    skill_filter.currentTextChanged.connect(lambda _t: _refresh())
    clear.clicked.connect(_clear_filters)

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = _invalidate
    root.release = lambda: None
    root.deps = deps
    root.row_count = lambda: len(slots_by_display)
    _refresh()
    return root


def _top_left():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
    except Exception:
        return 0
