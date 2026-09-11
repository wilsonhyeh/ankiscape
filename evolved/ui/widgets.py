# evolved/ui/widgets.py - Reusable Evolved widgets (browser/pixel look).
"""Stone panels, icon rail buttons, item slots, skill tiles, XP bars and
typography helpers. Every widget carries an explicit object name for the dev
test driver, a tooltip, and an accessible name. Qt is imported at this module
load; the module itself is only imported by the lazy UI shell, so pure tests
never touch it.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

try:  # pragma: no cover - exercised only inside Anki
    from aqt.qt import (QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
                        QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
                        QWidget)
    HAS_QT = True
except Exception:  # headless import: stubs so the module stays importable
    HAS_QT = False

    class QFrame:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QWidget:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QLabel:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QPushButton:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QToolButton:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QProgressBar:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QVBoxLayout:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QHBoxLayout:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QGridLayout:  # type: ignore
        def __init__(self, *a, **k):
            pass

    class QSizePolicy:  # type: ignore
        Expanding = Preferred = Fixed = 0

        def __init__(self, *a, **k):
            pass


from . import theme
from ..assets import (display_icon, font_path, manifest_revision,
                      nav_icon_path, skill_icon_path)

_ICON_CACHE_MAX_ENTRIES = 256
_ICON_CACHE_MAX_BYTES = 16 * 1024 * 1024  # estimated decoded pixel storage
_REGISTERED_FONTS = set()


class IconCache:
    """GUI-thread-only bounded LRU of decoded/scaled pixmaps.

    The key carries the absolute asset path, the asset-manifest revision, the
    logical size and the device-pixel ratio, so theme/asset/DPI changes can
    never serve a stale bitmap. Misses and fallbacks are cached too (None
    values), so a missing file is not re-probed on every refresh. The
    decode function is injectable, keeping the cache itself pure/testable.
    """

    def __init__(self, max_entries: int = _ICON_CACHE_MAX_ENTRIES,
                 max_bytes: int = _ICON_CACHE_MAX_BYTES, decode=None):
        from collections import OrderedDict

        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self._decode = decode
        self._data: "OrderedDict" = OrderedDict()
        self._bytes = 0
        self.stats = {"hits": 0, "misses": 0, "decodes": 0, "errors": 0,
                      "evictions": 0, "fallbacks": 0}

    def _size_of(self, value) -> int:
        try:
            if value is None:
                return 0
            width = int(value.width())
            height = int(value.height())
            return max(0, width * height * 4)
        except Exception:
            return 0

    def get(self, key):
        if key in self._data:
            self.stats["hits"] += 1
            self._data.move_to_end(key)
            return self._data[key]
        self.stats["misses"] += 1
        value = None
        if self._decode is not None:
            try:
                value = self._decode(key)
            except Exception:
                self.stats["errors"] += 1
                value = None
        else:
            self.stats["errors"] += 1
        self.stats["decodes"] += 1
        if value is None:
            self.stats["fallbacks"] += 1
        self._store(key, value)
        return value

    def _store(self, key, value) -> None:
        size = self._size_of(value)
        if size > self.max_bytes:
            return  # larger than the whole budget: never cache
        self._data[key] = value
        self._bytes += size
        self._data.move_to_end(key)
        while self._data and (len(self._data) > self.max_entries
                              or self._bytes > self.max_bytes):
            _old_key, old_value = self._data.popitem(last=False)
            self._bytes -= self._size_of(old_value)
            self.stats["evictions"] += 1

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._data)

    def bytes_used(self) -> int:
        return self._bytes


def _device_dpr() -> float:
    try:
        from aqt.qt import QApplication
        app = QApplication.instance()
        if app is not None:
            screen = app.primaryScreen()
            if screen is not None:
                return max(1.0, float(screen.devicePixelRatio()))
    except Exception:
        pass
    return 1.0


def _decode_icon_pixmap(key):
    """Decode one cache key (see IconCache) into a scaled QPixmap or None."""
    path, _revision, size, dpr = key
    try:
        from aqt.qt import QPixmap, Qt
        pix = QPixmap(path)
        if pix.isNull():
            return None
        target = max(1, int(round(int(size) * float(dpr))))
        scaled = pix.scaled(target, target,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.FastTransformation)
        try:
            scaled.setDevicePixelRatio(float(dpr))
        except Exception:
            pass
        return scaled
    except Exception:
        return None


_ICON_CACHE = IconCache(decode=_decode_icon_pixmap)


def icon_cache_stats() -> dict:
    stats = dict(_ICON_CACHE.stats)
    stats["entries"] = len(_ICON_CACHE)
    stats["bytes"] = _ICON_CACHE.bytes_used()
    stats["max_entries"] = _ICON_CACHE.max_entries
    stats["max_bytes"] = _ICON_CACHE.max_bytes
    return stats


def clear_icon_cache() -> None:
    _ICON_CACHE.clear()


def icon_pixmap(path: str, size: int, dpr: Optional[float] = None):
    """Integer-scaled, HiDPI-aware QPixmap, or None when the asset is missing.

    Aspect ratio and alpha are preserved; scaling is nearest-neighbor so
    pixel art stays crisp at every actual slot size. Results (including
    misses) come from a bounded GUI-thread LRU keyed by path + manifest
    revision + logical size + device-pixel ratio.
    """
    if not HAS_QT or not path:
        return None
    try:
        import os
        factor = float(dpr) if dpr else _device_dpr()
        key = (os.path.abspath(str(path)), manifest_revision(), int(size),
               round(factor, 2))
        return _ICON_CACHE.get(key)
    except Exception:
        return None


def body_label(text: str = "", *, wrap: bool = False,
               object_name: str = "") -> "QLabel":
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    if wrap:
        label.setWordWrap(True)
    return label


def display_label(text: str = "", *, object_name: str = "ankiscape-display",
                  size_delta: int = 0) -> "QLabel":
    label = QLabel(text)
    label.setObjectName(object_name)
    if HAS_QT:
        font = label.font()
        base = theme.scaled(theme.BODY_PX + size_delta)
        font.setPointSize(max(8, base - 2))
        font.setBold(True)
        try:
            font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 0.8)
        except Exception:
            pass
        label.setFont(font)
    label.setAccessibleName(text)
    return label


def heading_label(text: str) -> "QLabel":
    return display_label(text, object_name="ankiscape-heading", size_delta=2)


def muted_label(text: str = "", *, wrap: bool = False) -> "QLabel":
    label = body_label(text, wrap=wrap, object_name="ankiscape-muted")
    return label


def error_label(text: str = "") -> "QLabel":
    label = body_label(text, wrap=True, object_name="ankiscape-error")
    return label


def success_label(text: str = "") -> "QLabel":
    return body_label(text, object_name="ankiscape-success")


def apply_theme(widget, scale: int = theme.DEFAULT_SCALE) -> None:
    """Load the display font once and apply the palette QSS."""
    s = theme.clamp_scale(scale)
    if HAS_QT:
        try:
            from aqt.qt import QFontDatabase
            path = font_path()
            if path and path not in _REGISTERED_FONTS:
                QFontDatabase.addApplicationFont(path)
                _REGISTERED_FONTS.add(path)
        except Exception:
            pass
    try:
        widget.setStyleSheet(theme.build_stylesheet(s))
    except Exception:
        pass


def clear_layout(layout) -> None:
    if layout is None:
        return
    try:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
            else:
                child = item.layout() if item is not None else None
                if child is not None:
                    clear_layout(child)
    except Exception:
        pass


def _strong_focus(widget) -> None:
    if not HAS_QT:
        return
    try:
        from aqt.qt import Qt
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    except Exception:
        pass


class StonePanel(QFrame):
    """Beveled panel. `raised=True` selects the lighter surface."""

    def __init__(self, parent=None, *, raised: bool = False,
                 object_name: str = ""):
        super().__init__(parent)
        self.setObjectName(object_name or
                           ("ankiscape-raised-panel" if raised
                            else "ankiscape-stone-panel"))
        self.setFrameShape(QFrame.Shape.StyledPanel if HAS_QT else 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.CONTENT_INSET // 2,
                                  theme.CONTENT_INSET // 2,
                                  theme.CONTENT_INSET // 2,
                                  theme.CONTENT_INSET // 2)
        layout.setSpacing(theme.SPACING // 2)
        self.body = layout

    def add(self, widget) -> None:
        self.body.addWidget(widget)


class XpBar(QProgressBar):
    """XP progress with an explicit level label; never a full-account meter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ankiscape-xp-bar")
        self.setTextVisible(True)
        self.setRange(0, 1000)

    def set_xp(self, xp_micro: int, thresholds, level: int) -> None:
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            lvl = 1
        if lvl >= 99:
            self.setValue(1000)
            self.setFormat("Level 99 — maxed")
            return
        frac = theme.xp_progress(int(xp_micro or 0), thresholds, lvl)
        self.setValue(int(round(frac * 1000)))
        try:
            idx = max(0, lvl - 1)
            base = int(thresholds[idx]) * 1_000_000
            nxt = int(thresholds[lvl]) * 1_000_000
            current = max(0, int(xp_micro or 0))
            remaining = max(0, nxt - current)
            self.setFormat(
                f"Lv {lvl} — {remaining // 1_000_000:,} XP to {lvl + 1}")
            _ = base
        except (IndexError, TypeError, ValueError):
            self.setFormat(f"Lv {lvl}")


