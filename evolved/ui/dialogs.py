# evolved/ui/dialogs.py - Qt account dialogs (lazy aqt.qt; Anki 23.10 + Qt5/Qt6).
"""Register, email-code verify, login, recovery request, recovery confirm
(code + new password), logout confirm. Bounded input, paste support (native
QLineEdit), generic error labels. Opened only from explicit menu actions."""
from __future__ import annotations

from typing import Callable, Dict, Optional


def _qt():
    from aqt.qt import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                        QLineEdit, QVBoxLayout)
    return QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout


def _style(dlg) -> None:
    try:
        from .widgets import apply_theme
        apply_theme(dlg)
    except Exception:
        pass


def _error_label(QLabel, text: str = "") -> object:
    label = QLabel(text)
    label.setObjectName("ankiscape-error-label")
    return label


def _accepted(dlg) -> bool:
    """One modal round; True on Accept. Factored so the E2E driver's popup
    watchdog and automators can observe/close each round uniformly."""
    from aqt.qt import QDialog as _QD
    try:
        return dlg.exec() == _QD.DialogCode.Accepted
    except RuntimeError:
        return False


def show_register_dialog(parent, on_submit: Callable[[Dict[str, str]], Dict],
                         *, _test_hooks: Optional[Dict] = None) -> Optional[Dict]:
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout = _qt()
    dlg = QDialog(parent)
    try:
        from aqt.qt import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    dlg.setWindowTitle("AnkiScape — Create account")
    dlg.setObjectName("ankiscape-register-dialog")
    _style(dlg)
    layout = QVBoxLayout(dlg)
    form = QFormLayout()
    username = QLineEdit()
    username.setObjectName("ankiscape-register-username")
    username.setMaxLength(20)
    email = QLineEdit()
    email.setObjectName("ankiscape-register-email")
    email.setMaxLength(320)
    password = QLineEdit()
    password.setObjectName("ankiscape-register-password")
    password.setEchoMode(QLineEdit.EchoMode.Password)
    password.setMaxLength(256)
    form.addRow("&Username (3–20, a–z 0–9 _):", username)
    form.addRow("&Email (recovery only, never shown):", email)
    form.addRow("&Password:", password)
    layout.addLayout(form)
    error = _error_label(QLabel)
    layout.addWidget(error)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    if _test_hooks is not None:
        # E2E/automation escape hatch (dev only): expose the live widgets +
        # the submit closure so the driver can fill fields and invoke the
        # exact on_submit path without fighting nested exec() modal loops.
        # Production callers never pass this (dialogs stay purely modal).
        _test_hooks.update({"dialog": dlg, "username": username,
                            "email": email, "password": password,
                            "error": error, "submit": lambda: on_submit(
                                {"username": username.text(),
                                 "email": email.text(),
                                 "password": password.text()})})
    if not _accepted(dlg):
        return None
    try:
        fields = {"username": username.text(), "email": email.text(),
                  "password": password.text()}
    except RuntimeError:
        return None
    result = on_submit(fields)
    if not isinstance(result, dict) or not result.get("ok"):
        try:
            error.setText((result or {}).get("error", "invalid username or password"))
        except RuntimeError:
            pass
        if not _accepted(dlg):  # let them read the error
            return None
        return None
    return result


def show_code_dialog(parent, *, title: str, object_name: str,
                     on_submit: Callable[[str], Dict]) -> Optional[Dict]:
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout = _qt()
    dlg = QDialog(parent)
    try:
        from aqt.qt import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    dlg.setWindowTitle(title)
    dlg.setObjectName(object_name)
    _style(dlg)
    layout = QVBoxLayout(dlg)
    form = QFormLayout()
    code = QLineEdit()
    code.setObjectName("ankiscape-email-code")
    code.setMaxLength(32)
    code.setPlaceholderText("6-digit code from your email")
    form.addRow("&Verification code:", code)
    layout.addLayout(form)
    error = _error_label(QLabel)
    layout.addWidget(error)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    if not _accepted(dlg):
        return None
    try:
        typed_code = code.text()
    except RuntimeError:
        return None
    result = on_submit(typed_code)
    if not isinstance(result, dict) or not result.get("ok"):
        try:
            error.setText((result or {}).get("error", "invalid or expired code"))
        except RuntimeError:
            pass
        return None
    return result


