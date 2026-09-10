# evolved/ui/shell.py - One nonmodal Evolved window per profile (singleton).
"""Replaces the modal tab dialog with a reusable, non-blocking game window:
left icon rail (Training, Skills, Bank, Achievements, Hiscores + Settings cog),
content stack, compact status line, and tooltips/labels for every control.

Contract:
  - Exactly one instance per profile, owned by `mw.ankiscape_evolved_shell`.
  - `show_shell` opens or focuses; never constructs twice.
  - Closing hides; it never changes training and never deletes the singleton.
  - Profile close calls `release_shell` (delete + unbind) after generation
    invalidation, so stale callbacks cannot paint.
  - Screens are built lazily from their own modules and refresh from the pure
    view models; Qt imports stay inside this package.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import OBJECT_NAMES, theme
from .theme import clamp_scale
from .widgets import apply_theme, body_label, display_label, muted_label

SHELL_OBJECT_NAME = OBJECT_NAMES.get("shell", "ankiscape-evolved-shell")
SECTIONS = ("training", "skills", "bank", "achievements", "hiscores")
SECTION_LABELS = {
    "training": "Train",
    "skills": "Skills",
    "bank": "Bank",
    "achievements": "Feats",
    "hiscores": "Ranks",
    "settings": "Setup",
}
# Full names for headers/tooltips.
SECTION_FULL = {
    "training": "Training",
    "skills": "Skills",
    "bank": "Bank",
    "achievements": "Achievements",
    "hiscores": "Hiscores",
    "settings": "Settings",
}

_CLASS = None
_LAST_ERROR = {"error": ""}


def last_error() -> str:
    return str(_LAST_ERROR.get("error", ""))


def _note_error(exc) -> None:
    _LAST_ERROR["error"] = repr(exc)[:500]


def _attr(mw, name: str):
    try:
        return getattr(mw, name, None)
    except Exception:
        return None


def is_shell_open(mw) -> bool:
    widget = _attr(mw, "ankiscape_evolved_shell")
    try:
        return widget is not None and bool(widget.isVisible())
    except Exception:
        return widget is not None


def focus_shell(mw) -> bool:
    widget = _attr(mw, "ankiscape_evolved_shell")
    if widget is None:
        return False
    try:
        widget.show()
        widget.raise_()
        widget.activateWindow()
        return True
    except Exception:
        return False


def show_shell(mw, deps: Dict[str, Any]) -> bool:
    """Open (or focus) the singleton Evolved shell. Returns success."""
    widget = _attr(mw, "ankiscape_evolved_shell")
    if widget is not None:
        try:
            widget.set_deps(deps)
            widget.refresh_all(force=True)
            widget.show()
            widget.raise_()
            widget.activateWindow()
            return True
        except Exception as exc:
            _note_error(exc)
            release_shell(mw)
    try:
        shell_cls = _shell_class()
        shell = shell_cls(mw, deps)
    except Exception as exc:
        _note_error(exc)
        return False
    try:
        mw.ankiscape_evolved_shell = shell
    except Exception:
        return False
    try:
        shell.show()
        shell.raise_()
        shell.activateWindow()
    except Exception:
        pass
    return True


def release_shell(mw) -> None:
    """Profile close: drop the singleton and its widgets for good."""
    widget = _attr(mw, "ankiscape_evolved_shell")
    if widget is not None:
        try:
            widget.release()
        except Exception:
            pass
        try:
            widget.hide()
            widget.deleteLater()
        except Exception:
            pass
    try:
        delattr(mw, "ankiscape_evolved_shell")
    except Exception:
        pass


def refresh_shell(mw) -> bool:
    """Publish a projection/status change to the open window (no reopening)."""
    widget = _attr(mw, "ankiscape_evolved_shell")
    if widget is None:
        return False
    try:
        widget.refresh()
        return True
    except Exception:
        return False


def status_text(status: Optional[Dict[str, Any]]) -> str:
    """Compact status line copy (pure, testable)."""
    if not status:
        return "Local progress · not signed in"
    if not status.get("logged_in"):
        return "Local progress · not signed in"
    last_error = str(status.get("last_error") or "")
    pending = int(status.get("pending", 0) or 0)
    if last_error:
        return f"Sync problem · {last_error[:60]}"
    if pending:
        return f"Signed in · {pending} review{'s' if pending != 1 else ''} pending"
    if status.get("last_success"):
        return "Signed in · all progress synced"
    return "Signed in · never synced yet"


def _shell_class():
    global _CLASS
    if _CLASS is not None:
        return _CLASS
    from aqt.qt import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
                        QSizePolicy, QStackedWidget, QVBoxLayout, QWidget, Qt,
                        QTimer)
    from .widgets import RailButton, apply_theme

    class EvolvedShell(QWidget):
        def __init__(self, mw, deps):
            self.mw = mw
            self._deps = dict(deps or {})
            self._screens: Dict[str, Any] = {}
            self._current = "training"
            self._scale = clamp_scale(self._deps.get("ui_scale", theme.DEFAULT_SCALE))
            self._recap_summary = None
            super().__init__(mw)
            self.setObjectName(SHELL_OBJECT_NAME)
            self.setWindowTitle("AnkiScape: Evolved")
            try:
                self.setWindowFlags(Qt.WindowType.Window)
                self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            except Exception:
                pass
            self._build()
            self._apply_geometry()
            apply_theme(self, self._scale)
            self.refresh_all(force=True)

        # ------------------------------------------------------------- build

        def _build(self):
            root = QHBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)
            self._rail = self._build_rail()
            root.addWidget(self._rail, 0)
            right = QVBoxLayout()
            right.setContentsMargins(theme.CONTENT_INSET // 2,
                                     theme.CONTENT_INSET // 2,
                                     theme.CONTENT_INSET // 2,
                                     theme.CONTENT_INSET // 2)
            right.setSpacing(theme.SPACING // 2)
            header = QHBoxLayout()
            self._header = display_label("Training")
            self._header.setObjectName("ankiscape-shell-header")
            header.addWidget(self._header)
            header.addStretch(1)
            self._status = muted_label("")
            self._status.setObjectName("ankiscape-shell-status")
            self._status.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
            header.addWidget(self._status)
            right.addLayout(header)
            self._recap = self._build_recap_banner()
            right.addWidget(self._recap)
            self._stack = QStackedWidget()
            self._stack.setObjectName("ankiscape-shell-content")
            right.addWidget(self._stack, 1)
            root.addLayout(right, 1)

        def _build_rail(self):
            rail = QWidget()
            rail.setObjectName("ankiscape-icon-rail")
            layout = QVBoxLayout(rail)
            layout.setContentsMargins(4, theme.SPACING, 4, theme.SPACING)
            layout.setSpacing(theme.SPACING)
            self._rail_buttons: Dict[str, Any] = {}
            self._rail_group = []
            for section in SECTIONS:
                btn = RailButton(section, SECTION_LABELS[section], rail,
                                 scale=self._scale)
                try:
                    btn.setToolTip(SECTION_FULL[section])
                    btn.setAccessibleName(SECTION_FULL[section])
                except Exception:
                    pass
                btn.clicked.connect(lambda _c=False, s=section: self.set_section(s))
                layout.addWidget(btn)
                self._rail_buttons[section] = btn
                self._rail_group.append(btn)
            layout.addStretch(1)
            self._settings_btn = RailButton("settings", "Setup", rail,
                                            scale=self._scale)
            try:
                self._settings_btn.setToolTip("Settings")
                self._settings_btn.setAccessibleName("Settings")
            except Exception:
                pass
            self._settings_btn.clicked.connect(
                lambda _c=False: self.set_section("settings"))
            layout.addWidget(self._settings_btn)
            rail.setFixedWidth(theme.scaled(theme.RAIL_WIDTH, self._scale))
            return rail

        def _build_recap_banner(self):
            banner = QFrame(self)
            banner.setObjectName("ankiscape-raised-panel")
            banner.setVisible(False)
            layout = QHBoxLayout(banner)
            self._recap_label = body_label("", wrap=True)
            self._recap_label.setObjectName("ankiscape-recap")
            layout.addWidget(self._recap_label, 1)
            dismiss = QPushButton("Dismiss")
            dismiss.setObjectName("ankiscape-recap-dismiss")
            dismiss.clicked.connect(lambda: banner.setVisible(False))
            layout.addWidget(dismiss)
            return banner

        # ------------------------------------------------------------ layout

        def _apply_geometry(self):
            try:
                screen = QApplication.primaryScreen()
                avail = screen.availableGeometry() if screen is not None else None
            except Exception:
                avail = None
            want_w = theme.scaled(theme.WINDOW_W, self._scale)
            want_h = theme.scaled(theme.WINDOW_H, self._scale)
            if avail is not None:
                want_w = min(want_w, max(320, avail.width() - 40))
                want_h = min(want_h, max(240, avail.height() - 60))
            self.resize(want_w, want_h)
            min_w = min(theme.scaled(theme.WINDOW_MIN_W, self._scale), want_w)
            min_h = min(theme.scaled(theme.WINDOW_MIN_H, self._scale), want_h)
            self.setMinimumSize(min_w, min_h)
            if not getattr(self, "_placed", False) and avail is not None:
                # Center on the available screen once; later scale changes
                # keep the user's position.
                x = avail.x() + max(0, (avail.width() - want_w) // 2)
                y = avail.y() + max(0, (avail.height() - want_h) // 3)
                self.move(x, y)
                self._placed = True
            _ = QSizePolicy

        def apply_scale(self, scale=None):
            if scale is not None:
                self._scale = clamp_scale(scale)
            apply_theme(self, self._scale)
            self._apply_geometry()
            self._rail.setFixedWidth(theme.scaled(theme.RAIL_WIDTH, self._scale))
            for btn in self._rail_buttons.values():
                btn.setFixedWidth(theme.scaled(theme.RAIL_WIDTH - 4, self._scale))
            self._settings_btn.setFixedWidth(
                theme.scaled(theme.RAIL_WIDTH - 4, self._scale))

        # --------------------------------------------------------------- api

        def set_deps(self, deps) -> None:
            self._deps = dict(deps or {})
            for screen in self._screens.values():
                try:
                    screen.deps = self._deps
                except Exception:
                    pass

        @property
        def deps(self) -> Dict[str, Any]:
            return self._deps

        def call(self, name: str, *args, default=None):
            fn = self._deps.get(name)
            if not callable(fn):
                return default
            return fn(*args)

        def onboarding_active(self) -> bool:
            fn = self._deps.get("onboarding_active")
            try:
                return bool(fn()) if callable(fn) else False
            except Exception:
                return False

        def set_section(self, section: str) -> None:
            if self.onboarding_active() and section != "settings":
                return
            section = section if section in SECTION_LABELS else "training"
            self._current = section
            screen = self._ensure_screen(section)
            if screen is None:
                return
            index = self._stack.indexOf(screen)
            self._stack.setCurrentIndex(max(0, index))
            self._header.setText(SECTION_FULL.get(section, section.title()))
            for name, btn in self._rail_buttons.items():
                try:
                    btn.setChecked(section == name)
                except Exception:
                    pass
            try:
                self._settings_btn.setChecked(section == "settings")
            except Exception:
                pass
            try:
                screen.on_show()
            except Exception:
                pass
            self._refresh_screen(screen)

        def refresh_all(self, force: bool = False) -> None:
            onboarding = self.onboarding_active()
            try:
                self._rail.setVisible(not onboarding)
            except Exception:
                pass
            if onboarding:
                screen = self._ensure_screen("onboarding")
                if screen is not None:
                    if self._stack.indexOf(screen) < 0:
                        self._stack.addWidget(screen)
                    self._stack.setCurrentWidget(screen)
                    self._header.setText("Welcome")
                    self._refresh_screen(screen)
                self._status.setText("")
                return
            # Leave onboarding view when setup completes.
            screen = self._screens.get("onboarding")
            if screen is not None and self._stack.indexOf(screen) >= 0:
                self._stack.removeWidget(screen)
                screen.setParent(None)
                screen.deleteLater()
                self._screens.pop("onboarding", None)
            self.set_section(self._current)
            self.refresh()
            if force:
                for name in list(self._screens.keys()):
                    if name != self._current and name != "onboarding":
                        self._screens[name].invalidate()

        def refresh(self) -> None:
            """One projection read per publish; refresh the visible screen."""
            try:
                self._scale = clamp_scale(self._deps.get("ui_scale", self._scale))
            except Exception:
                pass
            try:
                status = self.call("get_status")
            except Exception:
                status = None
            try:
                self._status.setText(status_text(status))
            except Exception:
                pass
            self._publish_recap()
            screen = self._screens.get(self._current)
            if screen is not None:
                self._refresh_screen(screen)

        def show_recap(self, summary) -> None:
            self._recap_summary = summary
            self._publish_recap()

        def clear_recap(self) -> None:
            self._recap_summary = None
            self._publish_recap()

        def _publish_recap(self):
            summary = self._recap_summary
            if summary is None:
                try:
                    summary = self.call("get_session_recap")
                except Exception:
                    summary = None
            try:
                if not summary:
                    self._recap.setVisible(False)
                    return
                text = summary.get("text", "") if isinstance(summary, dict) else str(summary)
                self._recap_label.setText(text)
                self._recap.setVisible(bool(text))
            except Exception:
                pass

        def _ensure_screen(self, section: str):
            if section in self._screens:
                return self._screens[section]
            builder = _BUILDERS.get(section)
            if builder is None:
                return None
            try:
                screen = builder(self, self._deps)
            except Exception:
                return None
            if self._stack.indexOf(screen) < 0:
                self._stack.addWidget(screen)
            self._screens[section] = screen
            return screen

        def _refresh_screen(self, screen) -> None:
            try:
                screen.refresh()
            except Exception:
                pass

        def release(self) -> None:
            for screen in self._screens.values():
                try:
                    screen.release()
                except Exception:
                    pass
            self._screens = {}
            self._deps = {}

        # ------------------------------------------------------------ events

        def closeEvent(self, event):  # noqa: N802
            event.ignore()
            self.hide()

        def keyPressEvent(self, event):  # noqa: N802
            try:
                if event.key() == Qt.Key.Key_Escape:
                    self.hide()
                    return
            except Exception:
                pass
            super().keyPressEvent(event)

    _CLASS = EvolvedShell
    return _CLASS


def _build_training(shell, deps):
    from .training import build_training_screen
    return build_training_screen(shell, deps)


def _build_skills(shell, deps):
    from .skills import build_skills_screen
    return build_skills_screen(shell, deps)


def _build_bank(shell, deps):
    from .bank_view import build_bank_screen
    return build_bank_screen(shell, deps)


def _build_achievements(shell, deps):
    from .achievements import build_achievements_screen
    return build_achievements_screen(shell, deps)


def _build_hiscores(shell, deps):
    from .hiscores import build_hiscores_screen
    return build_hiscores_screen(shell, deps)


def _build_settings(shell, deps):
    from .settings import build_settings_screen
    return build_settings_screen(shell, deps)


def _build_onboarding(shell, deps):
    from .onboarding import build_onboarding_screen
    return build_onboarding_screen(shell, deps)


_BUILDERS = {
    "training": _build_training,
    "skills": _build_skills,
    "bank": _build_bank,
    "achievements": _build_achievements,
    "hiscores": _build_hiscores,
    "settings": _build_settings,
    "onboarding": _build_onboarding,
}