class ItemSlot(QFrame):
    """64px cell holding 48px art, qty overlay, lock badge, focus border.

    Locked items stay keyboard-selectable for preview; the caller decides
    whether a Train action is available. State is conveyed by text badges and
    borders, never by color/dimming alone.
    """

    def __init__(self, parent=None, *, scale: int = theme.DEFAULT_SCALE):
        super().__init__(parent)
        self.setObjectName("ankiscape-item-slot")
        self._scale = theme.clamp_scale(scale)
        cell = theme.scaled(theme.ITEM_CELL, self._scale)
        self.setFixedSize(cell, cell)
        _strong_focus(self)
        grid = QGridLayout(self)
        grid.setContentsMargins(2, 2, 2, 2)
        self._icon = QLabel(self)
        self._icon.setAlignment(_align_center())
        grid.addWidget(self._icon, 0, 0, 2, 2)
        self._qty = QLabel("", self)
        self._qty.setObjectName("ankiscape-item-qty")
        self._qty.setAlignment(_align_bottom_right() if HAS_QT else 0)
        grid.addWidget(self._qty, 1, 0, 1, 2,
                       _grid_align_bottom_right() if HAS_QT else 0)
        self._badge = QLabel("", self)
        self._badge.setObjectName("ankiscape-badge")
        self._badge.setAlignment(_align_top_left() if HAS_QT else 0)
        grid.addWidget(self._badge, 0, 0, 1, 2, _grid_align_top_left() if HAS_QT else 0)
        self._display = ""
        self._state = "ready"
        self.clicked: Optional[Callable[[str], None]] = None
        self._set_state("ready")

    def _set_state(self, state: str) -> None:
        self._state = state
        try:
            self.setProperty("state", state)
            style = self.style()
            if style is not None:
                style.unpolish(self)
                style.polish(self)
        except Exception:
            pass

    def set_item(self, display: str, qty: int = 0, *,
                 selected: bool = False, locked: bool = False,
                 paused: bool = False, level_req: int = 0,
                 produced: bool = False) -> None:
        self._display = str(display)
        art = theme.scaled(theme.ITEM_ART, self._scale)
        pix = icon_pixmap(display_icon(self._display), art)
        if pix is not None:
            self._icon.setPixmap(pix)
            self._icon.setToolTip(self._display)
        else:
            self._icon.setText(self._display[:2].upper())
        self._qty.setText(f"{int(qty):,}" if int(qty) > 1 else "")
        if locked:
            self._badge.setText(f"Lv {int(level_req)}" if level_req else "Locked")
            state = "locked"
        elif paused:
            self._badge.setText("Paused")
            state = "paused"
        elif selected:
            self._badge.setText("Training" if not produced else "Output")
            state = "selected"
        else:
            self._badge.setText("Output" if produced else "")
            state = "ready"
        self._set_state(state)
        name = self._display
        if locked:
            name += f" (locked, requires level {int(level_req)})"
        self.setAccessibleName(name)
        self.setToolTip(name)

    def keyPressEvent(self, event):  # noqa: N802 (Qt API)
        try:
            from aqt.qt import Qt
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                               Qt.Key.Key_Space):
                self._activate()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        super().mousePressEvent(event)
        self._activate()

    def _activate(self) -> None:
        if callable(self.clicked):
            try:
                self.clicked(self._display)
            except Exception:
                pass


