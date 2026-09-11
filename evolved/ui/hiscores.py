# evolved/ui/hiscores.py - Hiscores journeys: logged out, loading, cached,
# empty, no-match, expired login, and service error states.
"""Never renders a failed request as zero scores or empty success. Offline
cached results stay visibly dated; provisional local totals are explained
separately from confirmed ranks. No sign-in popup on reviews."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional


def build_hiscores_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                        QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
                        QWidget)
    from .stale import response_is_stale
    from . import OBJECT_NAMES
    from .theme import DEFAULT_SCALE
    from .widgets import (StonePanel, body_label, display_label, icon_pixmap,
                          muted_label, error_label, success_label)
    from ..assets import slot_icon_path

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["hiscores_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    state: Dict[str, Any] = {"loading": False, "result": None, "lookup": None,
                             "req": 0, "closed": False, "account": None,
                             "cohort": False}

    # Logged-out panel
    logged_out = StonePanel()
    logged_out_head = QHBoxLayout()
    logged_out_icon = QLabel()
    logged_out_icon.setFixedSize(24, 24)
    account_pix = icon_pixmap(slot_icon_path("hiscores.account"), 24)
    if account_pix is not None:
        logged_out_icon.setPixmap(account_pix)
        logged_out_icon.setAccessibleName("Account")
    logged_out_head.addWidget(logged_out_icon)
    logged_out_head.addWidget(display_label("Compete on the Hiscores"), 1)
    logged_out.body.addLayout(logged_out_head)
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
    rank_icon = QLabel()
    rank_icon.setFixedSize(24, 24)
    rank_pix = icon_pixmap(slot_icon_path("hiscores.rank"), 24)
    if rank_pix is not None:
        rank_icon.setPixmap(rank_pix)
        rank_icon.setAccessibleName("Hiscores rank")
    header.addWidget(rank_icon)
    header.addWidget(display_label("Hiscores"))
    header.addStretch(1)
    test_toggle = QCheckBox("Test leaderboard")
    test_toggle.setObjectName(OBJECT_NAMES.get("hiscores_test_toggle",
                                               "ankiscape-hiscores-test-toggle"))
    test_toggle.setToolTip(
        "Synthetic fixture accounts on a separate test leaderboard; "
        "real player ranks are unaffected")
    test_toggle.setAccessibleName("Test leaderboard")
    test_toggle.setVisible(False)
    header.addWidget(test_toggle)
    panel.body.addLayout(header)
    status_row = QHBoxLayout()
    status_icon = QLabel()
    status_icon.setFixedSize(20, 20)
    status_icon.setVisible(False)
    status_row.addWidget(status_icon)
    status = body_label("")
    status.setObjectName(OBJECT_NAMES["hiscores_status"])
    status_row.addWidget(status, 1)
    panel.body.addLayout(status_row)
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

    def _set_status(text: str, kind: str = "muted", icon_slot: str = ""):
        status.setText(text)
        try:
            status.setAccessibleName(text)
        except Exception:
            pass
        pix = icon_pixmap(slot_icon_path(icon_slot), 20) if icon_slot else None
        if pix is not None:
            status_icon.setPixmap(pix)
            status_icon.setAccessibleName(
                "Pending" if icon_slot == "hiscores.pending" else "Offline")
            status_icon.setVisible(True)
        else:
            status_icon.setVisible(False)
        try:
            # Keep the stable identity object name: style via a dynamic
            # property so tests/tools can always find this label.
            status.setProperty(
                "statusKind",
                "error" if kind == "error"
                else ("success" if kind == "ok" else "muted"))
            status.setObjectName(OBJECT_NAMES["hiscores_status"])
            status.style().unpolish(status)
            status.style().polish(status)
        except Exception:
            pass

    def _refresh():
        account = _account()
        key = (str(account.get("username") or ""),
               bool(account.get("logged_in")),
               bool(account.get("is_test")))
        if state["account"] is not None and state["account"] != key:
            # Profile/identity changed: discard any in-flight callback so a
            # late result cannot render into the new context.
            state["req"] += 1
            state["result"] = None
            state["lookup"] = None
            state["loading"] = False
            state["cohort"] = False
        state["account"] = key
        logged_in = bool(account.get("logged_in"))
        is_test = bool(account.get("is_test"))
        logged_out.setVisible(not logged_in)
        panel.setVisible(logged_in)
        # Only server-reported test accounts ever see the test toggle.
        test_toggle.setVisible(logged_in and is_test)
        if not is_test and test_toggle.isChecked():
            test_toggle.blockSignals(True)
            test_toggle.setChecked(False)
            test_toggle.blockSignals(False)
            state["cohort"] = False
        if not logged_in:
            _set_status("", "muted")
            return
        cohort = bool(state["cohort"])
        cached = shell.call("get_hiscores_cache", skill.currentText(), cohort,
                            default=None)
        if cached:
            _render(cached, stale=True)
        if not state["loading"]:
            _load()

    def _load():
        if state["closed"] or state["loading"]:
            return
        state["req"] += 1
        req = state["req"]
        state["loading"] = True
        state["lookup"] = None
        cohort = bool(state["cohort"])
        _set_status(
            "Loading test rankings…" if cohort else "Loading rankings…",
            "muted", "hiscores.pending")
        # Cached rows stay visible while the refresh runs; a successful
        # result or an error replaces them below.
        requested = skill.currentText()

        def _done(result):
            if response_is_stale(closed=state["closed"], request_id=req,
                                 current_request_id=state["req"]):
                return  # superseded by a newer request or profile change
            if skill.currentText() != requested:
                return  # the user moved to another skill mid-flight
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
                async_fn(requested, 50, _done, cohort)
                return
            except Exception as exc:
                _done({"ok": False, "error": repr(exc)})
                return
        sync_fn = shell.deps.get("query_hiscores")
        if callable(sync_fn):
            try:
                rows = sync_fn(requested, 50, cohort)
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
        cohort = bool(state["cohort"])
        if not rows:
            _set_status(("No test rankings yet." if cohort else
                         "No rankings yet — finish some reviews, then Sync now."),
                        "muted")
        else:
            when = _fmt_time(result.get("fetched_at"))
            prefix = "Offline — showing cached rankings" if stale else "Updated"
            scope = "Test leaderboard — synthetic test accounts only. " if cohort else ""
            _set_status(f"{scope}{prefix} {when}. Competition ranks; ties share a rank.",
                        "muted" if stale else "ok",
                        "hiscores.offline" if stale else "")
        for row in rows:
            rank = row.get("rank")
            rank_text = f"#{rank}" if rank else "unranked"
            label = (f"{rank_text}  {row.get('username', '?')}  —  "
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
        cohort = bool(state["cohort"])
        cached = shell.call("get_hiscores_cache", skill.currentText(), cohort,
                            default=None)
        if cached:
            _render(dict(cached, cached=True), stale=True)
            _set_status(f"Couldn't refresh ({error[:60]}). Showing cached "
                        f"rankings from {_fmt_time(cached.get('fetched_at'))}.",
                        "error", "hiscores.offline")
            return
        listing.clear()
        if "offline" in error or "logged" in error.lower():
            _set_status("You are signed out or offline. Log in to view "
                        "Hiscores — your local progress is safe.", "error",
                        "hiscores.offline")
        elif "jwt" in error.lower() or "401" in error or "expired" in error.lower():
            _set_status("Your session expired. Log in again to view Hiscores.",
                        "error")
        else:
            _set_status(f"Service problem: {error[:120]}", "error")

    def _lookup():
        name = lookup.text().strip()
        if not name or state["closed"]:
            return
        state["req"] += 1
        req = state["req"]
        selected = skill.currentText()
        cohort = bool(state["cohort"])
        state["lookup"] = {"text": name}
        listing.clear()
        _set_status(f"Looking up {name}…")

        def _done(result):
            if response_is_stale(closed=state["closed"], request_id=req,
                                 current_request_id=state["req"]):
                return  # superseded by a refresh, skill switch or close
            if not isinstance(result, dict) or not result.get("ok"):
                not_found = bool((result or {}).get("not_found"))
                _set_status(
                    f"No player named “{name}” was found."
                    if not_found
                    else f"Lookup failed: {str((result or {}).get('error', ''))[:80]}",
                    "muted" if not_found else "error")
                return
            profile = result.get("profile") or {}
            found = str(profile.get("username") or name)
            rank = profile.get("rank")
            xp_display = str(profile.get("xp_display") or "")
            if rank:
                _set_status(f"Found {found} — rank #{rank}, "
                            f"{xp_display} XP.", "ok")
            else:
                _set_status(f"Found {found} — {xp_display} XP; rank "
                            f"unavailable outside the loaded top list.", "ok")
            rows = result.get("rows") or []
            if rows:
                _render({"ok": True, "rows": rows,
                         "fetched_at": result.get("fetched_at")})

        async_fn = shell.deps.get("lookup_player_async")
        if callable(async_fn):
            try:
                async_fn(name, selected, _done, cohort)
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
    def _toggle_cohort(checked):
        state["cohort"] = bool(checked)
        state["req"] += 1
        state["result"] = None
        state["lookup"] = None
        state["loading"] = False
        _load()

    refresh.clicked.connect(_load)
    sync_btn.clicked.connect(lambda: shell.call("on_sync"))
    test_toggle.toggled.connect(_toggle_cohort)
    skill.currentTextChanged.connect(lambda _t: _load())
    lookup.returnPressed.connect(_lookup)
    lookup.textEdited.connect(lambda _t: state.update(lookup=None))

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None

    def _release():
        state["closed"] = True
        state["req"] += 1

    root.release = _release
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
