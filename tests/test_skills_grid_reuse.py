"""Regression: the Skills resource grid must reuse its ItemSlots.

The defect this pins down (measured on the 3.0 endurance rig): every
``build_skills_screen`` refresh tore the grid down and minted a brand new
``ItemSlot`` for each resource.  ``clear_layout`` detaches those widgets from
the layout with ``takeAt()`` -- which, per Qt, leaves the widget's *parent*
untouched -- and then only calls ``deleteLater()``.  The old widgets therefore
stay alive as hidden children of the grid host until an event loop drains the
deferred-delete queue.  Nothing reclaims them if that queue is starved, and the
census grows without bound: 750 created slots left 1672 live ones.

The Bank screen already does this correctly (``evolved/ui/bank_view.py``):
slots are cached by display and updated in place.  The assertion below is the
same invariant at the unit level -- refreshing a screen whose item set has not
changed must not construct another widget, full stop.

Qt is stood up with a small stand-in rather than a real PyQt, because the
public suite runs on the CI build lane, which has no Qt and no Anki.  The
stand-in models the three Qt behaviours this regression turns on: ``takeAt()``
does not unparent, ``deleteLater()`` defers destruction, and ``addWidget()``
re-parents without un-hiding.  A fix that merely renamed those calls would still
fail here.  All three were cross-checked against real Qt 6.11.0, because a
stand-in is only trustworthy on the behaviours someone has actually compared.
"""
from __future__ import annotations

import sys
import types
import unittest

from evolved.data import load_rules
from evolved.ui import menu_model

# Every widget the stand-in ever constructed, in creation order.  The census
# reads this instead of patching the production classes.
_CREATED = []


class _FakeFont:
    class SpacingType:
        AbsoluteSpacing = 0

    def setPointSize(self, *a, **k):
        pass

    def setBold(self, *a, **k):
        pass

    def setLetterSpacing(self, *a, **k):
        pass


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def disconnect(self, fn=None):
        self._slots = []

    def emit(self, *a):
        for fn in list(self._slots):
            fn(*a)


class _FakeItem:
    """What a layout hands back from ``takeAt()``."""

    def __init__(self, widget=None, layout=None):
        self._widget = widget
        self._layout = layout

    def widget(self):
        return self._widget

    def layout(self):
        return self._layout


class _FakeLayout:
    def __init__(self, parent=None):
        self._host = parent
        self._items = []

    def addWidget(self, widget, *a, **k):
        if self._host is not None:
            widget.setParent(self._host)
        self._items.append(_FakeItem(widget=widget))

    def addLayout(self, layout, *a, **k):
        # Qt: a sub-layout inherits its parent layout's widget, and widgets
        # already placed in it are reparented to that widget.
        if layout._host is None and self._host is not None:
            layout._host = self._host
        if layout._host is not None:
            for item in layout._items:
                widget = item.widget()
                if widget is not None and widget.parent() is None:
                    widget.setParent(layout._host)
        self._items.append(_FakeItem(layout=layout))

    def addStretch(self, *a, **k):
        self._items.append(_FakeItem())

    def count(self):
        return len(self._items)

    def takeAt(self, index):
        # Qt: detaches the item from the layout.  The widget keeps its parent.
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def removeWidget(self, widget):
        self._items = [i for i in self._items if i.widget() is not widget]

    def setContentsMargins(self, *a, **k):
        pass

    def setSpacing(self, *a, **k):
        pass

    def setAlignment(self, *a, **k):
        pass


