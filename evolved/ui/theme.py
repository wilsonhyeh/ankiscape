# evolved/ui/theme.py - One source for Evolved colors, spacing, type, states.
"""Pure data + string builders (no Qt import at module load). The Qt shell and
widgets consume `build_stylesheet()`; tests consume the tokens and
`contrast_ratio()` directly.

Design contract (AnkiScape 3.0 UI/UX plan, "Visual specification"):
  - Faithful old-school RuneScape look: brown stone, dark panels, gold accents.
  - Palette is FIXED regardless of Anki light/dark theme.
  - Body text must meet 4.5:1 on its own surface; `body_contrast_failures()`
    is the executable check.
  - Pixel-style display font for short game labels; system UI font for
    instructions/forms/errors at 14 logical px.
  - UI scale 100/125/150/200%, default 100. All sizes derive from the scale.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# --- Palette (fixed; do not theme-switch) ---------------------------------
BACKGROUND = "#211E19"       # window / outer frame
PANEL = "#3A342A"            # standard panel surface
PANEL_RAISED = "#4A4133"     # raised slots, headers, selected rows
BORDER = "#796744"           # beveled borders
GOLD = "#E3BE68"             # accents, headings, selected focus
TEXT = "#F2E6C9"             # primary body text
TEXT_MUTED = "#C1B59C"       # secondary text
SUCCESS = "#98C77A"          # ready / success states
ERROR = "#F0A08C"            # errors, unmet requirements
SLOT_INSET = "#2A251D"       # inset item slot background
LOCKED = "#2E2A23"           # locked overlay wash (never color-only)

PALETTE: Dict[str, str] = {
    "background": BACKGROUND,
    "panel": PANEL,
    "panel_raised": PANEL_RAISED,
    "border": BORDER,
    "gold": GOLD,
    "text": TEXT,
    "text_muted": TEXT_MUTED,
    "success": SUCCESS,
    "error": ERROR,
    "slot_inset": SLOT_INSET,
    "locked": LOCKED,
}

# --- Layout units (logical px at 100%) -------------------------------------
RAIL_WIDTH = 64
CONTENT_INSET = 16
SPACING = 8
WINDOW_W = 840
WINDOW_H = 640
WINDOW_MIN_W = 640
WINDOW_MIN_H = 480
ITEM_CELL = 64
ITEM_ART = 48
BODY_PX = 14

SCALES = (100, 125, 150, 200)
DEFAULT_SCALE = 100

DISPLAY_FONT = "Press Start 2P"
BODY_FONT_FALLBACKS = (
    "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Helvetica Neue",
    "Arial", "sans-serif",
)


def clamp_scale(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCALE
    if n not in SCALES:
        return DEFAULT_SCALE
    return n


def scaled(value: int, scale: int = DEFAULT_SCALE) -> int:
    return max(1, int(round(int(value) * int(scale) / 100.0)))


def body_font_css() -> str:
    return ", ".join(f'"{name}"' if " " in name else name
                     for name in BODY_FONT_FALLBACKS)


# --- Contrast (WCAG 2.x relative luminance) --------------------------------

def _channel(value: int) -> float:
    c = max(0, min(255, int(value))) / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    text = str(hex_color).lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b))


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# Body-text pairs that must meet 4.5:1. Muted/secondary text included; gold
# headings on panel surfaces included (headings are large text, but the
# palette is fixed so we hold them to the stricter bar).
BODY_CONTRAST_PAIRS: Tuple[Tuple[str, str, str], ...] = (
    ("text/background", TEXT, BACKGROUND),
    ("text/panel", TEXT, PANEL),
    ("text/panel_raised", TEXT, PANEL_RAISED),
    ("text_muted/background", TEXT_MUTED, BACKGROUND),
    ("text_muted/panel", TEXT_MUTED, PANEL),
    ("text_muted/panel_raised", TEXT_MUTED, PANEL_RAISED),
    ("gold/background", GOLD, BACKGROUND),
    ("gold/panel", GOLD, PANEL),
    ("gold/panel_raised", GOLD, PANEL_RAISED),
    ("success/panel", SUCCESS, PANEL),
    ("error/panel", ERROR, PANEL),
)


def body_contrast_failures(min_ratio: float = 4.5) -> List[str]:
    out = []
    for name, fg, bg in BODY_CONTRAST_PAIRS:
        ratio = contrast_ratio(fg, bg)
        if ratio < min_ratio:
            out.append(f"{name}: {ratio:.2f} < {min_ratio}")
    return out


# --- State style tokens (pure; consumed by widgets/screens) ----------------

STATE_STYLES: Dict[str, Dict[str, str]] = {
    "ready": {"color": SUCCESS, "label": "Ready"},
    "paused_materials": {"color": ERROR, "label": "Paused — gather materials"},
    "paused_level": {"color": ERROR, "label": "Paused — level too low"},
    "locked": {"color": TEXT_MUTED, "label": "Locked"},
    "selected": {"color": GOLD, "label": "Training"},
    "max_level": {"color": GOLD, "label": "Level 99"},
    "offline": {"color": TEXT_MUTED, "label": "Offline"},
    "error": {"color": ERROR, "label": "Problem"},
    "success": {"color": SUCCESS, "label": "Synced"},
}


def xp_progress(current_xp_micro: int, thresholds, level: int) -> float:
    """Progress toward the next level in [0, 1]; 1.0 at level 99.

    thresholds: cumulative XP per level (index i holds level i+1 requirement),
    same table the reducer uses. Pure, integer-safe.
    """
    try:
        lvl = int(level)
        if lvl >= 99:
            return 1.0
        if lvl < 1:
            lvl = 1
        current = max(0, int(current_xp_micro))
        base = int(thresholds[lvl - 1]) * 1_000_000 if thresholds else 0
        nxt = int(thresholds[lvl]) * 1_000_000 if thresholds and lvl < len(thresholds) else base
        span = nxt - base
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (current - base) / float(span)))
    except (IndexError, TypeError, ValueError):
        return 0.0


# --- Qt stylesheet ----------------------------------------------------------

def build_stylesheet(scale: int = DEFAULT_SCALE) -> str:
    """Return the Qt stylesheet for the Evolved shell at a UI scale."""
    s = clamp_scale(scale)
    rail = scaled(RAIL_WIDTH, s)
    cell = scaled(ITEM_CELL, s)
    inset = scaled(CONTENT_INSET, s)
    pad = scaled(SPACING, s)
    body = scaled(BODY_PX, s)
    radius = scaled(3, s)
    return f"""
