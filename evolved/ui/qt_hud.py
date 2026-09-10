# evolved/ui/qt_hud.py - Anchored, non-interrupting review HUD.
"""Reserved Anki layout space above or below the reviewer content (default
below). Never injected into the card template and never floating over card
content. Non-focusable and mouse-transparent so every native review shortcut
and click keeps working.

Updates come from persisted reward events (not answer-button clicks). Brief
reward flashes are coalesced; level/unlock celebrations are one short
highlight with an optional sound. Reduced motion makes updates immediate.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

HUD_OBJECT_NAME = "ankiscape-evolved-hud"
_R = {"reward_token": 0, "celebrate_token": 0}


def _diag(payload: dict) -> None:
    """Record HUD wiring diagnostics for the dev driver (never user-facing)."""
    try:
        import os
        if not os.environ.get("ANKISCAPE_DEBUG"):
            return
        from aqt import mw
        mw.col.set_config("ankiscape_hud_diag", payload, undoable=False)
    except Exception:
        pass


def _qt():
    from aqt.qt import (QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
                        QTimer, Qt)
    return (QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QTimer, Qt)


def ensure_hud(mw, settings: Optional[Dict[str, Any]] = None):
    """Create (or reuse) the anchored HUD frame and insert it into the
    reviewer layout, reserving real space. Returns the widget or None."""
    try:
        existing = getattr(mw, "ankiscape_evolved_hud", None)
        if existing is not None:
            apply_settings(existing, settings or {}, mw=mw)
            return existing
        QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QTimer, Qt = _qt()
        hud = QFrame(mw)
        hud.setObjectName(HUD_OBJECT_NAME)
        hud.setFrameShape(QFrame.Shape.StyledPanel)
        hud.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hud.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(hud)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        hud._icon = QLabel(hud)
        hud._icon.setFixedSize(24, 24)
        layout.addWidget(hud._icon)
        hud._label = QLabel("", hud)
        hud._label.setObjectName("ankiscape-hud-label")
        layout.addWidget(hud._label)
        hud._bar = QProgressBar(hud)
        hud._bar.setObjectName("ankiscape-xp-bar")
        hud._bar.setTextVisible(False)
        hud._bar.setRange(0, 1000)
        hud._bar.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)
        layout.addWidget(hud._bar, 1)
        hud._reward = QLabel("", hud)
        hud._reward.setObjectName("ankiscape-hud-reward")
        layout.addWidget(hud._reward)
        hud._status = QLabel("", hud)
        hud._status.setObjectName("ankiscape-hud-status")
        layout.addWidget(hud._status)
        hud.setVisible(False)
        try:
            from .widgets import apply_theme
            from .theme import clamp_scale
            apply_theme(hud, clamp_scale((settings or {}).get("ui_scale", 100)))
        except Exception:
            pass
        mw.ankiscape_evolved_hud = hud
        apply_settings(hud, settings or {}, mw=mw)
        _insert_into_reviewer(mw, hud, (settings or {}).get("position", "bottom"))
        return hud
    except Exception as exc:
        _diag({"ok": False, "phase": "ensure", "error": repr(exc)[:300]})
        return None


def apply_settings(hud, settings: Dict[str, Any], mw=None) -> None:
    if hud is None:
        return
    try:
        visible = bool(settings.get("hud_visible", True))
        hud.setVisible(visible and mw is not None and _in_review(mw))
    except Exception:
        pass
    try:
        if mw is not None:
            _insert_into_reviewer(mw, hud,
                                  str(settings.get("position", "bottom")))
    except Exception:
        pass


def _in_review(mw) -> bool:
    try:
        return getattr(mw, "state", "") == "review"
    except Exception:
        return False


def _insert_into_reviewer(mw, hud, position: str) -> bool:
    """Reserve layout space in the reviewer: top or bottom of card content.

    Anki 23.10 has a plain Reviewer QWidget with a layout; Anki 26 wraps the
    main web view (MainWebView) in a container widget. Prefer the layout one
    level above the web view's direct container: Anki rebuilds the reviewer's
    own layout during undo/redo, and a foreign widget inside it breaks that
    pipeline (observed: redo silently no-ops). Inserting beside the container
    reserves the same visual space without joining the rebuilt layout.
    """
    try:
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            return False
        web = getattr(reviewer, "web", None)
        layout = None
        anchor = None
        index = -1
        mode = ""
        # 1. Preferred: the web view wrapper's own box layout, appended below
        #    (or prepended above) the engine view. Anki keeps the reviewer
        #    layout stable here across undo/redo, unlike its outer container.
        try:
            wrapper_layout = web.layout() if web is not None else None
            if wrapper_layout is not None and hasattr(wrapper_layout, "insertWidget"):
                layout = wrapper_layout
                anchor = web
                mode = "web_wrapper"
                index = 0 if str(position) == "top" else wrapper_layout.count()
        except Exception:
            layout = None
        # 2. Fallback: the container's box layout (23.10-style reviewer).
        if layout is None:
            container = None
            try:
                container = web.parentWidget() if web is not None else None
            except Exception:
                container = None
            if container is not None:
                try:
                    container_layout = container.layout()
                    if (container_layout is not None
                            and hasattr(container_layout, "insertWidget")):
                        layout = container_layout
                        anchor = container
                        index = container_layout.indexOf(web)
                        mode = "container"
                        if index < 0:
                            index = container_layout.count()
                        elif str(position) != "top":
                            index = index + 1
                except Exception:
                    layout = None
        # 3. Last resort: the reviewer's own layout (Classic-style).
        if layout is None:
            try:
                candidate = reviewer.layout()
                if candidate is not None and hasattr(candidate, "insertWidget"):
                    layout = candidate
                    anchor = reviewer if hasattr(reviewer, "parentWidget") else web
                    mode = "reviewer"
                    index = candidate.indexOf(web) if web is not None else -1
                    if index < 0:
                        index = candidate.count()
                    elif str(position) != "top":
                        index = index + 1
            except Exception:
                layout = None
        if layout is None:
            return False
        # Stability: never remove/reinsert while the anchor and side hold.
        if (getattr(hud, "_ankiscape_anchor", None) is anchor
                and getattr(hud, "_ankiscape_pos", None) == str(position)
                and getattr(hud, "_ankiscape_mode", None) == mode):
            return True
        try:
            layout.removeWidget(hud)
        except Exception:
            pass
        if anchor is not None and hasattr(anchor, "setParent"):
            hud.setParent(anchor)
        index = max(0, min(int(index), layout.count()))
        layout.insertWidget(index, hud)
        hud._ankiscape_anchor = anchor
        hud._ankiscape_pos = str(position)
        hud._ankiscape_mode = mode
        _diag({"ok": True, "mode": mode, "layout": type(layout).__name__,
               "anchor": type(anchor).__name__ if anchor is not None else None,
               "index": int(index), "position": str(position),
               "has_web": web is not None})
        return True
    except Exception as exc:
        chain = []
        try:
            node = getattr(mw, "reviewer", None)
            web2 = getattr(node, "web", None)
            if web2 is not None:
                node = web2
            for _ in range(6):
                if node is None:
                    break
                layout = None
                try:
                    layout = node.layout()
                except Exception:
                    layout = None
                chain.append({"type": type(node).__name__,
                              "layout": type(layout).__name__ if layout is not None else None,
                              "parent": type(node.parentWidget()).__name__
                              if hasattr(node, "parentWidget") and node.parentWidget()
                              else None})
                node = node.parentWidget() if hasattr(node, "parentWidget") else None
        except Exception:
            chain.append({"chain_error": "walk failed"})
        _diag({"ok": False, "error": repr(exc)[:300],
               "reviewer": type(getattr(mw, "reviewer", None)).__name__,
               "reviewer_is_widget": hasattr(getattr(mw, "reviewer", None), "parentWidget"),
               "chain": chain})
        return False


def update_hud(mw, projection: Dict[str, Any], skill: str,
               settings: Optional[Dict[str, Any]] = None) -> None:
    """Refresh content from the projection; show inside review only."""
    settings = settings or {}
    if not bool(settings.get("visible", True)):
        hide_hud(mw)
        return
    if not _in_review(mw):
        # Content updates only inside the reviewer; never paint outside it.
        return
    hud = ensure_hud(mw, settings)
    if hud is None:
        return
    try:
        from .theme import xp_progress
        from ..assets import display_icon, skill_icon_path
        from .widgets import icon_pixmap
        rules = _rules()
        levels = projection.get("levels", {}) or {}
        xp_micro = projection.get("xp_micro", {}) or {}
        level = int(levels.get(skill, 1))
        thresholds = rules.get("thresholds", [])
        pix = icon_pixmap(skill_icon_path(skill), 24)
        if pix is not None:
            hud._icon.setPixmap(pix)
        hud._label.setText(f"{str(skill).title()} {level}")
        hud._bar.setValue(int(round(xp_progress(
            int(xp_micro.get(skill, 0)), thresholds, level) * 1000)))
        _ = display_icon
        hud._status.setText("")
        hud.setVisible(True)
    except Exception:
        pass


def on_review_event(mw, projection: Dict[str, Any], skill: str, *,
                    reward: Optional[Dict[str, Any]] = None,
                    paused_reason: str = "",
                    settings: Optional[Dict[str, Any]] = None) -> None:
    """Publish one persisted reward event: brief reward, celebration, pause."""
    settings = settings or {}
    if not bool(settings.get("visible", True)) or not _in_review(mw):
        hide_hud(mw)
        return
    update_hud(mw, projection, skill, settings)
    hud = getattr(mw, "ankiscape_evolved_hud", None)
    if hud is None:
        return
    reduced = bool(settings.get("reduced_motion", False))
    try:
        if reward and reward.get("rewarded"):
            xp = int(reward.get("xp_micro", 0))
            from .menu_model import format_xp
            text = f"+{format_xp(xp)} XP"
            items = reward.get("items") or {}
            if items:
                text += " · " + ", ".join(f"{n} ×{q}" for n, q in items.items())
            _flash(hud, text, ms=0 if reduced else 800)
            _maybe_sound(settings, level_up=False, unlock=False)
        elif paused_reason in ("paused_materials", "paused_level"):
            reason = ("Need materials" if paused_reason == "paused_materials"
                      else "Level requirement unmet")
            hud._status.setText(f"Paused — {reason}")
        else:
            hud._status.setText("")
    except Exception:
        pass


def celebrate(mw, text: str, *, kind: str = "level",
              settings: Optional[Dict[str, Any]] = None) -> None:
    """Level-up / unlock celebration (coalesced, <=1.5 s, sound optional)."""
    settings = settings or {}
    hud = getattr(mw, "ankiscape_evolved_hud", None)
    if hud is None:
        return
    reduced = bool(settings.get("reduced_motion", False))
    try:
        from .theme import GOLD
        hud._reward.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        _flash(hud, text, ms=0 if reduced else 1500)
        _maybe_sound(settings, level_up=(kind == "level"), unlock=(kind != "level"))
    except Exception:
        pass


def _flash(hud, text: str, *, ms: int) -> None:
    _R["reward_token"] += 1
    token = _R["reward_token"]
    try:
        hud._reward.setText(text)
    except Exception:
        return

    def _clear():
        if _R["reward_token"] == token:
            try:
                hud._reward.setText("")
            except Exception:
                pass

    if ms <= 0:
        return
    try:
        from aqt.qt import QTimer
        QTimer.singleShot(ms, _clear)
    except Exception:
        pass


def _maybe_sound(settings: Dict[str, Any], *, level_up: bool, unlock: bool) -> None:
    if not bool(settings.get("sound", False)):
        return
    if not (level_up or unlock):
        return
    try:
        from ..assets import sound_path
        path = sound_path("level_up" if level_up else "unlock")
        if not path:
            return
        try:
            from aqt.sound import av_player
            av_player.play_file(path)
        except Exception:
            from aqt.qt import QSoundEffect, QUrl
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(path))
            effect.play()
            # Keep a reference so the effect is not garbage-collected.
            _R["effect"] = effect
    except Exception:
        pass


def hide_hud(mw) -> None:
    hud = getattr(mw, "ankiscape_evolved_hud", None)
    if hud is not None:
        try:
            hud.setVisible(False)
        except Exception:
            pass


def release_hud(mw) -> None:
    hud = getattr(mw, "ankiscape_evolved_hud", None)
    if hud is not None:
        try:
            hud.hide()
            hud.setParent(None)
            hud.deleteLater()
        except Exception:
            pass
    try:
        delattr(mw, "ankiscape_evolved_hud")
    except Exception:
        pass


def _rules():
    from ..data import load_rules
    try:
        return load_rules()
    except Exception:
        return {}


# --- Compatibility with the pre-3.0 floating HUD API ----------------------

def ensure_qt_hud(owner, mw) -> Any:
    """Legacy shim: the anchored HUD no longer needs an owner."""
    _ = owner
    return ensure_hud(mw, {"visible": True, "position": "bottom"})


def request_qt_update(owner, mw, **fields) -> None:
    _ = owner
    projection = (fields.get("projection")
                  or getattr(mw, "ankiscape_last_projection", None) or {})
    update_hud(mw, projection, fields.get("skill", "mining"), fields)


def release_qt_hud(mw) -> None:
    release_hud(mw)