def show_login_dialog(parent, on_submit: Callable[[Dict[str, str]], Dict],
                        *, _test_hooks: Optional[Dict] = None) -> Optional[Dict]:
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout = _qt()
    dlg = QDialog(parent)
    try:
        from aqt.qt import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    dlg.setWindowTitle("AnkiScape — Log in")
    dlg.setObjectName("ankiscape-login-dialog")
    _style(dlg)
    layout = QVBoxLayout(dlg)
    form = QFormLayout()
    identity = QLineEdit()
    identity.setObjectName("ankiscape-login-identity")
    identity.setMaxLength(320)
    identity.setPlaceholderText("username or email")
    password = QLineEdit()
    password.setObjectName("ankiscape-login-password")
    password.setEchoMode(QLineEdit.EchoMode.Password)
    password.setMaxLength(256)
    form.addRow("&Username or email:", identity)
    form.addRow("&Password:", password)
    layout.addLayout(form)
    note = QLabel("Login is forgotten on restart — that is deliberate. "
                  "Your game stays on this computer either way.")
    note.setWordWrap(True)
    layout.addWidget(note)
    error = _error_label(QLabel)
    layout.addWidget(error)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    if _test_hooks is not None:
        _test_hooks.update({"dialog": dlg, "identity": identity,
                            "password": password, "error": error,
                            "submit": lambda: on_submit(
                                {"identity": identity.text(),
                                 "password": password.text()})})
    if not _accepted(dlg):
        return None
    try:
        fields = {"identity": identity.text(), "password": password.text()}
    except RuntimeError:
        return None
    result = on_submit(fields)
    if not isinstance(result, dict) or not result.get("ok"):
        try:
            error.setText((result or {}).get("error", "invalid username or password"))
        except RuntimeError:
            pass
        return None
    return result


def show_recovery_dialog(parent, on_request: Callable[[str], Dict],
                         on_confirm: Callable[[Dict[str, str]], Dict],
                         *, _test_hooks: Optional[Dict] = None) -> Optional[Dict]:
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout = _qt()
    dlg = QDialog(parent)
    try:
        from aqt.qt import Qt as _Qt
        dlg.setAttribute(_Qt.WidgetAttribute.WA_DeleteOnClose, True)
    except Exception:
        pass
    dlg.setWindowTitle("AnkiScape — Recover account")
    dlg.setObjectName("ankiscape-recovery-dialog")
    _style(dlg)
    layout = QVBoxLayout(dlg)
    form = QFormLayout()
    email = QLineEdit()
    email.setObjectName("ankiscape-recovery-email")
    email.setMaxLength(320)
    code = QLineEdit()
    code.setObjectName("ankiscape-email-code")
    code.setMaxLength(32)
    new_password = QLineEdit()
    new_password.setObjectName("ankiscape-recovery-password")
    new_password.setEchoMode(QLineEdit.EchoMode.Password)
    new_password.setMaxLength(256)
    form.addRow("&Email:", email)
    form.addRow("Verification &code:", code)
    form.addRow("&New password:", new_password)
    layout.addLayout(form)
    error = _error_label(QLabel)
    layout.addWidget(error)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    if _test_hooks is not None:
        def _recovery_submit():
            try:
                typed = {"email": email.text(), "code": code.text(),
                         "new_password": new_password.text()}
            except RuntimeError:
                return {"ok": False, "error": "dialog closed"}
            req = on_request(typed["email"])
            if not req.get("ok"):
                return req
            return on_confirm(typed)
        _test_hooks.update({"dialog": dlg, "email": email, "code": code,
                            "new_password": new_password, "error": error,
                            "submit": _recovery_submit})
    if not _accepted(dlg):
        return None
    try:
        typed = {"email": email.text(), "code": code.text(),
                 "new_password": new_password.text()}
    except RuntimeError:
        return None
    req = on_request(typed["email"])
    if not req.get("ok"):
        try:
            error.setText(req.get("error", "network error; try again shortly"))
        except RuntimeError:
            pass
        return None
    return on_confirm(typed)