class SkillTile(QPushButton):
    """Six-skill grid tile: icon + name + level + progress text."""

    def __init__(self, skill: str, parent=None, *,
                 scale: int = theme.DEFAULT_SCALE):
        super().__init__(parent)
        self.skill = str(skill).lower()
        self.setObjectName(f"ankiscape-skill-tile-{self.skill}")
        self.setCheckable(True)
        art = theme.scaled(40, scale)
        pix = icon_pixmap(skill_icon_path(self.skill), art)
        if pix is not None:
            self.setIcon(_qicon(pix))
            try:
                from aqt.qt import QSize
                self.setIconSize(QSize(art, art))
            except Exception:
                pass
        self.setToolTip(self.skill.title())
        self.setAccessibleName(f"{self.skill.title()} skill tile")
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def set_state(self, level: int, xp_micro: int, thresholds,
                  trained: bool = False) -> None:
        title = self.skill.title()
        mark = " (training)" if trained else ""
        self.setText(f"{title}\nLevel {int(level)}{mark}")
        try:
            lvl = int(level)
            if lvl >= 99:
                xp_text = "maxed"
            else:
                nxt = int(thresholds[lvl]) * 1_000_000
                have = max(0, int(xp_micro or 0))
                xp_text = f"{max(0, nxt - have) // 1_000_000:,} XP to go"
        except (IndexError, TypeError, ValueError):
            xp_text = ""
        self.setToolTip(f"{title} — level {int(level)}" +
                        (f" — {xp_text}" if xp_text else ""))
        self.setAccessibleName(
            f"{title}, level {int(level)}" + (", currently training" if trained else ""))


