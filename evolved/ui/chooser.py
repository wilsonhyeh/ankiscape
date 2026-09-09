# evolved/ui/chooser.py - Classic/Evolved first-run chooser (Qt-lazy).
"""Evolved preselected. Closing/Escape selects Classic and persists that
choice. Both menus expose switching; the new choice takes effect after Anki
restart. See mode.py for headless semantics (tested without Qt).
"""
from __future__ import annotations

from typing import Callable


def show_mode_chooser(*, get_requested, set_requested, qt_dialog=None) -> str:
    """Headless-safe chooser driver.

    qt_dialog, when provided, is a zero-arg callable returning the chosen mode
    ('classic'/'evolved') or None on close/Escape. Without Qt, returns the
    stored request (default Classic) without prompting.
    """
    try:
        current = get_requested()
    except Exception:
        current = "classic"
    if qt_dialog is None:
        return current if current in ("classic", "evolved") else "classic"
    try:
        chosen = qt_dialog()
    except Exception:
        chosen = None
    if chosen not in ("classic", "evolved"):
        chosen = "classic"  # close/Escape path
    try:
        set_requested(chosen)
    except Exception:
        pass
    return chosen


def qt_chooser_dialog(parent=None) -> Callable[[], object]:
    """Build the real Qt dialog caller. Returns a thunk for show_mode_chooser."""
    def _run():
        try:
            from aqt.qt import QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout
        except Exception:
            return None
        dlg = QDialog(parent)
        try:
            from aqt.qt import Qt
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        except Exception:
            pass
        dlg.setWindowTitle("AnkiScape")
        dlg.setObjectName("ankiscape-mode-chooser")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Choose your AnkiScape mode (takes effect after restart):"))
        classic_btn = QRadioButton("Classic (existing progress)")
        classic_btn.setObjectName("ankiscape-chooser-classic")
        evolved_btn = QRadioButton("Evolved (fresh six-skill progress)")
        evolved_btn.setObjectName("ankiscape-chooser-evolved")
        # Track the choice in a cell, not by reading widget state after
        # exec(): the accepted dialog may already be half-torn-down when the
        # thunk resumes, and state reads there proved unreliable (23.10 E2E).
        choice = {"mode": "evolved"}
        try:
            evolved_btn.toggled.connect(
                lambda on: choice.update(mode="evolved") if on else None)
            classic_btn.toggled.connect(
                lambda on: choice.update(mode="classic") if on else None)
        except Exception:
            pass
        evolved_btn.setChecked(True)  # Evolved preselected
        layout.addWidget(classic_btn)
        layout.addWidget(evolved_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        try:
            result = dlg.exec()
        finally:
            try:
                dlg.deleteLater()
            except Exception:
                pass
        if result != QDialog.DialogCode.Accepted:
            return None  # close/Escape -> Classic via caller
        mode = choice.get("mode", "evolved")
        return mode if mode in ("classic", "evolved") else "evolved"

    return _run
