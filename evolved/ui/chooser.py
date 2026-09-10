# evolved/ui/chooser.py - Classic/Evolved first-run chooser (Qt-lazy).
"""Evolved preselected. Closing/Escape selects Classic and persists that
choice. Both menus expose immediate switching outside the reviewer.
See mode.py for headless semantics (tested without Qt).
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
        try:
            from .widgets import apply_theme
            apply_theme(dlg)
        except Exception:
            pass
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Choose your AnkiScape mode:"))
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


def show_upgrade_prompt(*, get_requested, set_requested, qt_dialog=None) -> str:
    """Classic-upgrade prompt driver.

    'evolved' tries Evolved in the same visit; anything else (Continue
    Classic, close, Escape, dialog failure) keeps Classic active and leaves
    the Try Evolved invitation in the Classic menu. Either choice persists
    the requested mode so the prompt is asked once.
    """
    try:
        current = get_requested()
    except Exception:
        current = "classic"
    if current not in ("classic", "evolved"):
        current = "classic"
    if qt_dialog is None:
        chosen = "classic"
    else:
        try:
            chosen = qt_dialog()
        except Exception:
            chosen = None
    if chosen not in ("classic", "evolved"):
        chosen = "classic"
    try:
        set_requested(chosen)
    except Exception:
        pass
    return chosen


def qt_upgrade_dialog(parent=None) -> Callable[[], object]:
    """Build the real Try Evolved / Continue Classic dialog caller."""
    def _run():
        try:
            from aqt.qt import (QDialog, QDialogButtonBox, QLabel, QPushButton,
                                QVBoxLayout)
        except Exception:
            return None
        dlg = QDialog(parent)
        try:
            from aqt.qt import Qt
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        except Exception:
            pass
        dlg.setWindowTitle("AnkiScape Evolved")
        dlg.setObjectName("ankiscape-upgrade-dialog")
        try:
            from .widgets import apply_theme
            apply_theme(dlg)
        except Exception:
            pass
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "AnkiScape Evolved is ready.\n\n"
            "Evolved is a fresh six-skill game with its own progress, plus "
            "optional online Hiscores and cross-device catch-up. Your Classic "
            "progress is saved and untouched — you can switch back at any "
            "time, and nothing restarts."))
        choice = {"mode": "classic"}
        try_btn = QPushButton("Try Evolved")
        try_btn.setObjectName("ankiscape-upgrade-try")
        try:
            try_btn.setStyleSheet(
                "QPushButton { border-color: #E3BE68; color: #E3BE68; "
                "font-weight: bold; }")
        except Exception:
            pass
        try_btn.clicked.connect(
            lambda: (choice.update(mode="evolved"), dlg.accept()))
        classic_btn = QPushButton("Continue Classic")
        classic_btn.setObjectName("ankiscape-upgrade-classic")
        classic_btn.clicked.connect(
            lambda: (choice.update(mode="classic"), dlg.accept()))
        layout.addWidget(try_btn)
        layout.addWidget(classic_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        result = dlg.exec()
        try:
            dlg.deleteLater()
        except Exception:
            pass
        if result != QDialog.DialogCode.Accepted:
            return None
        return choice.get("mode", "classic")
    return _run
