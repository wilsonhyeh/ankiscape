# Account forms submit while alive; errors stay beside editable fields.
from __future__ import annotations


def _form(parent, title, name, fields):
    from aqt.qt import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                        QLineEdit, QVBoxLayout)
    from .widgets import apply_theme
    dlg = QDialog(parent)
    dlg.setWindowTitle('AnkiScape — ' + title)
    dlg.setObjectName(name)
    apply_theme(dlg)
    layout = QVBoxLayout(dlg)
    form = QFormLayout()
    inputs = {}
    for key, label, object_name, secret, limit in fields:
        edit = QLineEdit()
        edit.setObjectName(object_name)
        edit.setMaxLength(limit)
        if secret:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(label, edit)
        inputs[key] = edit
    layout.addLayout(form)
    error = QLabel('')
    error.setWordWrap(True)
    error.setObjectName('ankiscape-error-label')
    layout.addWidget(error)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    buttons.rejected.connect(dlg.reject)
    return dlg, layout, inputs, error, buttons


def _run(dlg, inputs, error, buttons, submit, *, hooks=None, extra=None):
    from aqt.qt import QDialog, QDialogButtonBox
    state = {'result': None, 'busy': False, 'closed': False}
    def values():
        return {key: edit.text() for key, edit in inputs.items()}
    def complete(result):
        if state['closed']:
            return
        state['busy'] = False
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        if isinstance(result, dict) and result.get('ok'):
            state['result'] = result
            dlg.accept()
        else:
            error.setText((result or {}).get('error', 'Could not complete the request. Try again.'))
    def send():
        if state['busy']:
            return
        payload = values()
        state['busy'] = True
        error.setText('Working…')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        # Account callbacks also update Anki state. Keep them on the main
        # thread; the forms remain alive until a successful submission.
        try:
            complete(submit(payload))
        except Exception:
            complete({'ok': False, 'error': 'Could not connect. Try again.'})
    buttons.accepted.connect(send)
    if hooks is not None:
        hooks.update({'dialog': dlg, **inputs, 'error': error,
                      'submit': lambda: submit(values())})
    try:
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        return state['result'] if accepted else None
    finally:
        state['closed'] = True
        for edit in inputs.values():
            edit.clear()
        dlg.deleteLater()


def show_register_dialog(parent, on_submit, *, _test_hooks=None):
    args = _form(parent, 'Create account', 'ankiscape-register-dialog', [
        ('username', '&Username (3–20, a–z 0–9 _):', 'ankiscape-register-username', False, 20),
        ('email', '&Email (verification and recovery):', 'ankiscape-register-email', False, 320),
        ('password', '&Password (at least 6 characters):', 'ankiscape-register-password', True, 256)])
    dlg, layout, fields, error, buttons = args
    return _run(dlg, fields, error, buttons, on_submit, hooks=_test_hooks)


def show_code_dialog(parent, *, title, object_name, on_submit):
    dlg, layout, fields, error, buttons = _form(parent, title.replace('AnkiScape — ', ''), object_name, [
        ('code', '&Code from your email:', 'ankiscape-email-code', False, 32)])
    return _run(dlg, fields, error, buttons, lambda data: on_submit(data['code']))


def show_login_dialog(parent, on_submit, *, on_recovery=None, _test_hooks=None):
    from aqt.qt import QCheckBox, QPushButton
    dlg, layout, fields, error, buttons = _form(parent, 'Log in', 'ankiscape-login-dialog', [
        ('identity', '&Username or email:', 'ankiscape-login-identity', False, 320),
        ('password', '&Password:', 'ankiscape-login-password', True, 256)])
    remember = QCheckBox('Keep me signed in on this computer')
    remember.setObjectName('ankiscape-login-remember')
    remember.setChecked(True)
    layout.insertWidget(1, remember)
    forgot = QPushButton('Forgot password?')
    forgot.setObjectName('ankiscape-login-forgot')
    forgot.setEnabled(callable(on_recovery))
    def recover():
        dlg.reject()
        recovery['requested'] = True
    recovery = {'requested': False}
    forgot.clicked.connect(recover)
    layout.insertWidget(2, forgot)
    result = _run(dlg, fields, error, buttons,
                 lambda data: on_submit({**data, 'remember': remember.isChecked()}),
                 hooks=_test_hooks)
    if recovery['requested'] and callable(on_recovery):
        return on_recovery()
    return result


def show_recovery_dialog(parent, on_request, on_confirm, *, _test_hooks=None):
    # Request the code FIRST. Confirmation never sends a second code.
    from aqt.qt import QLabel
    dlg, layout, fields, error, buttons = _form(parent, 'Reset password', 'ankiscape-recovery-request', [
        ('email', '&Account email:', 'ankiscape-recovery-email', False, 320)])
    layout.insertWidget(0, QLabel('Request a code, then enter it with your new password.'))
    saved = {}
    def request(data):
        saved.update(data)
        return on_request(data['email'])
    if not _run(dlg, fields, error, buttons, request):
        return None
    dlg, layout, fields, error, buttons = _form(parent, 'Enter reset code', 'ankiscape-recovery-dialog', [
        ('code', '&Code from your email:', 'ankiscape-email-code', False, 32),
        ('new_password', '&New password (at least 6 characters):', 'ankiscape-recovery-password', True, 256)])
    return _run(dlg, fields, error, buttons,
                lambda data: on_confirm({**saved, **data}), hooks=_test_hooks)
