# evolved/ui/menu.py - Evolved tabbed menu (lazy Qt; explicit object names).
"""Tabs: Skills, Bank, Achievements, Hiscores, Settings. Content rows come from
menu_model (pure). All critical controls keyboard-accessible (native widgets,
tab order, & mnemonics). No modal errors on review events - this dialog opens
only from explicit menu actions."""
from __future__ import annotations

from typing import Any, Callable, Dict

from . import OBJECT_NAMES
from .menu_model import (achievement_rows, format_hiscores_rows,
                         hiscores_status, skill_rows, validate_preset)
from .bank import filter_inventory


def show_evolved_menu(parent, deps: Dict[str, Any]) -> None:
    from aqt.qt import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                        QListWidget, QPushButton, QTabWidget, QTextEdit,
                        QVBoxLayout)
    rules = deps["rules"]
    projection = deps["get_projection"]()
    selections = deps.get("selections", {})

    dlg = QDialog(parent)
    try:
        from aqt.qt import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    dlg.setWindowTitle("AnkiScape: Evolved")
    dlg.setObjectName(OBJECT_NAMES["menu"])
    layout = QVBoxLayout(dlg)
    tabs = QTabWidget()
    tabs.setObjectName("ankiscape-menu-tabs")
    layout.addWidget(tabs)

    # --- Skills ---
    skills_box = _plain_tab()
    rows = skill_rows(rules, projection.get("levels", {}),
                      projection.get("xp_micro", {}),
                      projection.get("inventory", {}), selections)
    skills_text = "\n".join(
        f"{r['skill'].title()}: level {r['level']} — {int(r['xp_micro']) // 1000000} XP"
        f" — {r['selection'] or 'no selection'}"
        + ("" if r["materials"].get("ready") else f" ({r['materials'].get('note', '')})")
        for r in rows)
    skills_label = QLabel(skills_text or "No skills yet.")
    skills_label.setObjectName(OBJECT_NAMES["skills_tab"])
    skills_label.setTextInteractionFlags(
        skills_label.textInteractionFlags() | _selectable())
    skills_box.layout().addWidget(skills_label)
    tabs.addTab(skills_box, "&Skills")

    # --- Bank (read-only quantities + simple filters) ---
    bank_box = _plain_tab()
    bank_filter = QLineEdit()
    bank_filter.setObjectName("ankiscape-bank-filter")
    bank_filter.setPlaceholderText("Filter items…")
    bank_skill = QComboBox()
    bank_skill.setObjectName("ankiscape-bank-skill")
    bank_skill.addItems(["all", "mining", "woodcutting", "smithing",
                         "crafting", "fishing", "cooking"])
    bank_list = QListWidget()
    bank_list.setObjectName(OBJECT_NAMES["bank_tab"])
    inventory = projection.get("inventory", {})

    def _refresh_bank():
        skill = bank_skill.currentText()
        rows_b = filter_inventory(inventory, bank_filter.text(),
                                  "" if skill == "all" else skill)
        bank_list.clear()
        for name, qty in rows_b:
            bank_list.addItem(f"{name} × {qty}")

    bank_filter.textChanged.connect(lambda _t: _refresh_bank())
    bank_skill.currentTextChanged.connect(lambda _t: _refresh_bank())
    _refresh_bank()
    bank_box.layout().addWidget(bank_filter)
    bank_box.layout().addWidget(bank_skill)
    bank_box.layout().addWidget(bank_list)
    tabs.addTab(bank_box, "&Bank")

    # --- Achievements ---
    ach_box = _plain_tab()
    required = rules.get("achievements", {}).get("required", [])
    ach_rows = achievement_rows(required, projection.get("achievements", []))
    done = sum(1 for r in ach_rows if r["unlocked"])
    ach_label = QLabel(f"{done}/{len(ach_rows)} achievements\n" + "\n".join(
        f"[{'x' if r['unlocked'] else ' '}] {r['id']}" for r in ach_rows))
    ach_label.setObjectName(OBJECT_NAMES["achievements_tab"])
    ach_box.layout().addWidget(ach_label)
    tabs.addTab(ach_box, "&Achievements")

    # --- Hiscores ---
    hs_box = _plain_tab()
    hs_status = QLabel(hiscores_status(
        logged_in=bool(deps.get("logged_in")), last_success=deps.get("last_success"),
        pending=int(deps.get("pending", 0)), last_error=str(deps.get("last_error", ""))))
    hs_status.setObjectName("ankiscape-hiscores-status")
    hs_skill = QComboBox()
    hs_skill.setObjectName("ankiscape-hiscores-skill")
    hs_skill.addItems(["mining", "woodcutting", "smithing", "crafting",
                       "fishing", "cooking"])
    hs_list = QListWidget()
    hs_list.setObjectName(OBJECT_NAMES["hiscores_tab"])
    sync_btn = QPushButton("&Sync now")
    sync_btn.setObjectName(OBJECT_NAMES["sync_button"])
    refresh_btn = QPushButton("&Refresh")

    def _refresh_hs():
        query = deps.get("query_hiscores")
        if query is None:
            hs_list.clear()
            return
        try:
            rows_h = format_hiscores_rows(query(hs_skill.currentText(), 50))
        except Exception as exc:
            hs_status.setText(f"problem: {exc!r}")
            return
        hs_list.clear()
        for row in rows_h:
            hs_list.addItem(f"#{row['rank']} {row['username']} — {row['xp']} XP")

    sync_btn.clicked.connect(lambda: (deps.get("on_sync") or (lambda: None))())
    refresh_btn.clicked.connect(lambda: _refresh_hs())
    hs_skill.currentTextChanged.connect(lambda _t: _refresh_hs())
    hs_row = QHBoxLayout()
    hs_row.addWidget(sync_btn)
    hs_row.addWidget(refresh_btn)
    hs_box.layout().addWidget(hs_status)
    hs_box.layout().addWidget(hs_skill)
    hs_box.layout().addWidget(hs_list)
    hs_box.layout().addLayout(hs_row)
    tabs.addTab(hs_box, "&Hiscores")

    # --- Settings ---
    set_box = _plain_tab()
    preset = QComboBox()
    preset.setObjectName(OBJECT_NAMES["preset_combo"])
    preset.addItems(["mining", "woodcutting", "fishing"])
    current_preset = str(deps.get("preset", "mining"))
    if current_preset in ("mining", "woodcutting", "fishing"):
        preset.setCurrentText(current_preset)

    def _preset_changed(text: str):
        try:
            skill = validate_preset(text)
        except ValueError:
            return
        setter = deps.get("on_preset")
        if setter:
            setter(skill)

    preset.currentTextChanged.connect(_preset_changed)
    mode_btn = QPushButton("Switch to &Classic (restart required)")
    mode_btn.setObjectName(OBJECT_NAMES["mode_switch"])
    mode_btn.clicked.connect(lambda: (deps.get("on_mode_switch") or (lambda _m: None))("classic"))
    account_btn = QPushButton("&Account…")
    account_btn.setObjectName("ankiscape-account-button")
    account_btn.clicked.connect(lambda: (deps.get("on_account") or (lambda: None))())
    export_btn = QPushButton("&Export game backup…")
    export_btn.clicked.connect(lambda: (deps.get("on_export") or (lambda: None))())
    restore_btn = QPushButton("&Restore game backup…")
    restore_btn.clicked.connect(lambda: (deps.get("on_restore") or (lambda: None))())
    diag = QTextEdit()
    diag.setObjectName("ankiscape-diagnostics")
    diag.setReadOnly(True)
    try:
        diag.setPlainText(str((deps.get("get_diagnostics") or (lambda: ""))()))
    except Exception:
        pass
    for widget in (QLabel("Catch-up gathering &preset:"), preset, mode_btn,
                   account_btn, export_btn, restore_btn,
                   QLabel("&Diagnostics:"), diag):
        if isinstance(widget, QLabel) and widget.text().startswith("Catch-up"):
            widget.setBuddy(preset)
        set_box.layout().addWidget(widget)
    tabs.addTab(set_box, "Se&ttings")

    dlg.exec()


def _plain_tab():
    from aqt.qt import QVBoxLayout, QWidget
    box = QWidget()
    box.setLayout(QVBoxLayout())
    return box


def _selectable():
    from aqt.qt import Qt
    return Qt.TextInteractionFlag.TextSelectableByMouse
