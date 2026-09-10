# evolved/ui/hiscores.py - Hiscores journeys: logged out, loading, cached,
# empty, no-match, expired login, and service error states.
"""Never renders a failed request as zero scores or empty success. Offline
cached results stay visibly dated; provisional local totals are explained
separately from confirmed ranks. No sign-in popup on reviews."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional


def build_hiscores_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                        QListWidgetItem, QPushButton, QVBoxLayout, QWidget)
    from . import OBJECT_NAMES
    from .theme import DEFAULT_SCALE
    from .widgets import (StonePanel, body_label, display_label, muted_label,
                          error_label, success_label)

    root = QWidget()
    root.setObjectName(OBJECT_NAMES["hiscores_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    state: Dict[str, Any] = {"loading": False, "result": None, "lookup": None}

    # Logged-out panel
    logged_out = StonePanel()
    logged_out.body.addWidget(display_label("Compete on the Hiscores"))
    logged_out.body.addWidget(body_label(
        "Create a free account to back up this game and appear on the online "
        "leaderboards. Your reviews always earn XP locally — an account adds "
        "backup, cross-device catch-up, and shared rankings. It is optional "
        "and never interrupts studying.", wrap=True))
    out_row = QHBoxLayout()
    login_btn = QPushButton("Log in")
    login_btn.setObjectName("ankiscape-hiscores-login")
    register_btn = QPushButton("Create account")
    register_btn.setObjectName("ankiscape-hiscores-register")
    register_btn.setProperty("class", "primary")
    out_row.addWidget(login_btn)
    out_row.addWidget(register_btn)
    out_row.addStretch(1)
    logged_out.body.addLayout(out_row)
    layout.addWidget(logged_out)

    # Logged-in panel
    panel = StonePanel()
    header = QHBoxLayout()
    header.addWidget(display_label("Hiscores"))
    header.addStretch(1)
    panel.body.addLayout(header)
    status = body_label("")
    status.setObjectName(OBJECT_NAMES["hiscores_status"])
    panel.body.addWidget(status)
    controls = QHBoxLayout()
    skill = QComboBox()
    skill.setObjectName(OBJECT_NAMES["hiscores_skill"])
    skill.addItems(["mining", "woodcutting", "smithing", "crafting",
                    "fishing", "cooking"])
    lookup = QLineEdit()
    lookup.setObjectName(OBJECT_NAMES["hiscores_lookup"])
    lookup.setPlaceholderText("Find a player…")
    refresh = QPushButton("Refresh")
    refresh.setObjectName(OBJECT_NAMES["hiscores_refresh"])
    sync_btn = QPushButton("Sync now")
    sync_btn.setObjectName(OBJECT_NAMES["sync_button"])
    controls.addWidget(QLabel("Skill:"))
    controls.addWidget(skill)
    controls.addWidget(lookup, 1)
    controls.addWidget(refresh)
    controls.addWidget(sync_btn)
    panel.body.addLayout(controls)
    listing = QListWidget()
    listing.setObjectName(OBJECT_NAMES["hiscores_list"])
    panel.body.addWidget(listing, 1)
    layout.addWidget(panel, 1)

    def _account() -> Dict[str, Any]:
        try:
            info = shell.call("get_account", default={}) or {}
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def _set_status(text: str, kind: str = "muted"):
        status.setText(text)
        try:
            status.setObjectName("ankiscape-error" if kind == "error"
                                 else ("ankiscape-success" if kind == "ok"
                                       else "ankiscape-muted"))
            status.style().unpolish(status)
            status.style().polish(status)
        except Exception:
            pass

    def _refresh():
        account = _account()
        logged_in = bool(account.get("logged_in"))
        logged_out.setVisible(not logged_in)
        panel.setVisible(logged_in)
        if not logged_in:
            return
        cached = shell.call("get_hiscores_cache", skill.currentText(),
                            default=None)
        if cached:
            _render(cached, stale=True)
        if not state["loading"]:
            _load()

    def _load():
        if state["loading"]:
            return
        state["loading"] = True
        state["lookup"] = None
        _set_status("Loading rankings…")
        listing.clear()
        requested = skill.currentText()

        def _done(result):
            state["loading"] = False
            if not isinstance(result, dict):
                result = {"ok": False, "error": "unexpected response"}
            if result.get("ok"):
                state["result"] = result
                _render(result, stale=bool(result.get("cached")))
            else:
                _render_error(result)

        async_fn = shell.deps.get("query_hiscores_async")
        if callable(async_fn):
            try:
                async_fn(requested, 50, _done)
                return
            except Exception as exc:
                _done({"ok": False, "error": repr(exc)})
                return
        sync_fn = shell.deps.get("query_hiscores")
        if callable(sync_fn):
            try:
                rows = sync_fn(requested, 50)
                _done({"ok": True, "rows": rows, "fetched_at": time.time()})
            except Exception as exc:
                _done({"ok": False, "error": str(exc)})
            return
        _done({"ok": False, "error": "no query path configured"})

    def _render(result: Dict[str, Any], stale: bool = False):
        rows = result.get("rows") or []
        listing.clear()
        account = _account()
        me = str(account.get("username", "") or "")
        if not rows:
            _set_status("No rankings yet — finish some reviews, then Sync now.",
                        "muted")
        else:
            when = _fmt_time(result.get("fetched_at"))
            prefix = "Offline — showing cached rankings" if stale else "Updated"
            _set_status(f"{prefix} {when}. Competition ranks; ties share a rank.",
                        "muted" if stale else "ok")
        for row in rows:
            label = (f"#{row.get('rank', '?')}  {row.get('username', '?')}  —  "
                     f"{row.get('xp_display', row.get('xp', '0'))} XP")
            item = QListWidgetItem(label)
            if me and str(row.get("username", "")) == me:
                item.setText(label + "  (you)")
                try:
                    from aqt.qt import QFont
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                except Exception:
                    pass
            listing.addItem(item)
        lookup_row = state.get("lookup")
        if lookup_row is not None:
            listing.insertItem(0, QListWidgetItem(
                f"Lookup: {lookup_row.get('text', '')}"))
        if listing.count() == 0:
            listing.addItem(QListWidgetItem("Nothing to show."))

    def _render_error(result: Dict[str, Any]):
        error = str(result.get("error") or "unknown error")
        cached = shell.call("get_hiscores_cache", skill.currentText(),
                            default=None)
        if cached:
            _render(dict(cached, cached=True), stale=True)
            _set_status(f"Couldn't refresh ({error[:60]}). Showing cached "
                        f"rankings from {_fmt_time(cached.get('fetched_at'))}.",
                        "error")
            return
        listing.clear()
        if "offline" in error or "logged" in error.lower():
            _set_status("You are signed out or offline. Log in to view "
                        "Hiscores — your local progress is safe.", "error")
        elif "jwt" in error.lower() or "401" in error or "expired" in error.lower():
            _set_status("Your session expired. Log in again to view Hiscores.",
                        "error")
        else:
            _set_status(f"Service problem: {error[:120]}", "error")

    def _lookup():
        name = lookup.text().strip()
        if not name:
            return
        state["lookup"] = {"text": name}
        listing.clear()
        _set_status(f"Looking up {name}…")

        def _done(result):
            if not isinstance(result, dict) or not result.get("ok"):
                _set_status(
                    f"No player named “{name}” was found."
                    if result and result.get("not_found")
                    else f"Lookup failed: {str((result or {}).get('error', ''))[:80]}",
                    "error" if not (result or {}).get("not_found") else "muted")
                return
            profile = result.get("profile") or {}
            _set_status(f"Found {profile.get('username', name)}.", "ok")
            rows = result.get("rows") or []
            if rows:
                _render({"ok": True, "rows": rows,
                         "fetched_at": result.get("fetched_at")})

        async_fn = shell.deps.get("lookup_player_async")
        if callable(async_fn):
            try:
                async_fn(name, _done)
                return
            except Exception as exc:
                _done({"ok": False, "error": repr(exc)})
        else:
            _done({"ok": False, "not_found": True})

    def _open_login():
        shell.call("on_account")

    def _open_register():
        shell.call("on_register")

    login_btn.clicked.connect(_open_login)
    register_btn.clicked.connect(_open_register)
    refresh.clicked.connect(_load)
    sync_btn.clicked.connect(lambda: shell.call("on_sync"))
    skill.currentTextChanged.connect(lambda _t: _load())
    lookup.returnPressed.connect(_lookup)
    lookup.textEdited.connect(lambda _t: state.update(lookup=None))

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None
    root.release = lambda: None
    root.deps = deps
    _refresh()
    return root


def _fmt_time(ts) -> str:
    try:
        value = float(ts)
        if value <= 0:
            return "earlier"
        return time.strftime("%b %d, %H:%M", time.localtime(value))
    except (TypeError, ValueError):
        return "earlier"
