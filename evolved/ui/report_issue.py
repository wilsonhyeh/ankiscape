# evolved/ui/report_issue.py - One public-report dialog (no automatic sends).
"""Report a bug: short summary, what happened, expected behavior,
reproduction steps and an optional allowlisted diagnostic preview.

Contract:
  - "Reports are public on GitHub. Review the details before continuing."
  - Opening GitHub requires a user action and never submits the issue.
  - Cancel and preview send zero network requests; the add-on never uploads
    anything and never creates an issue itself.
  - Clipboard fallback works offline; typed text survives a browser failure.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple


def show_report_issue(parent, deps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build and run the dialog. deps:
      collect_diagnostics() -> allowlisted payload dict
      copy_text(text) -> bool
      open_url(url) -> bool
    Returns {"action": "copy"|"open"|"cancel", ...}."""
    deps = dict(deps or {})
    from aqt.qt import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                        QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                        QVBoxLayout, QWidget)
    from .. import diagnostics as diag
    from .widgets import body_label, display_label, icon_pixmap
    from ..assets import slot_icon_path

    dialog = QDialog(parent)
    dialog.setWindowTitle("Report a bug")
    dialog.setObjectName("ankiscape-report-issue")
    layout = QVBoxLayout(dialog)
    layout.setSpacing(8)

    header = QWidget()
    from aqt.qt import QHBoxLayout
    header_row = QHBoxLayout(header)
    header_row.setContentsMargins(0, 0, 0, 0)
    icon = QLabel()
    icon.setFixedSize(24, 24)
    pix = icon_pixmap(slot_icon_path("support.report"), 24)
    if pix is not None:
        icon.setPixmap(pix)
        icon.setAccessibleName("Report a bug")
    header_row.addWidget(icon)
    header_row.addWidget(display_label("Report a bug"), 1)
    layout.addWidget(header)

    warning = body_label(
        "Reports are public on GitHub. Review the details before continuing. "
        "Do not include collection, account or card content.",
        wrap=True)
    warning.setObjectName("ankiscape-report-warning")
    layout.addWidget(warning)

    form = QFormLayout()
    summary = QLineEdit()
    summary.setObjectName("ankiscape-report-summary")
    summary.setPlaceholderText("One line: what broke?")
    happened = QPlainTextEdit()
    happened.setObjectName("ankiscape-report-happened")
    happened.setPlaceholderText("What happened?")
    expected = QPlainTextEdit()
    expected.setObjectName("ankiscape-report-expected")
    expected.setPlaceholderText("What did you expect?")
    steps = QPlainTextEdit()
    steps.setObjectName("ankiscape-report-steps")
    steps.setPlaceholderText("1. ...\n2. ...")
    form.addRow("Summary:", summary)
    form.addRow("What happened:", happened)
    form.addRow("Expected:", expected)
    form.addRow("Steps to reproduce:", steps)
    layout.addLayout(form)

    include_diag = QCheckBox("Include allowlisted diagnostics "
                             "(no account or personal data)")
    include_diag.setObjectName("ankiscape-report-include-diagnostics")
    include_diag.setChecked(True)
    layout.addWidget(include_diag)

    preview = QPlainTextEdit()
    preview.setObjectName("ankiscape-report-preview")
    preview.setReadOnly(True)
    preview.setMinimumHeight(120)
    layout.addWidget(preview)

    copy_btn = QPushButton("Copy report")
    copy_btn.setObjectName("ankiscape-report-copy")
    open_btn = QPushButton("Open GitHub")
    open_btn.setObjectName("ankiscape-report-open")
    open_btn.setProperty("class", "primary")
    cancel_btn = QPushButton("Cancel")
    cancel_btn.setObjectName("ankiscape-report-cancel")
    buttons = QDialogButtonBox()
    buttons.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
    buttons.addButton(open_btn, QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
    layout.addWidget(buttons)

    status = body_label("")
    status.setObjectName("ankiscape-report-status")
    layout.addWidget(status)
    result: Dict[str, Any] = {"action": "cancel", "report": ""}

    def _diagnostics_payload() -> Dict[str, Any]:
        collector = deps.get("collect_diagnostics")
        if not callable(collector) or not include_diag.isChecked():
            return {}
        try:
            payload = collector()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _body() -> str:
        payload = _diagnostics_payload()
        return diag.report_body(
            summary.text(), happened.toPlainText(), expected.toPlainText(),
            steps.toPlainText(),
            diag.render_text(payload) if payload else "")

    def _refresh_preview() -> None:
        try:
            preview.setPlainText(_body())
        except Exception:
            pass

    def _copy() -> bool:
        copier = deps.get("copy_text")
        text = _body()
        result["report"] = text
        ok = False
        if callable(copier):
            try:
                ok = bool(copier(text))
            except Exception:
                ok = False
        status.setText("Copied. Paste it into the issue."
                       if ok else
                       "Couldn't copy automatically — select the preview "
                       "text and copy it manually.")
        return ok

    def _open() -> None:
        body = _body()
        result["report"] = body
        url, full_body = diag.issue_url(title=summary.text(), body=body)
        opener = deps.get("open_url")
        if url is not None and callable(opener):
            try:
                if opener(url):
                    result["action"] = "open"
                    result["url"] = url
                    dialog.accept()
                    return
            except Exception:
                pass
        # Browser failed or the report is too long: copy the full report and
        # open the correct blank template with a paste instruction. Typed
        # text is retained in the dialog.
        _copy()
        if url is None:
            opener = deps.get("open_url")
            blank, _ = diag.issue_url(title=summary.text(), body="",
                                      template=diag.TEMPLATE_BUG)
            if callable(opener) and blank:
                try:
                    opener(blank)
                except Exception:
                    pass
            status.setText("Report copied — paste it into the GitHub issue, "
                           "then fill the form. Your text is still here.")
        else:
            status.setText("Report copied — open a new GitHub issue and "
                           "paste it. Your text is still here.")
        result["action"] = "copy"

    def _cancel() -> None:
        result["action"] = "cancel"
        dialog.reject()

    summary.textChanged.connect(lambda _t: _refresh_preview())
    happened.textChanged.connect(_refresh_preview)
    expected.textChanged.connect(_refresh_preview)
    steps.textChanged.connect(_refresh_preview)
    include_diag.toggled.connect(lambda _t: _refresh_preview())
    copy_btn.clicked.connect(lambda: _copy())
    open_btn.clicked.connect(_open)
    cancel_btn.clicked.connect(_cancel)
    _refresh_preview()

    try:
        dialog.resize(560, 620)
    except Exception:
        pass
    dialog.exec()
    return result
