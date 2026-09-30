# evolved/ui/hiscores_widgets.py - Qt pieces of the Hiscores screen.
"""Board tabs, the pinned "You" bar, the painted board rows and
the player card. Pure decisions live in `hiscores_model`; this module only
turns already-decided values into pixels and forwards clicks.

Every interactive piece is keyboard reachable and carries an accessible name;
rank movement and medals are never conveyed by colour alone.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from aqt.qt import (QFontMetrics, QFrame, QGridLayout, QHBoxLayout, QLabel,
                    QPainter, QPen, QPushButton, QRect, QSize, QSizePolicy,
                    QStyle, QStyledItemDelegate, Qt, QToolButton, QVBoxLayout,
                    QWidget, QColor)

from . import hiscores_model as hm
from . import theme
from ..assets import display_icon, skill_icon_path, slot_icon_path
from .widgets import XpBar, body_label, display_label, icon_pixmap, muted_label

ROW_H = 34
ROLE_ROW = int(Qt.ItemDataRole.UserRole) + 1

_MEDAL_COLORS = {"gold": theme.GOLD, "silver": theme.SILVER,
                 "bronze": theme.BRONZE}
# The medals are the game's own bar items.
_MEDAL_ITEMS = {"gold": "Gold bar", "silver": "Silver bar",
                "bronze": "Bronze bar"}


def board_icon_path(board: str) -> str:
    if board == hm.OVERALL:
        return slot_icon_path("hiscores.rank")
    return skill_icon_path(board)


def _repolish(widget) -> None:
    try:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
    except Exception:
        pass


def _elide(widget, text: str, width: int) -> str:
    try:
        return QFontMetrics(widget.font()).elidedText(
            text, Qt.TextElideMode.ElideRight, max(20, width))
    except Exception:
        return text


# --------------------------------------------------------------------- tabs --

def make_board_tab(board: str, label: str, on_pick: Callable[[str], None]):
    tab = QToolButton()
    tab.setProperty("hsTab", True)
    tab.setObjectName(f"ankiscape-hiscores-tab-{board}")
    tab.setCheckable(True)
    tab.setAutoExclusive(True)
    tab.setText(label)
    tab.setToolTip(f"{label} rankings")
    tab.setAccessibleName(f"{label} rankings")
    tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    tab.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    tab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    tab.ensurePolished()
    size = max(24, int(QFontMetrics(tab.font()).height() * 1.4))
    pix = icon_pixmap(board_icon_path(board), size)
    if pix is not None:
        from aqt.qt import QIcon
        tab.setIcon(QIcon(pix))
        tab.setIconSize(QSize(size, size))
    tab.clicked.connect(lambda _c=False, b=board: on_pick(b))
    return tab


# ------------------------------------------------------------------ you bar --

class YouBar(QFrame):
    """Pinned line showing where the signed-in player stands and what the
    next rank costs. Always visible when signed in, on every board."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ankiscape-hs-you")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        top = QHBoxLayout()
        self.tag = display_label("YOU", size_delta=-1)
        top.addWidget(self.tag)
        self.headline = QLabel("")
        font = self.headline.font()
        font.setBold(True)
        self.headline.setFont(font)
        top.addWidget(self.headline, 1)
        self.figures = QLabel("")
        self.figures.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.figures)
        lay.addLayout(top)
        self.goal = QLabel("")
        self.goal.setProperty("statusKind", "success")
        lay.addWidget(self.goal)

    def set_content(self, headline: str, goal: str, figures: str,
                    quiet: bool = False) -> None:
        self.headline.setText(headline)
        self.goal.setText(goal)
        self.figures.setText(figures)
        self.setProperty("state", "quiet" if quiet else "active")
        self.setAccessibleName(f"You. {headline}. {goal}. {figures}")
        _repolish(self)


# ------------------------------------------------------------- board rows --