class _FakeGridLayout(_FakeLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cells = {}

    def addWidget(self, widget, row=0, column=0, *a, **k):
        super().addWidget(widget)
        self._cells[(row, column)] = widget

    def rowCount(self):
        return max((r for r, _ in self._cells), default=-1) + 1

    def setRowStretch(self, *a, **k):
        pass


class _FakeWidget:
    def __init__(self, parent=None):
        _CREATED.append(self)
        self._parent = None
        self._children = []
        self._name = ""
        self._props = {}
        self._hidden = False
        self._delete_pending = False
        self._text = ""
        self._enabled = True
        self._checked = False
        if parent is not None:
            self.setParent(parent)

    # -- ownership ---------------------------------------------------------
    def setParent(self, parent):
        if self._parent is not None and self in self._parent._children:
            self._parent._children.remove(self)
        self._parent = parent
        if parent is not None:
            parent._children.append(self)

    def parent(self):
        return self._parent

    def childWidgets(self):
        return list(self._children)

    def findChildren(self, kind=None):
        found = []
        for child in self._children:
            if kind is None or isinstance(child, kind):
                found.append(child)
            found.extend(child.findChildren(kind))
        return found

    # -- identity ----------------------------------------------------------
    def setObjectName(self, name):
        self._name = str(name)

    def objectName(self):
        return self._name

    def setProperty(self, key, value):
        self._props[key] = value

    def property(self, key):
        return self._props.get(key)

    # -- lifetime / visibility --------------------------------------------
    def hide(self):
        self._hidden = True

    def show(self):
        self._hidden = False

    def isVisible(self):
        return not self._hidden

    def isHidden(self):
        return self._hidden

    def deleteLater(self):
        # Qt: marks for deletion; destruction waits for the event loop.
        self._delete_pending = True

    # -- text / state ------------------------------------------------------
    def setText(self, text):
        self._text = str(text)

    def text(self):
        return self._text

    def setEnabled(self, value):
        self._enabled = bool(value)

    def isEnabled(self):
        return self._enabled

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked

    def font(self):
        return _FakeFont()

    def style(self):
        return None

    def __getattr__(self, name):
        # Everything else the widget toolkit reaches for is cosmetic.
        if name.startswith("_"):
            raise AttributeError(name)

        def _noop(*a, **k):
            return None

        return _noop


class _FakeFrame(_FakeWidget):
    class Shape:
        StyledPanel = 0


class _FakeLabel(_FakeWidget):
    def __init__(self, *args, **kwargs):
        text, parent = _split(args)
        super().__init__(parent)
        self._text = text


class _FakeButton(_FakeWidget):
    def __init__(self, *args, **kwargs):
        text, parent = _split(args)
        super().__init__(parent)
        self._text = text
        self.clicked = _FakeSignal()

    def click(self):
        self.clicked.emit()

    def setCheckable(self, value):
        self._checkable = bool(value)


class _FakeProgressBar(_FakeWidget):
    pass


class _FakeComboBox(_FakeWidget):
    pass


class _FakeScrollArea(_FakeWidget):
    def setWidgetResizable(self, value):
        self._resizable = bool(value)

    def setWidget(self, widget):
        widget.setParent(self)

    def widget(self):
        return self._children[0] if self._children else None


def _split(args):
    """Qt overloads: ``QWidget(parent)`` vs ``QLabel(text, parent)``."""
    text = ""
    parent = None
    for arg in args:
        if isinstance(arg, _FakeWidget):
            parent = arg
        elif isinstance(arg, str):
            text = arg
    return text, parent


class _FakeSizePolicy:
    class Policy:
        Expanding = 0
        Preferred = 1
        Fixed = 2


class _FakeQtNamespace:
    class AlignmentFlag:
        AlignTop = 1
        AlignLeft = 2
        AlignBottom = 4
        AlignRight = 8
        AlignCenter = 16

    class AspectRatioMode:
        KeepAspectRatio = 0

    class TransformationMode:
        FastTransformation = 0


class _FakeApplication:
    @staticmethod
    def instance():
        return None


def _fake_qt_module():
    qt = types.ModuleType("aqt.qt")
    qt.QWidget = _FakeWidget
    qt.QFrame = _FakeFrame
    qt.QLabel = _FakeLabel
    qt.QPushButton = _FakeButton
    qt.QToolButton = _FakeButton
    qt.QProgressBar = _FakeProgressBar
    qt.QComboBox = _FakeComboBox
    qt.QScrollArea = _FakeScrollArea
    qt.QVBoxLayout = _FakeLayout
    qt.QHBoxLayout = _FakeLayout
    qt.QGridLayout = _FakeGridLayout
    qt.QSizePolicy = _FakeSizePolicy
    qt.QApplication = _FakeApplication
    qt.Qt = _FakeQtNamespace
    # Deliberately no QPixmap/QPainter: icon decoding falls back to text,
    # which keeps the census about slots rather than about assets.
    return qt


class _FakeShell(_FakeWidget):
    """The host widget plus the ``shell.call`` contract the screen depends on."""

    def __init__(self, rules):
        super().__init__(None)
        self.values = {
            "get_active_skill": "mining",
            "take_preselect": None,
            "get_rules": rules,
            "get_projection": {"levels": {}, "inventory": {}, "xp_micro": {}},
            "get_selections": {},
        }

    def call(self, name, *args, **kwargs):
        if name in self.values:
            return self.values[name]
        return kwargs.get("default")

    def set_section(self, section):
        self.section = section


class SkillsGridReuseTest(unittest.TestCase):
    """A refresh must update the grid, not rebuild it."""

    @classmethod
    def setUpClass(cls):
        # Install the stand-in before evolved.ui.widgets binds its Qt names.
        cls._saved_modules = {name: module for name, module in sys.modules.items()
                              if name == "aqt" or name.startswith("aqt.")
                              or name.startswith("evolved.ui")}
        for name in list(cls._saved_modules):
            del sys.modules[name]
        sys.modules["aqt"] = types.ModuleType("aqt")
        sys.modules["aqt.qt"] = _fake_qt_module()

        from evolved.ui import skills, widgets
        cls.skills = skills
        cls.widgets = widgets
        cls.ItemSlot = widgets.ItemSlot

    @classmethod
    def tearDownClass(cls):
        for name in [n for n in sys.modules
                     if n == "aqt" or n.startswith("aqt.")
                     or n.startswith("evolved.ui")]:
            del sys.modules[name]
        sys.modules.update(cls._saved_modules)

    def setUp(self):
        del _CREATED[:]  # per-screen census
        self.rules = load_rules()
        self.shell = _FakeShell(self.rules)
        self.screen = self.skills.build_skills_screen(self.shell, {})
        self.grid_host = self._grid_host()

    def _grid_host(self):
        scrolls = self.screen.findChildren(_FakeScrollArea)
        self.assertTrue(scrolls, "the Skills screen should own a scroll area")
        hosts = scrolls[0].childWidgets()
        self.assertTrue(hosts, "the scroll area should hold the grid host")
        return hosts[0]

    # -- helpers -----------------------------------------------------------
    def _created_slots(self):
        return [w for w in _CREATED if isinstance(w, self.ItemSlot)]

    def _live_slots(self):
        return self.grid_host.findChildren(self.ItemSlot)

    def _visible_displays(self):
        return sorted(s._display for s in self._live_slots() if s.isVisible())

    def _expected_displays(self, skill):
        return sorted(e["display"] for e in
                      menu_model.resource_entries(self.rules, skill, {}, {}, ""))

    # -- the regression ----------------------------------------------------
    def test_refresh_reuses_slots_instead_of_rebuilding_the_grid(self):
        created_after_build = len(self._created_slots())
        live_after_build = len(self._live_slots())
        self.assertGreater(created_after_build, 0,
                           "the first build should populate the grid")

        for _ in range(12):
            self.screen.refresh()

        self.assertEqual(
            len(self._created_slots()), created_after_build,
            "refreshing the Skills grid with an unchanged item set constructed "
            "another ItemSlot per item; the grid must reuse its slots instead "
            "of tearing them down and relying on deleteLater()")
        self.assertEqual(
            len(self._live_slots()), live_after_build,
            "old ItemSlots accumulated under the grid host across refreshes")

    def test_refresh_keeps_the_grid_showing_the_current_items(self):
        for _ in range(3):
            self.screen.refresh()
        self.assertEqual(self._visible_displays(), self._expected_displays("mining"))

    def test_switching_skill_swaps_the_visible_grid(self):
        tiles = {w.objectName(): w for w in self.screen.findChildren(_FakeWidget)
                 if w.objectName().startswith("ankiscape-skill-tile-")}
        self.assertIn("ankiscape-skill-tile-woodcutting", tiles)
        tiles["ankiscape-skill-tile-woodcutting"].click()

        self.assertEqual(self._visible_displays(),
                         self._expected_displays("woodcutting"))
        for display in self._expected_displays("mining"):
            self.assertNotIn(display, self._visible_displays(),
                             "the previous skill's slots must not stay visible")

        # Revisiting a skill whose slots have left the grid must render them
        # again -- re-adding a widget to a layout does not un-hide it.
        tiles["ankiscape-skill-tile-mining"].click()
        self.assertEqual(self._visible_displays(),
                         self._expected_displays("mining"),
                         "returning to a skill must show its items again")
        tiles["ankiscape-skill-tile-woodcutting"].click()
        self.assertEqual(self._visible_displays(),
                         self._expected_displays("woodcutting"))
        self.assertEqual(len(self._visible_displays()),
                         len(set(self._visible_displays())),
                         "a skill must not end up with duplicate visible slots")


if __name__ == "__main__":
    unittest.main()
