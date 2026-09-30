# evolved/ui/hiscores.py - Public Hiscores: browse, compete, look people up.
"""The public board is browsable without an account: live rankings and player
lookup work logged out with the anonymous key. Signing in adds your own
standing, sync and catch-up, shown as a call to action and never a locked
panel.

Layout: a tab strip (Overall + the six skills), a pinned "You" bar with your
rank and the gap to the next player, then one list of everyone. The top three
ranks carry a gold, silver or bronze bar after their name and an outline in
the same color. Click anyone for a player card with all six skills.

Every board loads in the background so ranks, levels and cards are complete;
only the selected board is ever waited on. Zero-XP players are counted, not
listed: a wall of tied "0 XP" rows says nothing.

States covered: logged out, loading, cached/stale, empty, no-match, session
expired, service error, and the authenticated Test leaderboard for
server-reported test accounts. Cached rows survive network failure with their
timestamp; a real empty result is distinct from an unavailable service.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


def build_hiscores_screen(shell, deps: Dict[str, Any]):
    from aqt.qt import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                        QListWidget, QListWidgetItem, QPushButton,
                        QStackedWidget, QToolButton, QVBoxLayout, QWidget, Qt)
    from .stale import response_is_stale
    from . import OBJECT_NAMES
    from . import hiscores_model as hm
    from .hiscores_widgets import (ROLE_ROW, BoardDelegate, PlayerCard, YouBar,
                                   board_icon_path, make_board_tab)
    from .widgets import StonePanel, body_label, muted_label
    from ..assets import slot_icon_path
    from .widgets import icon_pixmap

    root = QWidget(shell)
    root.setObjectName(OBJECT_NAMES["hiscores_screen"])
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    state: Dict[str, Any] = {
        "board": hm.OVERALL, "boards": {}, "stale": {}, "fetched": {},
        "moves": {}, "seen": None, "seen_out": {}, "rolled": set(),
        "failed": set(), "fetched_at_mono": {},
        "loading": False, "inflight": set(), "epoch": 0, "closed": False,
        "account": None,
        "cohort": False, "sync_requested_at": 0.0, "overall_ok": True,
        "sort": hm.SORT_XP, "mode_shown": hm.SORT_XP,
        "error": None, "card_entry": None, "card_name": "", "lookup_seq": 0,
    }

    # ---- tab strip ---------------------------------------------------------
    tab_row = QHBoxLayout()
    tab_row.setSpacing(4)
    tabs: Dict[str, Any] = {}
    for board in hm.BOARDS:
        tab = make_board_tab(board, hm.board_title(board),
                             lambda b: _select_board(b))
        tabs[board] = tab
        tab_row.addWidget(tab, 1)
    tabs[hm.OVERALL].setChecked(True)
    layout.addLayout(tab_row)

    # ---- main panel: board page + player card page -------------------------
    panel = StonePanel()
    stack = QStackedWidget()
    panel.body.addWidget(stack, 1)

    board_page = QWidget()
    bp = QVBoxLayout(board_page)
    bp.setContentsMargins(0, 0, 0, 0)
    bp.setSpacing(6)

    controls = QHBoxLayout()
    lookup = QLineEdit()
    lookup.setObjectName(OBJECT_NAMES["hiscores_lookup"])
    lookup.setPlaceholderText("Find a player…")
    lookup.setClearButtonEnabled(True)
    lookup.setAccessibleName("Find a player")
    # Qt's built-in clear button is an unnamed, unfocusable QToolButton. Name
    # it and make it reachable by keyboard (the journey accessibility gate
    # fails any unnamed or unfocusable button).
    for clear_btn in lookup.findChildren(QToolButton):
        clear_btn.setAccessibleName("Clear search")
        clear_btn.setToolTip("Clear search")
        clear_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    refresh = QPushButton("Refresh")
    refresh.setObjectName(OBJECT_NAMES["hiscores_refresh"])
    sync_btn = QPushButton("Sync now")
    sync_btn.setObjectName(OBJECT_NAMES["sync_button"])
    test_toggle = QCheckBox("Test leaderboard")
    test_toggle.setObjectName(OBJECT_NAMES.get("hiscores_test_toggle",
                                               "ankiscape-hiscores-test-toggle"))
    test_toggle.setToolTip(
        "Synthetic fixture accounts on a separate test leaderboard; "
        "real player ranks are unaffected")
    test_toggle.setAccessibleName("Test leaderboard")
    test_toggle.setVisible(False)
    controls.addWidget(lookup, 1)
    controls.addWidget(test_toggle)
    controls.addWidget(refresh)
    controls.addWidget(sync_btn)
    bp.addLayout(controls)

    # Overall only: order by total XP (the server's ranking) or by total
    # level, with XP breaking ties.
    sort_row_w = QWidget()
    sort_row = QHBoxLayout(sort_row_w)
    sort_row.setContentsMargins(0, 0, 0, 0)
    sort_row.setSpacing(6)
    sort_row.addWidget(muted_label("Sort by"))
    sort_buttons: Dict[str, Any] = {}
    for mode, label in ((hm.SORT_XP, "Total XP"),
                        (hm.SORT_LEVEL, "Total level")):
        btn = QPushButton(label)
        btn.setObjectName(f"ankiscape-hiscores-sort-{mode}")
        btn.setProperty("hsSort", True)
        btn.setCheckable(True)
        btn.setAutoExclusive(True)
        btn.setChecked(mode == hm.SORT_XP)
        btn.setAccessibleName(f"Sort by {label.lower()}")
        btn.setToolTip("Ties in total level are broken by XP"
                       if mode == hm.SORT_LEVEL else
                       "Rank by total XP across all six skills")
        btn.clicked.connect(lambda _c=False, m=mode: _set_sort(m))
        sort_buttons[mode] = btn
        sort_row.addWidget(btn)
    sort_row.addStretch(1)
    bp.addWidget(sort_row_w)

    status_row = QHBoxLayout()
    status_icon = QLabel()
    status_icon.setFixedSize(16, 16)
    status_icon.setVisible(False)
    status_row.addWidget(status_icon)
    status = body_label("")
    status.setObjectName(OBJECT_NAMES["hiscores_status"])
    status_row.addWidget(status, 1)
    bp.addLayout(status_row)

    you = YouBar()
    you.setVisible(False)
    bp.addWidget(you)

    listing = QListWidget()
    listing.setObjectName(OBJECT_NAMES["hiscores_list"])
    listing.setItemDelegate(BoardDelegate(listing))
    listing.setMouseTracking(True)
    listing.setVerticalScrollMode(
        QListWidget.ScrollMode.ScrollPerPixel)
    listing.setToolTip("Click a player to open their card")
    bp.addWidget(listing, 1)

    empty_box = QWidget()
    empty_lay = QVBoxLayout(empty_box)
    empty_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_icon = QLabel()
    empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_lay.addWidget(empty_icon)
    empty = body_label("", wrap=True)
    empty.setObjectName("ankiscape-hiscores-empty")
    empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    empty_lay.addWidget(empty)
    empty_box.setVisible(False)
    bp.addWidget(empty_box, 1)

    note = muted_label("", wrap=True)
    note.setObjectName("ankiscape-hiscores-untrained")
    bp.addWidget(note)
    spacer = QWidget()
    bp.addWidget(spacer, 1)

    card = PlayerCard(lambda: _close_card())
    stack.addWidget(board_page)
    stack.addWidget(card)
    layout.addWidget(panel, 1)

    # ---- signed-out call to action (below the board, never a lock) ---------
    cta = StonePanel()
    cta.setObjectName("ankiscape-hiscores-cta")
    cta_row = QHBoxLayout()
    account_icon = QLabel()
    account_icon.setFixedSize(24, 24)
    account_pix = icon_pixmap(slot_icon_path("hiscores.account"), 24)
    if account_pix is not None:
        account_icon.setPixmap(account_pix)
        account_icon.setAccessibleName("Account")
    cta_row.addWidget(account_icon)
    cta_text = body_label(
        "Want your name on the board? A free account starts your own synced "
        "game and puts you in the rankings. Your offline game on this "
        "computer stays separate and is never uploaded. An account is "
        "optional.", wrap=True)
    cta_row.addWidget(cta_text, 1)
    login_btn = QPushButton("Log in")
    login_btn.setObjectName("ankiscape-hiscores-login")
    register_btn = QPushButton("Create account")
    register_btn.setObjectName("ankiscape-hiscores-register")
    register_btn.setProperty("class", "primary")
    cta_row.addWidget(login_btn)
    cta_row.addWidget(register_btn)
    cta.body.addLayout(cta_row)
    layout.addWidget(cta)

    # ---- helpers -----------------------------------------------------------

    def _account() -> Dict[str, Any]:
        try:
            info = shell.call("get_account", default={}) or {}
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def _thresholds() -> List[int]:
        try:
            rules = shell.call("get_rules", default={}) or {}
            return list(rules.get("thresholds", []) or [])
        except Exception:
            return []

    def _me() -> str:
        acct = _account()
        return str(acct.get("username", "") or "") \
            if acct.get("logged_in") else ""

    def _set_status(text: str, kind: str = "muted", icon_slot: str = ""):
        status.setText(text)
        try:
            status.setAccessibleName(text)
        except Exception:
            pass
        pix = icon_pixmap(slot_icon_path(icon_slot), 16) if icon_slot else None
        if pix is not None:
            status_icon.setPixmap(pix)
            status_icon.setAccessibleName(
                "Pending" if icon_slot == "hiscores.pending" else "Offline")
            status_icon.setVisible(True)
        else:
            status_icon.setVisible(False)
        try:
            status.setProperty(
                "statusKind",
                "error" if kind == "error"
                else ("success" if kind == "ok" else "muted"))
            status.setObjectName(OBJECT_NAMES["hiscores_status"])
            status.style().unpolish(status)
            status.style().polish(status)
        except Exception:
            pass

    def _fmt_time(ts) -> str:
        return _fmt_time_value(ts)

    def _boards_shown():
        return [b for b in hm.BOARDS
                if not (b == hm.OVERALL and (state["cohort"]
                                             or not state["overall_ok"]))]

    def _sync_tabs():
        for board, tab in tabs.items():
            hidden = board == hm.OVERALL and (state["cohort"]
                                              or not state["overall_ok"])
            tab.setVisible(not hidden)
            tab.setChecked(board == state["board"] and not hidden)
        if state["board"] == hm.OVERALL and (state["cohort"]
                                             or not state["overall_ok"]):
            state["board"] = hm.SKILLS[0]
            tabs[state["board"]].setChecked(True)
        sort_row_w.setVisible(state["board"] == hm.OVERALL)

    def _levels_pending() -> bool:
        """Overall levels are computed from the six skill boards; until each
        has loaded (or failed) a missing level is "still coming", not "n/a"."""
        return any(b not in state["boards"] and b not in state["failed"]
                   for b in hm.SKILLS)

    def _index() -> Dict[str, Dict[str, Any]]:
        return hm.player_index({b: rows for b, rows in state["boards"].items()})

    def _moves_for(board: str, rows) -> Dict[str, Any]:
        """Movement against the board as it was at the last visit.

        The remembered snapshot is read once and frozen for the life of this
        screen: comparing against it, not against what this very screen just
        saved, is what keeps arrows from vanishing on the next refresh. A
        newer snapshot is written (for the next visit) only once the old one
        is old enough to mean "last visit".
        """
        if state["cohort"] or not rows:
            return {}
        if state["seen"] is None:
            try:
                loaded = shell.call("get_hiscores_seen", default={}) or {}
            except Exception:
                loaded = {}
            state["seen"] = dict(loaded)         # frozen: what we compare to
            state["seen_out"] = dict(loaded)     # what we will save
        prev = state["seen"].get(board)
        moves = hm.movement(prev, rows)
        if board not in state["rolled"] \
                and hm.should_roll_snapshot(prev, time.time()):
            state["rolled"].add(board)
            state["seen_out"][board] = {"ts": time.time(),
                                        "ranks": hm.snapshot_ranks(rows)}
            try:
                shell.call("save_hiscores_seen", dict(state["seen_out"]))
            except Exception:
                pass
        return moves

    # ---- rendering ---------------------------------------------------------

    def _render():
        board = state["board"]
        th = _thresholds()
        rows = state["boards"].get(board)
        me = _me()
        logged_in = bool(_account().get("logged_in"))
        cta.setVisible(not logged_in)
        sync_btn.setVisible(logged_in)
        listing.clear()
        note.setText("")

        if rows is None:
            you.setVisible(False)
            listing.setVisible(False)
            empty_box.setVisible(False)
            spacer.setVisible(True)
            return

        index = _index()
        enriched = hm.rows_with_levels(board, rows, index, th)
        # Overall can be ordered by total level (XP breaks ties). That needs
        # every total level, so until the six skill boards have loaded the
        # XP order is shown instead of a half-known level order.
        mode = hm.SORT_XP
        view = enriched
        if board == hm.OVERALL and state["sort"] == hm.SORT_LEVEL:
            reranked = hm.rerank_by_level(enriched)
            if reranked is not None:
                mode, view = hm.SORT_LEVEL, reranked
        state["mode_shown"] = mode
        if mode == hm.SORT_LEVEL:
            moves = _moves_for(hm.OVERALL + ":level", view)
        else:
            moves = state["moves"].get(board, {})
        trained, untrained = hm.split_trained(view)
        count, _exact = hm.population(rows)

        # You bar: always shown when signed in.
        if logged_in and me:
            st = (hm.standing_by_level(view, me) if mode == hm.SORT_LEVEL
                  else hm.standing(rows, me, th))
            text = hm.standing_text(st, board)
            if st is not None and st["trained"]:
                my_level = next((r["level"] for r in view
                                 if r["username"].casefold() == me.casefold()),
                                None)
                lvl = ("Total …" if my_level is None and _levels_pending()
                       else hm.level_label(board, my_level))
                figures = f"{lvl}  ·  {hm.format_whole_xp(st['xp'])} XP"
            else:
                figures = ""
            headline = text["headline"]
            mv = moves.get(me.casefold())
            glyph, kind = hm.movement_glyph(mv) if me.casefold() in moves \
                else ("", "flat")
            if kind in ("up", "down"):
                headline += f"  ·  {glyph} since your last visit"
            you.set_content(headline, text["goal"], figures,
                            quiet=not (st and st["trained"]))
            you.setVisible(True)
        else:
            you.setVisible(False)

        if not trained:
            listing.setVisible(False)
            spacer.setVisible(False)
            empty.setText(hm.empty_board_copy(board, count))
            pix = icon_pixmap(board_icon_path(board), 48)
            if pix is not None:
                empty_icon.setPixmap(pix)
            empty_box.setVisible(True)
            note.setText("")
            return
        empty_box.setVisible(False)

        for row in trained:
            key = row["username"].casefold()
            is_you = bool(me) and key == me.casefold()
            move = hm.movement_glyph(moves[key]) if key in moves else ("", "flat")
            level_text = hm.level_label(board, row.get("level"))
            if row.get("level") is None and _levels_pending():
                level_text = "Total …"
            xp_text = hm.format_whole_xp(row.get("xp"))
            medal = hm.MEDALS.get(int(row.get("rank") or 0), "")
            item = QListWidgetItem(
                f"#{row.get('rank')} {hm.display_name(row)} — {level_text} — "
                f"{xp_text} XP" + (" (you)" if is_you else "")
                + (f" — {medal} medal" if medal else ""))
            item.setData(ROLE_ROW, {
                "rank": row.get("rank"), "username": row["username"],
                "is_demo": bool(row.get("is_demo")), "medal": medal,
                "level_text": level_text, "xp_text": xp_text,
                "move": move, "you": is_you})
            item.setData(int(Qt.ItemDataRole.UserRole), row["username"])
            listing.addItem(item)
        has_rest = listing.count() > 0
        listing.setVisible(has_rest)
        spacer.setVisible(not has_rest)
        note.setText(hm.untrained_note(board, len(untrained)))

    def _render_status():
        board = state["board"]
        if state["board"] not in state["boards"]:
            return
        when = _fmt_time(state["fetched"].get(board))
        stale = bool(state["stale"].get(board))
        if stale and state["loading"]:
            # Cached rows are on screen while the fresh ones load: that is
            # not "offline", and saying so on every open would be false.
            _set_status(f"Showing rankings from {when}. Updating…", "muted",
                        "hiscores.pending")
        elif stale:
            _set_status(f"Offline — showing cached rankings from {when}.",
                        "muted", "hiscores.offline")
        else:
            scope = ("Test leaderboard: synthetic test accounts only. "
                     if state["cohort"] else "")
            how = ("Ranked by total level; XP breaks ties."
                   if state["mode_shown"] == hm.SORT_LEVEL
                   else "Ties share a rank.")
            _set_status(f"{scope}Updated {when}. {how}", "ok")

    # ---- loading -----------------------------------------------------------

    def _prime_from_cache():
        """Show what we already have instantly; fresh data replaces it."""
        for b in _boards_shown():
            if b in state["boards"]:
                continue
            cached = shell.call("get_hiscores_cache", b, state["cohort"],
                                default=None)
            if isinstance(cached, dict) and cached.get("rows") is not None:
                state["boards"][b] = list(cached.get("rows") or [])
                state["stale"][b] = True
                state["fetched"][b] = cached.get("fetched_at")
                state["moves"][b] = _moves_for(b, state["boards"][b])

    def _fetch(board: str, prefetch: bool = False):
        """Load one board. The selected board is waited on; the rest load
        quietly, one at a time, so opening Ranks never bursts requests."""
        if state["closed"]:
            return
        epoch = state["epoch"]
        cohort = bool(state["cohort"])
        if prefetch:
            state["inflight"].add(board)
        else:
            state["loading"] = True
            if board not in state["boards"]:
                _set_status("Loading test rankings…" if cohort
                            else "Loading rankings…", "muted",
                            "hiscores.pending")

        def _done(result):
            state["inflight"].discard(board)
            if response_is_stale(closed=state["closed"], request_id=epoch,
                                 current_request_id=state["epoch"]):
                return  # superseded by a refresh, account change or close
            if not prefetch:
                state["loading"] = False
            if not isinstance(result, dict):
                result = {"ok": False, "error": "unexpected response"}
            if result.get("ok"):
                rows = list(result.get("rows") or [])
                state["boards"][board] = rows
                state["stale"][board] = bool(result.get("cached"))
                state["fetched"][board] = result.get("fetched_at")
                state["fetched_at_mono"][board] = time.monotonic()
                state["moves"][board] = _moves_for(board, rows)
                state["failed"].discard(board)
                # Overall levels come from the six skill boards, so Overall
                # repaints as each one arrives.
                if board == state["board"] or state["board"] == hm.OVERALL:
                    _render()
                if board == state["board"]:
                    _render_status()
                _prefetch_next()
                return
            error = str(result.get("error") or "unknown error")
            if board == hm.OVERALL and "bad_skill" in error:
                # A server without the Overall board: drop the tab quietly.
                state["overall_ok"] = False
                state["boards"].pop(hm.OVERALL, None)
                _sync_tabs()
                if state["board"] == hm.OVERALL:
                    _select_board(hm.SKILLS[0])
                _prefetch_next()
                return
            state["failed"].add(board)
            if prefetch:
                _prefetch_next()
            else:
                _render_error(error)

        async_fn = shell.deps.get("query_hiscores_async")
        if callable(async_fn):
            try:
                async_fn(board, hm.BOARD_LIMIT, _done, cohort)
                return
            except Exception as exc:
                _done({"ok": False, "error": repr(exc)})
                return
        sync_fn = shell.deps.get("query_hiscores")
        if callable(sync_fn):
            try:
                rows = sync_fn(board, hm.BOARD_LIMIT, cohort)
                _done({"ok": True, "rows": rows, "fetched_at": time.time()})
            except Exception as exc:
                _done({"ok": False, "error": str(exc)})
            return
        _done({"ok": False, "error": "no query path configured"})

    def _prefetch_next():
        """Load every other board in the background, all at once: Overall
        levels need all six skills, and seven small anonymous reads are far
        cheaper than making people watch "—" for several round trips."""
        if state["closed"]:
            return
        for b in _boards_shown():
            if b in state["failed"] or b in state["inflight"]:
                continue
            if b == state["board"] and state["loading"]:
                continue
            if b in state["boards"] and not state["stale"].get(b):
                continue
            _fetch(b, prefetch=True)

    def _render_error(error: str):
        board = state["board"]
        if board in state["boards"]:
            _render()
            _set_status(
                f"Couldn't refresh ({error[:60]}). Showing cached rankings "
                f"from {_fmt_time(state['fetched'].get(board))}.",
                "error", "hiscores.offline")
            return
        you.setVisible(False)
        listing.setVisible(False)
        empty_box.setVisible(False)
        spacer.setVisible(True)
        note.setText("")
        lower = error.lower()
        if "jwt" in lower or "401" in error or "expired" in lower:
            _set_status("Your session expired. Log in again for your own "
                        "score; public rankings still work.", "error")
        elif "offline" in lower or "connect" in lower or "unconfigured" in lower:
            _set_status("You are offline. Public rankings need a connection; "
                        "your local progress is safe.", "error",
                        "hiscores.offline")
        else:
            _set_status(f"Service problem: {error[:120]}", "error")

    def _reset_epoch():
        state["epoch"] += 1
        state["boards"] = {}
        state["stale"] = {}
        state["fetched"] = {}
        state["moves"] = {}
        state["failed"] = set()
        state["loading"] = False
        state["inflight"] = set()

    def _refresh():
        account = _account()
        key = (str(account.get("username") or ""),
               bool(account.get("logged_in")),
               bool(account.get("is_test")))
        if state["account"] is not None and state["account"] != key:
            _reset_epoch()
            state["cohort"] = False
            state["card_entry"] = None
        state["account"] = key
        logged_in = bool(account.get("logged_in"))
        is_test = bool(account.get("is_test"))
        cta.setVisible(not logged_in)
        sync_btn.setVisible(logged_in)
        # Only server-reported test accounts ever see the test toggle.
        test_toggle.setVisible(logged_in and is_test)
        if not is_test and test_toggle.isChecked():
            test_toggle.blockSignals(True)
            test_toggle.setChecked(False)
            test_toggle.blockSignals(False)
            state["cohort"] = False
        _sync_tabs()
        _prime_from_cache()
        _render()
        _render_status()
        _request_sync_if_useful(account)
        fresh_for = time.monotonic() - float(
            state["fetched_at_mono"].get(state["board"]) or -1e9)
        if not state["loading"] and (fresh_for > 5.0
                                     or state["board"] not in state["boards"]
                                     or state["stale"].get(state["board"])):
            _fetch(state["board"])
        _prefetch_next()

    def _request_sync_if_useful(account: Dict[str, Any]) -> None:
        """Open-the-leaderboard sync request: at most one per short window so
        view refreshes never create a refresh -> sync -> refresh loop."""
        if not account.get("logged_in"):
            return
        now = time.monotonic()
        if now - float(state.get("sync_requested_at") or 0) < 10.0:
            return
        state["sync_requested_at"] = now
        try:
            shell.call("on_sync")
        except Exception:
            pass

    # ---- interactions ------------------------------------------------------

    def _select_board(board: str):
        if board not in hm.BOARDS or state["closed"]:
            return
        if board == hm.OVERALL and (state["cohort"] or not state["overall_ok"]):
            return
        state["board"] = board
        _close_card()
        _sync_tabs()
        if board in state["boards"]:
            _render()
            _render_status()
        else:
            _render()
            _fetch(board)

    def _set_sort(mode: str):
        if mode not in (hm.SORT_XP, hm.SORT_LEVEL) or state["closed"]:
            return
        state["sort"] = mode
        for m, btn in sort_buttons.items():
            btn.setChecked(m == mode)
        _render()
        _render_status()

    def _full_refresh():
        _reset_epoch()
        state["card_entry"] = None
        _fetch(state["board"])

    def _close_card():
        state["card_entry"] = None
        state["card_name"] = ""
        stack.setCurrentIndex(0)

    def _show_card(entry: Dict[str, Any], ranks_known: bool):
        th = _thresholds()
        me = _me()
        index = _index()
        mine = index.get(me.casefold()) if me else None
        is_me = bool(me) and str(entry.get("name", "")).casefold() == me.casefold()
        card.show_entry(hm.card_summary(entry, th), hm.card_rows(entry, th),
                        th, mine, is_me, ranks_known)
        state["card_name"] = str(entry.get("name", ""))
        stack.setCurrentIndex(1)

    def _open_card(name: str):
        if state["closed"] or not name:
            return
        entry = _index().get(str(name).casefold())
        if entry is not None:
            _show_card(entry, True)
            return
        _server_lookup(name)

    def _find_local(text: str) -> Optional[str]:
        """Exact (case-insensitive) match, else a unique prefix."""
        idx = _index()
        needle = text.strip().casefold()
        if not needle:
            return None
        if needle in idx:
            return idx[needle]["name"]
        starts = [e["name"] for k, e in idx.items() if k.startswith(needle)]
        return starts[0] if len(starts) == 1 else None

    def _lookup():
        name = lookup.text().strip()
        if not name or state["closed"]:
            return
        local = _find_local(name)
        if local:
            _open_card(local)
            return
        _server_lookup(name)

    def _server_lookup(name: str):
        state["lookup_seq"] += 1
        seq = state["lookup_seq"]
        epoch = state["epoch"]
        cohort = bool(state["cohort"])
        _set_status(f"Looking up {name}…", "muted", "hiscores.pending")

        def _done(result):
            if state["closed"] or seq != state["lookup_seq"] \
                    or epoch != state["epoch"]:
                return
            if not isinstance(result, dict) or not result.get("ok"):
                not_found = bool((result or {}).get("not_found"))
                _set_status(
                    f"No player named “{name}” was found."
                    if not_found
                    else f"Lookup failed: "
                         f"{str((result or {}).get('error', ''))[:80]}",
                    "muted" if not_found else "error")
                return
            profile = result.get("profile") or {}
            found = str(profile.get("username") or name)
            table = profile.get("skills_xp")
            if not isinstance(table, dict):
                table = {state["board"]: profile.get("xp", 0)}
            entry = hm.entry_from_profile(found, table,
                                          bool(profile.get("is_demo")))
            _render_status()
            _show_card(entry, False)

        async_fn = shell.deps.get("lookup_player_async")
        if callable(async_fn):
            try:
                async_fn(name, state["board"], _done, cohort)
                return
            except Exception as exc:
                _done({"ok": False, "error": repr(exc)})
        else:
            _done({"ok": False, "not_found": True})

    def _on_item(item):
        name = item.data(int(Qt.ItemDataRole.UserRole))
        if name:
            _open_card(str(name))

    def _toggle_cohort(checked):
        state["cohort"] = bool(checked)
        _reset_epoch()
        state["card_entry"] = None
        _close_card()
        _sync_tabs()
        _render()
        _fetch(state["board"])

    login_btn.clicked.connect(lambda: shell.call("on_account"))
    register_btn.clicked.connect(lambda: shell.call("on_register"))
    refresh.clicked.connect(_full_refresh)
    sync_btn.clicked.connect(lambda: shell.call("on_sync"))
    test_toggle.toggled.connect(_toggle_cohort)
    lookup.returnPressed.connect(_lookup)
    listing.itemActivated.connect(_on_item)
    listing.itemClicked.connect(_on_item)

    root.refresh = _refresh
    root.on_show = _refresh
    root.invalidate = lambda: None

    def _release():
        state["closed"] = True
        state["epoch"] += 1

    root.release = _release
    root.deps = deps
    _refresh()
    return root


def _fmt_time_value(ts) -> str:
    try:
        value = float(ts)
        if value <= 0:
            return "earlier"
        return time.strftime("%b %d, %H:%M", time.localtime(value))
    except (TypeError, ValueError):
        return "earlier"


def _fmt_time(ts) -> str:  # kept for callers/tests of the old module surface
    return _fmt_time_value(ts)
