# evolved/ui/qt_hud.py - Qt HUD widget bound to HudOwner (lazy Qt).
"""Frameless QLabel without stealing keyboard focus; hidden outside review and
on profile close. The owner coalesces updates into one pending pump (wired to
a single-shot timer, not polling). Never intercepts review keys/clicks
(WA_TransparentForMouseEvents + NoFocus).
"""
from __future__ import annotations

from typing import Any


def ensure_qt_hud(owner, mw) -> Any:
    """Create (or reuse) the Qt HUD label for this profile. Returns widget."""
    from aqt.qt import QLabel, Qt, QTimer

    existing = getattr(mw, "ankiscape_evolved_hud", None)
    if existing is not None:
        return existing
    label = QLabel(mw)
    label.setObjectName("ankiscape-evolved-hud")
    flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
    label.setWindowFlags(flags)
    label.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _apply(state) -> None:
        label.setText(f"{state.skill.title()} {state.level} — "
                      f"total {state.total_level}"
                      + (f" — {state.adjustments}" if state.adjustments else ""))
        label.adjustSize()
        label.setVisible(bool(state.visible))

    owner._apply = _apply  # bind paint; owner.release() unbinds on close
    mw.ankiscape_evolved_hud = label
    return label


def request_qt_update(owner, mw, **fields) -> None:
    """Coalesced update: mark pending and pump once on the event loop."""
    from aqt.qt import QTimer

    owner.request_update(**fields)
    QTimer.singleShot(0, owner.pump)


def release_qt_hud(mw) -> None:
    widget = getattr(mw, "ankiscape_evolved_hud", None)
    if widget is not None:
        try:
            widget.hide()
            widget.deleteLater()
        except Exception:
            pass
    try:
        delattr(mw, "ankiscape_evolved_hud")
    except AttributeError:
        pass