class BoardDelegate(QStyledItemDelegate):
    """Paints one board row: rank, name, medal, movement, level, XP. The top
    three ranks carry a gold/silver/bronze bar after the name and an outline
    in the same color around the whole row. Everything is drawn from the row
    dict in `ROLE_ROW`; the item's text stays set (plain "#4 Name — Lv 12 —
    34 XP") for screen readers and tests, and names the medal in words so it
    is never colour-only."""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(option.rect.width() or 300,
                     max(ROW_H, int(option.fontMetrics.height() * 2)))

    def paint(self, painter, option, index):
        row = index.data(ROLE_ROW)
        if not isinstance(row, dict):
            return super().paint(painter, option, index)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            r = option.rect
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            you = bool(row.get("you"))
            if you or selected or hovered:
                bg = theme.PANEL_RAISED
            elif index.row() % 2:
                bg = "#2F2A22"
            else:
                bg = theme.SLOT_INSET
            painter.fillRect(r, QColor(bg))
            if you:
                painter.fillRect(QRect(r.left(), r.top(), 4, r.height()),
                                 QColor(theme.GOLD))
            font = option.font
            painter.setFont(font)
            fm = QFontMetrics(font)
            mid = Qt.AlignmentFlag.AlignVCenter
            # Column widths follow the text so any UI scale fits.
            adv = fm.horizontalAdvance
            pad = max(10, fm.height() * 2 // 3)
            xp_w = adv("00,000,000 XP") + 4
            lv_w = adv("Total 000") + 8
            mv_w = adv("▼00") + 12
            rank_w = adv("#000") + 6
            x_xp = r.right() - pad - xp_w
            x_lv = x_xp - lv_w
            x_mv = x_lv - mv_w
            x_name = r.left() + pad + rank_w
            rank = row.get("rank")
            rank_text = f"#{rank}" if rank else "–"
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(QRect(r.left() + pad, r.top(), rank_w, r.height()),
                             int(Qt.AlignmentFlag.AlignLeft | mid), rank_text)
            name = hm.display_name(row)
            if you:
                name += "  (you)"
            painter.setPen(QColor(theme.GOLD if you else theme.TEXT))
            name_font = font
            name_font.setBold(you)
            painter.setFont(name_font)
            medal = str(row.get("medal") or "")
            icon = int(fm.height() * 1.5) if medal else 0
            name_fm = QFontMetrics(name_font)
            gap = fm.height() // 3 if medal else 0
            name_room = max(20, x_mv - x_name - 6 - icon - gap)
            shown = name_fm.elidedText(name, Qt.TextElideMode.ElideRight,
                                       name_room)
            painter.drawText(QRect(x_name, r.top(), name_room, r.height()),
                             int(Qt.AlignmentFlag.AlignLeft | mid), shown)
            if medal:
                pix = icon_pixmap(display_icon(_MEDAL_ITEMS[medal]), icon)
                if pix is not None:
                    dpr = pix.devicePixelRatio() or 1.0
                    h = int(pix.height() / dpr)
                    x_icon = x_name + name_fm.horizontalAdvance(shown) + gap
                    painter.drawPixmap(
                        x_icon, r.top() + (r.height() - h) // 2, pix)
            painter.setFont(font)
            glyph, kind = row.get("move") or ("", "flat")
            if glyph:
                color = {"up": theme.SUCCESS, "down": theme.ERROR,
                         "new": theme.GOLD}.get(kind, theme.TEXT_MUTED)
                painter.setPen(QColor(color))
                painter.drawText(QRect(x_mv, r.top(), mv_w, r.height()),
                                 int(Qt.AlignmentFlag.AlignHCenter | mid),
                                 glyph)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(QRect(x_lv, r.top(), lv_w, r.height()),
                             int(Qt.AlignmentFlag.AlignRight | mid),
                             str(row.get("level_text", "")))
            painter.setPen(QColor(theme.GOLD if you else theme.TEXT))
            painter.drawText(QRect(x_xp, r.top(), xp_w, r.height()),
                             int(Qt.AlignmentFlag.AlignRight | mid),
                             f"{row.get('xp_text', '0')} XP")
            outline = _MEDAL_COLORS.get(str(row.get("medal") or ""))
            if outline:
                painter.setPen(QPen(QColor(outline), 2))
                painter.drawRect(r.adjusted(1, 1, -2, -2))
            if option.state & QStyle.StateFlag.State_HasFocus:
                painter.setPen(QPen(QColor(theme.GOLD), 2))
                painter.drawRect(r.adjusted(3, 3, -4, -4))
        finally:
            painter.restore()


# -------------------------------------------------------------- player card --

class PlayerCard(QWidget):
    """A player's six skills: level, XP bar, rank; compared with you."""

    def __init__(self, on_back: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setObjectName("ankiscape-hiscores-card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.back = QPushButton("◀ Back to rankings")
        self.back.setObjectName("ankiscape-hiscores-card-back")
        self.back.setAccessibleName("Back to rankings")
        self.back.clicked.connect(lambda _c=False: on_back())
        top.addWidget(self.back)
        top.addStretch(1)
        lay.addLayout(top)
        self.title = QLabel("")
        self.title.setObjectName("ankiscape-hiscores-card-title")
        self.title.setProperty("hsBig", True)
        self.title.setProperty("medal", "gold")
        lay.addWidget(self.title)
        self.summary = body_label("", wrap=True)
        lay.addWidget(self.summary)
        self.rows_host = QWidget()
        self.grid = QGridLayout(self.rows_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        lay.addWidget(self.rows_host)
        self.note = muted_label("", wrap=True)
        lay.addWidget(self.note)
        lay.addStretch(1)
        self._cells: Dict[str, Dict[str, Any]] = {}
        self._build_rows()

    def _build_rows(self) -> None:
        for i, skill in enumerate(hm.SKILLS):
            frame = QFrame()
            frame.setObjectName("ankiscape-hs-cardrow")
            h = QHBoxLayout(frame)
            h.setContentsMargins(8, 4, 8, 4)
            h.setSpacing(8)
            icon = QLabel()
            pix = icon_pixmap(skill_icon_path(skill), 28)
            if pix is not None:
                icon.setPixmap(pix)
            icon.setFixedWidth(30)
            name = QLabel(hm.board_title(skill))
            level = QLabel("")
            bar = XpBar()
            bar.setMinimumWidth(120)
            bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            rank = QLabel("")
            rank.setAlignment(Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
            vs = muted_label("")
            vs.setAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
            for w, stretch in ((icon, 0), (name, 0), (level, 0), (bar, 1),
                               (rank, 0), (vs, 0)):
                h.addWidget(w, stretch)
            self.grid.addWidget(frame, i, 0)
            self._cells[skill] = {"level": level, "bar": bar, "rank": rank,
                                  "vs": vs, "frame": frame, "name": name}

    def show_entry(self, summary: Dict[str, Any], rows: List[Dict[str, Any]],
                   thresholds, mine: Optional[Dict[str, Any]],
                   is_me: bool, ranks_known: bool) -> None:
        name = str(summary.get("name") or "")
        self.title.setText(hm.display_name(summary)
                           + ("  (you)" if is_me else ""))
        self.title.setAccessibleName(f"Player card: {name}")
        parts = []
        tl = summary.get("total_level")
        parts.append(f"Total level {tl}" if tl is not None
                     else "Total level unavailable")
        if summary.get("total_xp") is not None:
            parts.append(f"{hm.format_whole_xp(summary['total_xp'])} XP")
        if summary.get("overall_rank"):
            parts.append(f"Overall #{summary['overall_rank']}")
        best = summary.get("best")
        if best:
            parts.append(f"Best at {hm.board_title(best)}")
        self.summary.setText("  ·  ".join(parts))
        by_skill = {r["skill"]: r for r in rows}
        self.ensurePolished()
        adv = QFontMetrics(self.font()).horizontalAdvance
        for cells in self._cells.values():
            cells["level"].setMinimumWidth(adv("Lv 99") + 8)
            cells["rank"].setMinimumWidth(adv("untrained") + 8)
            cells["vs"].setMinimumWidth(adv("level with you") + 8)
            cells["name"].setMinimumWidth(adv("Woodcutting") + 8)
        for skill, cells in self._cells.items():
            r = by_skill.get(skill, {})
            xp = r.get("xp")
            lvl = r.get("level")
            if xp is None:
                cells["level"].setText("—")
                cells["bar"].setValue(0)
                cells["bar"].setFormat("unknown")
                cells["rank"].setText("")
            elif not r.get("trained"):
                cells["level"].setText("Lv 1")
                cells["bar"].setValue(0)
                cells["bar"].setFormat("Not trained yet")
                cells["rank"].setText("")
            else:
                cells["level"].setText(f"Lv {lvl}")
                cells["bar"].set_xp(xp, thresholds, lvl)
                cells["rank"].setText(f"#{r['rank']}" if r.get("rank") else "")
            vs_text = ""
            if mine is not None and not is_me:
                mine_xp = mine["xp"].get(skill)
                if mine_xp is not None and xp is not None and (mine_xp or xp):
                    my_lvl = hm.level_of(mine_xp, thresholds)
                    diff = (lvl or 1) - my_lvl
                    vs_text = ("level with you" if diff == 0 else
                               f"{diff:+d} vs you")
            cells["vs"].setText(vs_text)
        self.note.setText(
            "" if ranks_known else
            "Ranks are shown for players on the loaded boards only.")
