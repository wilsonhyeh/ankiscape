# evolved/ui/account.py - One account window for every account entry point.
"""Pages: login, registration, signup verification, recovery request and
recovery confirmation. Named actions (Log in / Register / Verify / Reset
password), Back, explicit Resend, show-password control, and status/error
lines. All network work is executed by the injected runner off the UI thread;
this module never calls HTTP itself and never blocks the event loop.

Stable object names (documented for tests and the native journey):
  window            ankiscape-account-window
  pages             ankiscape-account-<login|register|verify|
                    recovery-request|recovery-confirm>
  inputs            ankiscape-account-identity, -password, -remember,
                    -username, -email, -register-password, -verify-email-input,
                    -code, -new-password
  actions           ankiscape-account-primary, -back, -cancel, -resend,
                    -check-status, -forgot, -go-register, -reset-shortcut,
                    -show-password
  status/error      ankiscape-account-status, ankiscape-account-error
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional


def default_runner() -> Callable[..., None]:
    """Thread + queued-signal runner for environments without Anki taskman."""
    from aqt.qt import QObject, pyqtSignal

    class _Bridge(QObject):
        done = pyqtSignal(object)

    def runner(work, callback):
        bridge = _Bridge()

        def _thread():
            try:
                value = work()
            except BaseException as exc:  # delivered as an error, never raised
                value = exc
            try:
                bridge.done.emit(value)
            except RuntimeError:
                pass

        bridge.done.connect(lambda value: callback(value))
        threading.Thread(target=_thread, daemon=True,
                         name="ankiscape-account").start()

    return runner


def build_account_window(parent, flow, *, runner: Optional[Callable] = None,
                         title: str = "AnkiScape account"):
    from aqt.qt import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                        QLineEdit, QPushButton, QStackedWidget, QTimer,
                        QVBoxLayout, QWidget)
    from .theme import DEFAULT_SCALE
    from .widgets import apply_theme, body_label, display_label, muted_label

    runner = runner or default_runner()
    dlg = QDialog(parent)
    dlg.setObjectName("ankiscape-account-window")
    dlg.setWindowTitle(title)
    apply_theme(dlg)
    root = QVBoxLayout(dlg)

    heading = display_label("Account")
    heading.setObjectName("ankiscape-account-heading")
    root.addWidget(heading)

    status = body_label("")
    status.setObjectName("ankiscape-account-status")
    status.setWordWrap(True)
    root.addWidget(status)
    error = QLabel("")
    error.setObjectName("ankiscape-account-error")
    error.setWordWrap(True)
    error.setProperty("class", "error")
    root.addWidget(error)

    stack = QStackedWidget()
    stack.setObjectName("ankiscape-account-pages")
    root.addWidget(stack, 1)

    pages: Dict[str, QWidget] = {}
    inputs: Dict[str, Any] = {}
    shows: Dict[str, Any] = {}

    def _page(name: str, page_title: str):
        page = QWidget()
        page.setObjectName(f"ankiscape-account-{name}")
        layout = QVBoxLayout(page)
        label = muted_label(page_title, wrap=True)
        layout.addWidget(label)
        form = QFormLayout()
        layout.addLayout(form)
        pages[name] = page
        stack.addWidget(page)
        return page, layout, form

    def _secret_row(form, key: str, label: str, object_name: str,
                    limit: int = 256):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setObjectName(object_name)
        edit.setMaxLength(limit)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        show = QPushButton("Show")
        show.setObjectName("ankiscape-account-show-password")
        show.setCheckable(True)
        show.setAccessibleName("Show password")

        def _toggle(checked: bool) -> None:
            edit.setEchoMode(QLineEdit.EchoMode.Normal if checked
                             else QLineEdit.EchoMode.Password)
            show.setText("Hide" if checked else "Show")

        show.toggled.connect(_toggle)
        row.addWidget(edit, 1)
        row.addWidget(show, 0)
        form.addRow(label, holder)
        inputs[key] = edit
        shows[key] = show
        return edit

    # ------------------------------------------------------------ login page
    login_page, login_layout, login_form = _page(
        "login", "Log in to back up progress and join the Hiscores.")
    identity = QLineEdit()
    identity.setObjectName("ankiscape-account-identity")
    identity.setMaxLength(320)
    login_form.addRow("Username or email:", identity)
    inputs["identity"] = identity
    _secret_row(login_form, "password", "Password:", "ankiscape-account-password")
    remember = QCheckBox("Keep me signed in on this computer")
    remember.setObjectName("ankiscape-account-remember")
    remember.setChecked(True)
    login_form.addRow("", remember)
    inputs["remember"] = remember
    login_actions = QHBoxLayout()
    login_primary = QPushButton("Log in")
    login_primary.setObjectName("ankiscape-account-primary")
    login_primary.setProperty("class", "primary")
    login_primary.setDefault(True)
    forgot = QPushButton("Forgot password?")
    forgot.setObjectName("ankiscape-account-forgot")
    go_register = QPushButton("Create account")
    go_register.setObjectName("ankiscape-account-go-register")
    login_actions.addWidget(login_primary)
    login_actions.addWidget(forgot)
    login_actions.addWidget(go_register)
    login_actions.addStretch(1)
    login_layout.addLayout(login_actions)
    login_layout.addStretch(1)

    # --------------------------------------------------------- register page
    register_page, register_layout, register_form = _page(
        "register", "Create an account (optional; local play works without "
                    "one).")
    username = QLineEdit()
    username.setObjectName("ankiscape-account-username")
    username.setMaxLength(20)
    register_form.addRow("Username (3\u201320, a\u2013z 0\u20139 _):", username)
    inputs["username"] = username
    email = QLineEdit()
    email.setObjectName("ankiscape-account-email")
    email.setMaxLength(320)
    register_form.addRow("Email (verification and recovery):", email)
    inputs["email"] = email
    _secret_row(register_form, "register_password", "Password:",
                "ankiscape-account-register-password")
    register_actions = QHBoxLayout()
    register_primary = QPushButton("Register")
    register_primary.setObjectName("ankiscape-account-primary")
    register_primary.setProperty("class", "primary")
    register_actions.addWidget(register_primary)
    register_actions.addStretch(1)
    register_layout.addLayout(register_actions)
    reset_shortcut = QPushButton("Reset password")
    reset_shortcut.setObjectName("ankiscape-account-reset-shortcut")
    reset_shortcut.setVisible(False)
    register_layout.addWidget(reset_shortcut)
    register_layout.addStretch(1)

    # ----------------------------------------------------------- verify page
    verify_page, verify_layout, verify_form = _page(
        "verify", "Enter the verification code from your email.")
    verify_email = muted_label("", wrap=True)
    verify_email.setObjectName("ankiscape-account-verify-email")
    verify_email.setText(
        "Enter the code we emailed you for this account. Codes can take a "
        "minute. Resend is available after the cooldown; providers may "
        "limit repeated requests.")
    verify_layout.insertWidget(1, verify_email)
    verify_email_input = QLineEdit()
    verify_email_input.setObjectName("ankiscape-account-verify-email-input")
    verify_email_input.setMaxLength(320)
    verify_form.addRow("Email for this account:", verify_email_input)
    inputs["verify_email"] = verify_email_input
    code = QLineEdit()
    code.setObjectName("ankiscape-account-code")
    code.setMaxLength(32)
    verify_form.addRow("Code from your email:", code)
    inputs["code"] = code
    verify_actions = QHBoxLayout()
    verify_primary = QPushButton("Verify")
    verify_primary.setObjectName("ankiscape-account-primary")
    verify_primary.setProperty("class", "primary")
    resend = QPushButton("Resend code")
    resend.setObjectName("ankiscape-account-resend")
    verify_actions.addWidget(verify_primary)
    verify_actions.addWidget(resend)
    verify_actions.addStretch(1)
    verify_layout.addLayout(verify_actions)
    verify_layout.addStretch(1)

    # --------------------------------------------------- recovery request page
    recovery_request_page, recovery_request_layout, recovery_request_form = \
        _page("recovery-request", "Request a reset code by email.")
    recovery_email = QLineEdit()
    recovery_email.setObjectName("ankiscape-account-email")
    recovery_email.setMaxLength(320)
    recovery_request_form.addRow("Account email:", recovery_email)
    inputs["recovery_email"] = recovery_email
    recovery_request_actions = QHBoxLayout()
    recovery_request_primary = QPushButton("Send reset code")
    recovery_request_primary.setObjectName("ankiscape-account-primary")
    recovery_request_primary.setProperty("class", "primary")
    recovery_request_actions.addWidget(recovery_request_primary)
    recovery_request_actions.addStretch(1)
    recovery_request_layout.addLayout(recovery_request_actions)
    recovery_request_layout.addStretch(1)

    # --------------------------------------------------- recovery confirm page
    recovery_confirm_page, recovery_confirm_layout, recovery_confirm_form = \
        _page("recovery-confirm", "Enter the code and choose a new password.")
    recovery_code = QLineEdit()
    recovery_code.setObjectName("ankiscape-account-code")
    recovery_code.setMaxLength(32)
    recovery_confirm_form.addRow("Reset code:", recovery_code)
    inputs["recovery_code"] = recovery_code
    _secret_row(recovery_confirm_form, "new_password", "New password:",
                "ankiscape-account-new-password")
    recovery_confirm_actions = QHBoxLayout()
    recovery_primary = QPushButton("Reset password")
    recovery_primary.setObjectName("ankiscape-account-primary")
    recovery_primary.setProperty("class", "primary")
    recovery_resend = QPushButton("Resend code")
    recovery_resend.setObjectName("ankiscape-account-resend")
    recovery_confirm_actions.addWidget(recovery_primary)
    recovery_confirm_actions.addWidget(recovery_resend)
    recovery_confirm_actions.addStretch(1)
    recovery_confirm_layout.addLayout(recovery_confirm_actions)
    recovery_confirm_layout.addStretch(1)

    # ------------------------------------------------------------- home page
    home_page, home_layout, home_form = _page(
        "home", "Signed in. Progress syncs to your account.")
    home_username = body_label("")
    home_username.setObjectName("ankiscape-account-home-username")
    home_layout.insertWidget(1, home_username)
    home_sync = muted_label("", wrap=True)
    home_sync.setObjectName("ankiscape-account-home-sync")
    home_layout.insertWidget(2, home_sync)
    home_actions = QHBoxLayout()
    home_logout = QPushButton("Log out")
    home_logout.setObjectName("ankiscape-account-logout")
    delete_open = QPushButton("Delete account\u2026")
    delete_open.setObjectName("ankiscape-account-delete-open")
    delete_open.setProperty("class", "danger")
    home_actions.addWidget(home_logout)
    home_actions.addWidget(delete_open)
    home_actions.addStretch(1)
    home_layout.addLayout(home_actions)
    home_layout.addStretch(1)

    # ----------------------------------------------------------- delete page
    delete_page, delete_layout, delete_form = _page(
        "delete", "Deleting your account removes it permanently.")
    delete_explain = body_label(
        "This removes your account and its online data: game progress, "
        "backups, scores and your place on the leaderboard. Your Classic "
        "progress is not touched. Local Evolved progress on this computer "
        "stays unless you choose to remove it below. Any pending changes "
        "will be lost.", wrap=True)
    delete_explain.setObjectName("ankiscape-account-delete-explain")
    delete_layout.insertWidget(1, delete_explain)
    delete_username = QLineEdit()
    delete_username.setObjectName("ankiscape-account-delete-username")
    delete_username.setMaxLength(64)
    delete_form.addRow("Type your username exactly:", delete_username)
    inputs["delete_username"] = delete_username
    _secret_row(delete_form, "delete_password", "Your password:",
                "ankiscape-account-delete-password")
    delete_local = QCheckBox(
        "Also delete my local progress for this game on this computer \u2014 "
        "this cannot be undone.")
    delete_local.setObjectName("ankiscape-account-delete-local")
    delete_local.setChecked(False)
    try:
        # QCheckBox gained word wrap after some supported Qt builds.
        delete_local.setWordWrap(True)
    except AttributeError:
        pass
    delete_form.addRow("", delete_local)
    inputs["delete_local"] = delete_local
    delete_local_warning = QLabel(
        "Local Evolved progress for this game will be erased on this "
        "computer. Other computers keep their own copies. This cannot be "
        "undone.")
    delete_local_warning.setObjectName("ankiscape-account-delete-local-warning")
    delete_local_warning.setWordWrap(True)
    delete_local_warning.setProperty("class", "error")
    delete_local_warning.setVisible(False)
    delete_layout.addWidget(delete_local_warning)
    delete_actions = QHBoxLayout()
    delete_confirm = QPushButton("Delete account")
    delete_confirm.setObjectName("ankiscape-account-delete-confirm")
    delete_confirm.setProperty("class", "danger")
    delete_confirm.setEnabled(False)
    delete_check = QPushButton("Check status")
    delete_check.setObjectName("ankiscape-account-delete-check")
    delete_check.setVisible(False)
    delete_retry_local = QPushButton("Retry local cleanup")
    delete_retry_local.setObjectName("ankiscape-account-delete-retry-local")
    delete_retry_local.setVisible(False)
    delete_actions.addWidget(delete_confirm)
    delete_actions.addWidget(delete_check)
    delete_actions.addWidget(delete_retry_local)
    delete_actions.addStretch(1)
    delete_layout.addLayout(delete_actions)
    delete_layout.addStretch(1)

    # ---------------------------------------------------------- shared footer
    footer = QHBoxLayout()
    back = QPushButton("Back")
    back.setObjectName("ankiscape-account-back")
    cancel = QPushButton("Cancel")
    cancel.setObjectName("ankiscape-account-cancel")
    check = QPushButton("Check status")
    check.setObjectName("ankiscape-account-check-status")
    check.setVisible(False)
    footer.addWidget(back)
    footer.addWidget(check)
    footer.addStretch(1)
    footer.addWidget(cancel)
    root.addLayout(footer)

    page_index = {"login": 0, "register": 1, "verify": 2,
                  "recovery_request": 3, "recovery_confirm": 4,
                  "home": 5, "delete": 6}
    primary_by_page = {"login": login_primary, "register": register_primary,
                       "verify": verify_primary,
                       "recovery_request": recovery_request_primary,
                       "recovery_confirm": recovery_primary,
                       "delete": delete_confirm}

    def _current_primary():
        return primary_by_page.get(flow.page, login_primary)

    # ------------------------------------------------------------- presenter
    def _apply_page(payload: Dict[str, Any]) -> None:
        page = payload.get("page", "login")
        if page not in page_index:
            return
        stack.setCurrentIndex(page_index[page])
        for key in ("password", "register_password", "code", "new_password",
                    "recovery_code", "delete_password"):
            edit = inputs.get(key)
            if edit is not None and page_index[page] != _page_index_for_input(key):
                edit.clear()
        error.setText(payload.get("error", "") or "")
        status.setText(payload.get("status", "") or "")
        if page == "home":
            info = flow.home_info()
            name = str(info.get("username", "") or "your account")
            home_username.setText(f"Signed in as {name}.")
            pending = int(info.get("pending", 0) or 0)
            rejected = int(info.get("rejected", 0) or 0)
            state = str(info.get("sync_state", "") or "local_only")
            lines = []
            if state:
                lines.append("Sync: " + state.replace("_", " ") + ".")
            lines.append(
                f"{pending} change(s) waiting to sync"
                + (f", {rejected} rejected" if rejected else "") + ".")
            home_sync.setText(" ".join(lines))
            delete_retry_local.setVisible(False)
            delete_check.setVisible(False)
        if page == "delete":
            delete_check.setVisible(False)
            delete_retry_local.setVisible(False)
            _update_delete_confirm()
        prefill_identity = payload.get("prefill_identity", "") or ""
        if prefill_identity:
            inputs["identity"].setText(prefill_identity)
        prefill_email = payload.get("prefill_email", "") or ""
        if prefill_email:
            inputs["recovery_email"].setText(prefill_email)
        verify_seed = (getattr(flow, "_verify_email", "")
                       or flow._register_fields.get("email", ""))
        if page == "verify" and verify_seed \
                and not inputs["verify_email"].text().strip():
            # Seed only an untouched field so a typed address survives.
            inputs["verify_email"].setText(verify_seed)
        if page == "verify":
            _update_resend()
        if page == "recovery_confirm":
            _update_resend()
        reset_shortcut.setVisible(
            page == "register"
            and bool(getattr(flow, "_reset_shortcut", False)))
        back.setEnabled(flow.can_back())
        check.setVisible(bool(getattr(flow, "_uncertain_registration", False)))
        heading.setText({
            "login": "Log in",
            "register": "Create account",
            "verify": "Verify your email",
            "recovery_request": "Reset password",
            "recovery_confirm": "Choose a new password",
            "home": "Your account",
            "delete": "Delete account",
        }.get(page, "Account"))
        focus = payload.get("focus", "")
        widget = inputs.get({
            "identity": "identity", "username": "username",
            "email": "recovery_email", "code": "code",
            "delete_username": "delete_username",
        }.get(focus, ""))
        if widget is not None:
            try:
                widget.setFocus()
            except Exception:
                pass
        _refresh_primary_text(page)

    def _page_index_for_input(key: str) -> int:
        return {
            "password": 0, "register_password": 1, "code": 2,
            "recovery_code": 4, "new_password": 4, "delete_password": 6,
        }.get(key, -1)

    def _refresh_primary_text(page: str) -> None:
        text = {"login": "Log in", "register": "Register",
                "verify": "Verify", "recovery_request": "Send reset code",
                "recovery_confirm": "Reset password",
                "delete": "Delete account and local progress"
                if inputs["delete_local"].isChecked() else "Delete account",
                }.get(page, "Continue")
        primary = primary_by_page.get(page)
        if primary is not None:
            primary.setText(text)

    def _update_delete_confirm() -> None:
        """The confirm button unlocks only for the exact display username and
        a nonempty password. The controller revalidates at dispatch."""
        authoritative = (flow.delete_username()
                         if hasattr(flow, "delete_username") else "")
        typed = inputs["delete_username"].text()
        ok = bool(authoritative) and typed == authoritative \
            and bool(inputs["delete_password"].text())
        delete_confirm.setEnabled(bool(ok) and not flow.busy)
        _refresh_primary_text(flow.page)

    def _update_resend() -> None:
        remaining = flow.resend_available_in() if hasattr(flow, "resend_available_in") else 0
        for button in (resend, recovery_resend):
            button.setEnabled(remaining <= 0)
            button.setText("Resend code" if remaining <= 0
                           else f"Resend in {remaining}s")

    def emit(name: str, payload: Dict[str, Any]) -> None:
        if name == "page":
            _apply_page(payload)
        elif name == "status":
            status.setText(payload.get("message", "") or "")
        elif name == "error":
            message = payload.get("message", "") or ""
            error.setText(message)
        elif name == "busy":
            busy = bool(payload.get("busy"))
            for button in (login_primary, register_primary, verify_primary,
                           recovery_request_primary, recovery_primary,
                           go_register, forgot, resend, recovery_resend,
                           reset_shortcut, delete_check, delete_retry_local):
                button.setEnabled(not busy)
            inputs["verify_email"].setEnabled(not busy)
            inputs["delete_username"].setEnabled(not busy)
            inputs["delete_password"].setEnabled(not busy)
            inputs["delete_local"].setEnabled(not busy)
            check.setEnabled(not busy)
            back.setEnabled(not busy and flow.can_back())
            _update_delete_confirm()
            if not busy:
                _update_resend()
        elif name == "resend":
            _update_resend()
        elif name == "reset_shortcut":
            reset_shortcut.setVisible(bool(payload.get("visible"))
                                      and flow.page == "register")
        elif name == "delete_started":
            delete_check.setVisible(False)
            delete_retry_local.setVisible(False)
            delete_local_warning.setVisible(
                bool(inputs["delete_local"].isChecked()))
        elif name == "delete_result":
            result_status = str(payload.get("status", "") or "")
            can_check = bool(payload.get("can_check"))
            delete_check.setVisible(can_check
                                    and result_status != "cleanup_incomplete")
            delete_retry_local.setVisible(
                result_status == "cleanup_incomplete")
            delete_local_warning.setVisible(
                bool(inputs["delete_local"].isChecked())
                and result_status not in ("exists",))
        elif name == "uncertain":
            check.setVisible(True)
        elif name == "close":
            dlg._account_result = payload.get("result", {"ok": True})
            dlg.accept()

    flow.ctx.emit = emit  # presenter sink on the context

    # -------------------------------------------------------------- actions
    def _primary():
        page = flow.page
        if page == "login":
            flow.submit_login(inputs["identity"].text(),
                              inputs["password"].text(),
                              bool(inputs["remember"].isChecked()))
        elif page == "register":
            flow.submit_register(inputs["username"].text(),
                                 inputs["email"].text(),
                                 inputs["register_password"].text())
        elif page == "verify":
            flow.submit_verify(inputs["code"].text(),
                               inputs["verify_email"].text())
        elif page == "recovery_request":
            flow.request_recovery(inputs["recovery_email"].text())
        elif page == "recovery_confirm":
            flow.submit_reset(inputs["recovery_code"].text(),
                              inputs["new_password"].text())
        elif page == "delete":
            flow.submit_delete(inputs["delete_password"].text(),
                               inputs["delete_username"].text(),
                               bool(inputs["delete_local"].isChecked()))

    def _resend():
        # The verify page resends the signup code to its own recipient; the
        # reset page resends recovery. Widgets are read on the main thread
        # and the controller captures the recipient before any worker runs.
        if flow.page == "verify":
            flow.resend_code(inputs["verify_email"].text())
        else:
            flow.resend_code()

    for button in (login_primary, register_primary, verify_primary,
                   recovery_request_primary, recovery_primary):
        button.clicked.connect(_primary)
    back.clicked.connect(flow.back)
    cancel.clicked.connect(flow.cancel)
    check.clicked.connect(flow.check_status)
    resend.clicked.connect(_resend)
    recovery_resend.clicked.connect(_resend)
    forgot.clicked.connect(lambda: flow.start("recovery_request"))
    reset_shortcut.clicked.connect(
        lambda: flow.open_recovery_with(flow._register_fields.get("email", "")))
    home_logout.clicked.connect(flow.logout)
    delete_open.clicked.connect(flow.open_delete)
    delete_confirm.clicked.connect(_primary)
    delete_check.clicked.connect(flow.check_deletion)
    delete_retry_local.clicked.connect(flow.retry_local_cleanup)

    def _local_toggled(checked: bool) -> None:
        delete_local_warning.setVisible(bool(checked)
                                        and not flow.busy)
        _refresh_primary_text(flow.page)

    delete_local.toggled.connect(_local_toggled)
    inputs["delete_username"].textChanged.connect(
        lambda _text: _update_delete_confirm())
    inputs["delete_password"].textChanged.connect(
        lambda _text: _update_delete_confirm())

    def _to_register():
        flow.start("register")

    go_register.clicked.connect(_to_register)

    tick = QTimer(dlg)
    tick.setObjectName("ankiscape-account-cooldown")
    tick.setInterval(1000)
    tick.timeout.connect(_update_resend)
    tick.start()

    def _closing(event):
        if not flow.closed:
            flow.cancel()
        event.accept()

    dlg.closeEvent = _closing
    dlg._account_flow = flow
    dlg._account_pages = pages
    dlg._account_inputs = inputs
    dlg._account_primary = primary_by_page
    dlg._account_runner = runner
    return dlg


def open_account_window(parent, flow, *, runner: Optional[Callable] = None,
                        start_page: str = "login") -> Dict[str, Any]:
    """Show the one account window modally and return its result dict.

    A second open while one is visible is refused: there is exactly one
    account window per profile, and the existing one keeps focus."""
    from aqt.qt import QApplication
    for widget in QApplication.topLevelWidgets():
        if widget.objectName() == "ankiscape-account-window" \
                and widget.isVisible():
            try:
                widget.raise_()
                widget.activateWindow()
            except Exception:
                pass
            return {"ok": False, "already_open": True}
    dlg = build_account_window(parent, flow, runner=runner)
    dlg._account_result = {"ok": False, "cancelled": True}
    flow.start(start_page)
    dlg.exec()
    result = getattr(dlg, "_account_result", {"ok": False, "cancelled": True})
    try:
        dlg.deleteLater()
    except Exception:
        pass
    return result