* {{
  font-family: {body_font_css()};
  font-size: {body}px;
  color: {TEXT};
}}
QWidget#ankiscape-evolved-shell, QDialog#ankiscape-evolved-shell {{
  background-color: {BACKGROUND};
}}
QWidget#ankiscape-shell-content {{
  background-color: {BACKGROUND};
}}
QFrame#ankiscape-stone-panel, QWidget#ankiscape-stone-panel {{
  background-color: {PANEL};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
}}
QFrame#ankiscape-raised-panel {{
  background-color: {PANEL_RAISED};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
}}
QLabel#ankiscape-display, QLabel#ankiscape-heading,
QLabel#ankiscape-shell-header {{
  font-family: "{DISPLAY_FONT}", {body_font_css()};
  color: {GOLD};
  font-weight: normal;
  font-size: {body}px;
}}
QLabel#ankiscape-shell-header {{ font-size: {body + 1}px; }}
QLabel#ankiscape-training-title {{ font-size: {body + 2}px; color: {TEXT}; }}
QLabel#ankiscape-muted,
QLabel[statusKind="muted"] {{
  color: {TEXT_MUTED};
}}
QLabel#ankiscape-success,
QLabel[statusKind="success"] {{ color: {SUCCESS}; }}
QLabel#ankiscape-error,
QLabel[statusKind="error"] {{ color: {ERROR}; }}
QWidget#ankiscape-icon-rail {{
  background-color: {PANEL};
  border-right: 2px solid {BORDER};
}}
QToolButton#ankiscape-rail-button {{
  background-color: {PANEL};
  border: 2px solid transparent;
  border-radius: {radius}px;
  padding: {scaled(6, s)}px;
  color: {TEXT_MUTED};
}}
QToolButton#ankiscape-rail-button:hover {{
  background-color: {PANEL_RAISED};
  border-color: {BORDER};
  color: {TEXT};
}}
QToolButton#ankiscape-rail-button:checked {{
  background-color: {PANEL_RAISED};
  border-color: {GOLD};
  color: {GOLD};
}}
QToolButton#ankiscape-rail-button:focus {{
  border-color: {GOLD};
}}
QPushButton {{
  background-color: {PANEL_RAISED};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  padding: {scaled(5, s)}px {scaled(10, s)}px;
  color: {TEXT};
}}
QPushButton:hover {{ border-color: {GOLD}; }}
QPushButton:focus {{ border-color: {GOLD}; }}
QPushButton:disabled {{
  color: {TEXT_MUTED};
  border-color: {PANEL};
  background-color: {PANEL};
}}
QPushButton#ankiscape-primary {{
  background-color: {PANEL_RAISED};
  border: 2px solid {GOLD};
  color: {GOLD};
  font-weight: bold;
}}
QPushButton#ankiscape-primary:hover {{ background-color: {BORDER}; color: {TEXT}; }}
QPushButton#ankiscape-danger {{ border-color: {ERROR}; color: {ERROR}; }}
QLineEdit, QComboBox, QSpinBox {{
  background-color: {SLOT_INSET};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  padding: {scaled(4, s)}px;
  color: {TEXT};
  selection-background-color: {BORDER};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {GOLD}; }}
