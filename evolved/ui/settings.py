# evolved/ui/settings.py - Grouped settings pages with immediate apply.
"""Appearance & HUD | Training & Catch-up | Account & Sync | Advanced.
No Save button: changes persist and apply immediately. Advanced holds backups,
diagnostics and the mode switch; diagnostics are collapsed by default and
credential-redacted by the provider."""
from __future__ import annotations

from typing import Any, Dict


def build_settings_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
                        QPushButton, QTabWidget, QTextEdit, QVBoxLayout,
                        QWidget, QSignalBlocker)
    from . import OBJECT_NAMES
    from .menu_model import CATCHUP_PRESETS
    from .theme import SCALES
    from .widgets import StonePanel, body_label, display_label, muted_label

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["settings_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    tabs = QTabWidget()
    tabs.setObjectName("ankiscape-settings-tabs")
    layout.addWidget(tabs, 1)

    # ------------------------------------------------- Appearance & HUD
    appearance = _page()
    appearance.layout().addWidget(display_label("Appearance & HUD"))
    form = QFormLayout()
    scale = QComboBox()
    scale.setObjectName("ankiscape-setting-ui-scale")
    scale.addItems([str(s) for s in SCALES])
    form.addRow("UI scale:", scale)
    hud_visible = QCheckBox("Show the review HUD")
    hud_visible.setObjectName("ankiscape-setting-hud-visible")
    form.addRow("", hud_visible)
    hud_position = QComboBox()
    hud_position.setObjectName("ankiscape-setting-hud-position")
    hud_position.addItems(["bottom", "top"])
    form.addRow("HUD position:", hud_position)
    celebrations = QCheckBox("Play reward celebrations (brief animations)")
    celebrations.setObjectName("ankiscape-setting-celebrations")
    form.addRow("", celebrations)
    reduced = QCheckBox("Reduce motion (updates appear immediately)")
    reduced.setObjectName("ankiscape-setting-reduced-motion")
    form.addRow("", reduced)
    sound = QCheckBox("Sound effects (level-ups and unlocks only)")
    sound.setObjectName("ankiscape-setting-sound")
    form.addRow("", sound)
    appearance.layout().addLayout(form)
    appearance.layout().addWidget(muted_label(
        "The game keeps its fixed RuneScape palette in light and dark Anki "
        "themes. Body text stays at a readable system font.", wrap=True))
    appearance.layout().addStretch(1)
    tabs.addTab(appearance, "&Appearance")

    # ------------------------------------------------- Training & Catch-up
    training_page = _page()
    training_page.layout().addWidget(display_label("Training & Catch-up"))
    training_page.layout().addWidget(body_label(
        "Desktop training is what you selected on the Skills screen. "
        "Catch-up is different: reviews that happened on this collection while "
        "the add-on was inactive (for example, phone reviews synced later) are "
        "credited to one gathering skill you choose here. It never changes "
        "your desktop training.", wrap=True))
    preset = QComboBox()
    preset.setObjectName(OBJECT_NAMES["preset_combo"])
    preset.addItems(list(CATCHUP_PRESETS))
    preset_form = QFormLayout()
    preset_form.addRow("Catch-up gathering preset:", preset)
    training_page.layout().addLayout(preset_form)
    change = QPushButton("Change desktop training…")
    change.setObjectName("ankiscape-settings-change-training")
    change.clicked.connect(lambda: shell.set_section("skills"))
    training_page.layout().addWidget(change)
    training_page.layout().addStretch(1)
    tabs.addTab(training_page, "&Training")

    # ------------------------------------------------- Account & Sync
    account = _page()
    account.layout().addWidget(display_label("Account & Sync"))
    account_status = body_label("")
    account_status.setObjectName("ankiscape-settings-account-status")
    account_status.setWordWrap(True)
    account.layout().addWidget(account_status)
    account_actions = QHBoxLayout()
    login = QPushButton("Log in")
    login.setObjectName("ankiscape-settings-login")
    register = QPushButton("Create account")
    register.setObjectName("ankiscape-settings-register")
    sync_now = QPushButton("Sync now")
    sync_now.setObjectName("ankiscape-settings-sync")
    logout = QPushButton("Log out")
    logout.setObjectName("ankiscape-settings-logout")
    for widget in (login, register, sync_now, logout):
        account_actions.addWidget(widget)
    account_actions.addStretch(1)
    account.layout().addLayout(account_actions)
    account.layout().addWidget(muted_label(
        "Keep me signed in saves your session in the operating system credential "
        "vault when available. Log out removes it. Your password is never saved.", wrap=True))
    recover = QPushButton("Forgot password?")
    recover.setObjectName("ankiscape-settings-recovery")
    recover.clicked.connect(lambda: shell.call("on_recovery"))
    account.layout().addWidget(recover)
    account.layout().addStretch(1)
    tabs.addTab(account, "&Account")

    # ------------------------------------------------- Advanced
    advanced = _page()
    advanced.layout().addWidget(display_label("Advanced"))
    backup_row = QHBoxLayout()
    export = QPushButton("Export backup…")
    export.setObjectName("ankiscape-settings-export")
    restore = QPushButton("Restore backup…")
    restore.setObjectName("ankiscape-settings-restore")
    backup_row.addWidget(export)
    backup_row.addWidget(restore)
    backup_row.addStretch(1)
    advanced.layout().addLayout(backup_row)
    advanced.layout().addWidget(body_label(
        "Restoring shows what it will replace and asks for confirmation before "
        "anything changes.", wrap=True))
    switch_row = QHBoxLayout()
    switch_btn = QPushButton("Switch to Classic…")
    switch_btn.setObjectName(OBJECT_NAMES["mode_switch"])
    switch_btn.setProperty("class", "danger")
    switch_row.addWidget(switch_btn)
    switch_row.addStretch(1)
    advanced.layout().addLayout(switch_row)
    advanced.layout().addWidget(muted_label(
        "Switching modes keeps both games; each mode has its own progress. "
        "You must leave the reviewer before switching.", wrap=True))
    diag_toggle = QPushButton("Show diagnostics")
    diag_toggle.setObjectName("ankiscape-settings-diagnostics-toggle")
    diag_toggle.setCheckable(True)
    advanced.layout().addWidget(diag_toggle)
    diagnostics = QTextEdit()
    diagnostics.setObjectName("ankiscape-diagnostics")
    diagnostics.setReadOnly(True)
    diagnostics.setVisible(False)
    advanced.layout().addWidget(diagnostics, 1)
    help_row = QHBoxLayout()
    report_btn = QPushButton("Report a bug…")
    report_btn.setObjectName("ankiscape-settings-report-bug")
    report_btn.setToolTip("Open a prefilled GitHub issue you review and send")
    report_btn.clicked.connect(lambda: shell.call("on_report_issue"))
    help_row.addWidget(report_btn)
    help_row.addStretch(1)
    advanced.layout().addLayout(help_row)
    advanced.layout().addWidget(muted_label(
        "Reports are public on GitHub. Nothing is uploaded automatically; "
        "you review the exact text before it opens.", wrap=True))
    tabs.addTab(advanced, "Ad&vanced")

    def _refresh():
        blockers = [QSignalBlocker(w) for w in (scale, hud_visible, hud_position,
                    celebrations, reduced, sound, preset)]
        settings = shell.call("get_settings", default={}) or {}
        scale.setCurrentText(str(settings.get("ui_scale", 100)))
        hud_visible.setChecked(bool(settings.get("hud_visible", True)))
        position = str(settings.get("hud_position", "bottom"))
        hud_position.setCurrentText(position if position in ("top", "bottom")
                                    else "bottom")
        celebrations.setChecked(bool(settings.get("celebrations", True)))
        reduced.setChecked(bool(settings.get("reduced_motion", False)))
        sound.setChecked(bool(settings.get("sound", False)))
        preset.setCurrentText(str(shell.call("get_preset", default="mining")))

        status = shell.call("get_status", default={}) or {}
        account_info = shell.call("get_account", default={}) or {}
        if account_info.get("logged_in"):
            pending = int(status.get("pending", 0) or 0)
            rejected = int(status.get("rejected", 0) or 0)
            if status.get("sync_state") == "syncing":
                progress = "Syncing…"
            elif pending or rejected:
                parts = []
                if pending:
                    parts.append(f"{pending} change(s) waiting to sync")
                if rejected:
                    parts.append(f"{rejected} change(s) couldn't sync")
                progress = "; ".join(parts) + "."
            else:
                progress = "All progress synced."
            account_status.setText(
                f"Signed in as {account_info.get('username', '?')}. " + progress)
            if not account_info.get("remembered"):
                account_status.setText(account_status.text() + " Sign-in is active for this session only; no saved credential is available.")
            login.setVisible(False)
            register.setVisible(False)
            sync_now.setVisible(True)
            logout.setVisible(True)
        else:
            account_status.setText(
                "Not signed in. Your progress is saved on this computer; an "
                "account adds online backup, catch-up from other devices, and "
                "Hiscores.")
            login.setVisible(True)
            register.setVisible(True)
            sync_now.setVisible(False)
            logout.setVisible(False)
        switch_btn.setText("Switch to Classic…")

    def _setting_changed(key: str):
        def _apply():
            shell.call("apply_setting", key, _read(key))
        return _apply

    def _read(key: str):
        if key == "ui_scale":
            return int(scale.currentText())
        if key == "hud_visible":
            return bool(hud_visible.isChecked())
        if key == "hud_position":
            return hud_position.currentText()
        if key == "celebrations":
            return bool(celebrations.isChecked())
        if key == "reduced_motion":
            return bool(reduced.isChecked())
        if key == "sound":
            return bool(sound.isChecked())
        return None

    scale.currentTextChanged.connect(lambda _t: _setting_changed("ui_scale")())
    hud_visible.toggled.connect(lambda _c: _setting_changed("hud_visible")())
    hud_position.currentTextChanged.connect(
        lambda _t: _setting_changed("hud_position")())
    celebrations.toggled.connect(lambda _c: _setting_changed("celebrations")())
    reduced.toggled.connect(lambda _c: _setting_changed("reduced_motion")())
    sound.toggled.connect(lambda _c: _setting_changed("sound")())
    preset.currentTextChanged.connect(lambda text: shell.call("on_preset", text))
    login.clicked.connect(lambda: shell.call("on_account"))
    register.clicked.connect(lambda: shell.call("on_register"))
    sync_now.clicked.connect(lambda: shell.call("on_sync"))
    logout.clicked.connect(lambda: shell.call("on_logout"))
    def backup_action(name):
        result = shell.call(name, default={}) or {}
        from aqt.utils import showInfo, showWarning
        if result.get("ok"):
            showInfo("Backup exported." if name == "on_export" else "Backup restored.", parent=shell)
        elif result.get("error") != "cancelled":
            showWarning(str(result.get("error", "Could not complete the backup action.")), parent=shell)
    export.clicked.connect(lambda: backup_action("on_export"))
    restore.clicked.connect(lambda: backup_action("on_restore"))
    switch_btn.clicked.connect(lambda: _switch_mode(switch_btn))

    def _switch_mode(button):
        result = shell.call("on_mode_switch", "classic", default={}) or {}
        if isinstance(result, dict) and not result.get("ok", True):
            button.setText(str(result.get("error", "Cannot switch now"))[:60])
        else:
            button.setText("Switching…")

    def _toggle_diagnostics(checked: bool):
        diagnostics.setVisible(bool(checked))
        diag_toggle.setText("Hide diagnostics" if checked else "Show diagnostics")
        if checked:
            text = shell.call("get_diagnostics", default="")
            diagnostics.setPlainText(str(text))

    diag_toggle.toggled.connect(_toggle_diagnostics)

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _page():
    from aqt.qt import QVBoxLayout, QWidget
    page = QWidget()
    page.setLayout(QVBoxLayout())
    return page