class RailButton(QToolButton):
    """Left icon-rail button with a visible label and tooltip."""

    def __init__(self, section: str, label: str, parent=None, *,
                 scale: int = theme.DEFAULT_SCALE):
        super().__init__(parent)
        self.section = str(section)
        self.setObjectName(f"ankiscape-rail-button")
        self.setProperty("section", self.section)
        self.setText(label)
        self.setCheckable(True)
        self.setAutoRaise(False)
        self.setToolButtonStyle(_text_under_icon() if HAS_QT else 0)
        art = theme.scaled(24, scale)
        pix = icon_pixmap(nav_icon_path(section), art)
        if pix is not None:
            self.setIcon(_qicon(pix))
            try:
                from aqt.qt import QSize
                self.setIconSize(QSize(art, art))
            except Exception:
                pass
        else:
            self.setToolButtonStyle(0)
        self.setToolTip(label)
        self.setAccessibleName(label)
        self.setFixedWidth(theme.scaled(theme.RAIL_WIDTH - 4, scale))


def set_tab_order(widgets: Iterable) -> None:
    items = [w for w in widgets if w is not None]
    for idx in range(len(items) - 1):
        try:
            QWidget.setTabOrder(items[idx], items[idx + 1])
        except Exception:
            pass


def _qicon(pix):
    try:
        from aqt.qt import QIcon
        return QIcon(pix)
    except Exception:
        return pix


def _align_center():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignCenter
    except Exception:
        return 0


def _align_top_left():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
    except Exception:
        return 0


def _align_bottom_right():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
    except Exception:
        return 0


def _grid_align_top_left():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
    except Exception:
        return 0


def _grid_align_bottom_right():
    try:
        from aqt.qt import Qt
        return Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
    except Exception:
        return 0


def _text_under_icon():
    try:
        from aqt.qt import Qt
        return Qt.ToolButtonStyle.ToolButtonTextUnderIcon
    except Exception:
        return 0