QComboBox QAbstractItemView {{
  background-color: {PANEL};
  border: 2px solid {BORDER};
  color: {TEXT};
  selection-background-color: {PANEL_RAISED};
}}
QListWidget, QTreeWidget, QTableWidget {{
  background-color: {SLOT_INSET};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  color: {TEXT};
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
  background-color: {PANEL_RAISED};
  color: {GOLD};
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
  background: {BACKGROUND};
  width: {scaled(10, s)}px;
  margin: 0;
}}
QScrollBar::handle:vertical {{
  background: {BORDER};
  min-height: {scaled(24, s)}px;
  border-radius: {radius}px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
  background: {BACKGROUND};
  height: {scaled(10, s)}px;
  margin: 0;
}}
QScrollBar::handle:horizontal {{
  background: {BORDER};
  min-width: {scaled(24, s)}px;
  border-radius: {radius}px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QFrame#ankiscape-item-slot {{
  background-color: {SLOT_INSET};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  min-width: {cell}px;
  min-height: {cell}px;
}}
QFrame#ankiscape-item-slot:hover {{ border-color: {GOLD}; }}
QFrame#ankiscape-item-slot[state="selected"] {{ border-color: {GOLD}; }}
QFrame#ankiscape-item-slot[state="locked"] {{ border-color: {PANEL}; }}
QFrame#ankiscape-item-slot[state="paused"] {{ border-color: {ERROR}; }}
QLabel#ankiscape-item-qty {{
  color: {GOLD};
  font-weight: bold;
  background: rgba(0, 0, 0, 0.55);
  border-radius: {radius}px;
  padding: 0 {scaled(3, s)}px;
}}
QLabel#ankiscape-badge {{
  color: {TEXT_MUTED};
  background: rgba(0, 0, 0, 0.65);
  border-radius: {radius}px;
  padding: 0 {scaled(3, s)}px;
}}
QProgressBar#ankiscape-xp-bar {{
  background-color: {SLOT_INSET};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  height: {scaled(14, s)}px;
  text-align: center;
  color: {TEXT};
}}
QProgressBar#ankiscape-xp-bar::chunk {{
  background-color: {GOLD};
  border-radius: {radius}px;
}}
QTabWidget::pane {{
  border: 2px solid {BORDER};
  background-color: {PANEL};
  top: -{scaled(2, s)}px;
}}
QTabBar::tab {{
  background-color: {PANEL};
  border: 2px solid {BORDER};
  color: {TEXT_MUTED};
  padding: {scaled(5, s)}px {scaled(10, s)}px;
}}
QTabBar::tab:selected {{
  background-color: {PANEL_RAISED};
  color: {GOLD};
}}
QGroupBox {{
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  margin-top: {scaled(10, s)}px;
  color: {GOLD};
  padding: {inset}px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: {pad}px; }}
QCheckBox, QRadioButton {{ color: {TEXT}; spacing: {pad}px; }}
QCheckBox:focus, QRadioButton:focus {{ color: {GOLD}; }}
QToolTip {{
  background-color: {PANEL_RAISED};
  color: {TEXT};
  border: 2px solid {BORDER};
}}
QLabel#ankiscape-hud-label {{
  color: {TEXT};
  background-color: {PANEL};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
  padding: {scaled(3, s)}px {pad}px;
}}
QLabel#ankiscape-hud-reward {{ color: {GOLD}; font-weight: bold; }}
QLabel#ankiscape-hud-status {{ color: {ERROR}; }}
QFrame#ankiscape-evolved-hud {{
  background-color: {PANEL};
  border: 2px solid {BORDER};
  border-radius: {radius}px;
}}
QFrame#ankiscape-reward-toast {{
  background-color: {PANEL_RAISED};
  border: 2px solid {GOLD};
  border-radius: {radius}px;
}}
"""


def state_style(state: str) -> Dict[str, str]:
    return dict(STATE_STYLES.get(str(state), STATE_STYLES["offline"]))
