# dev-only E2E driver addon. NEVER SHIPPED (lives under dev/, outside the
# package allowlist). Installed next to the packaged AnkiScape zip in an
# isolated base directory. Drives REAL reviewer inputs (answer buttons'
# underlying _answerCard path), real mw.undo()/mw.redo(), and real collection
# data — never calls award functions directly. Product read paths (reducer
# replay, catch-up scan) are exercised through the shipped modules.
# Protocol with dev.py: journey.json {journey, phase, run_id} in the base.
# A phase ends with relaunch.json {next_phase, run_id} (dev.py relaunches
# into the next phase) or e2e-assertions.json (final, run_id stamped).
import json
import os
import time
import traceback

RESULT = {"journey": "fresh", "phase": 1, "run_id": None, "steps": [],
          "assertions": {}, "screenshots": [], "errors": [],
          "started": time.time()}

RUN_ID = None
JOURNEY = "fresh"
PHASE = 1


def _load_run_id():
    global RUN_ID
    try:
        with open(os.path.join(_base_dir(), "run-id.txt"), encoding="utf-8") as fh:
            RUN_ID = fh.read().strip()
            RESULT["run_id"] = RUN_ID
    except Exception:
        pass


def _load_journey():
    global JOURNEY, PHASE
    try:
        with open(os.path.join(_base_dir(), "journey.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        JOURNEY = str(spec.get("journey", "fresh"))
        PHASE = int(spec.get("phase", 1))
        RESULT["journey"] = JOURNEY
        RESULT["phase"] = PHASE
    except Exception:
        pass


def _base_dir():
    # baseFolder() was removed after Anki 23.10; profileFolder() remains.
    # Base dir = parent of the profile folder; fall back to legacy API.
    from aqt import mw
    try:
        base = mw.pm.baseFolder()  # Anki <= 24.x
        if base:
            return base
    except Exception:
        pass
    return os.path.dirname(mw.pm.profileFolder())


def _out_path(name):
    return os.path.join(_base_dir(), f"e2e-{name}")


def _step(name, ok, detail=""):
    RESULT["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    if not ok:
        RESULT["errors"].append(f"{name}: {detail}"[:500])


def _step_once(state, key, name, ok, detail=""):
    """Record a stage-transition step only the first time (avoids spam when
    a later check in the same stage keeps failing)."""
    if state.get(f"step_{key}"):
        return
    state[f"step_{key}"] = True
    _step(name, ok, detail)


def _shot(name):
    try:
        from aqt import mw
        path = _out_path(f"{name}.png")
        mw.grab().save(path)
        RESULT["screenshots"].append(path)
    except Exception as exc:
        RESULT["errors"].append(f"screenshot {name}: {exc!r}")


def _finish(exit_code):
    RESULT["finished"] = time.time()
    try:
        with open(_out_path("assertions.json"), "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, indent=2)
    except Exception:
        pass
    _quit(exit_code)


def _finish_phase(next_phase):
    """End a non-final phase: request relaunch into next_phase, then quit."""
    try:
        with open(os.path.join(_base_dir(), "relaunch.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"next_phase": int(next_phase), "run_id": RUN_ID}, fh)
    except Exception:
        pass
    _quit(0)


def _quit(exit_code):
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    try:
        from aqt.qt import QApplication
        from aqt import mw
        mw.close()
        QApplication.instance().quit()
    except Exception:
        pass
    raise SystemExit(exit_code)


def _fail(msg):
    _step("fatal", False, msg)
    _shot("fatal")
    _finish(1)


def run():
    from aqt import mw
    from aqt.qt import QTimer
    try:
        profile = str(mw.pm.name)
    except Exception:
        profile = ""
    if not profile.startswith("e2e-"):
        return  # only act in E2E profiles
    _load_run_id()
    _load_journey()

    state = {"answered": 0, "target": 5, "ticks": 0}

    # Independent popup watchdog: Classic celebration dialogs are MODAL and
    # open *inside* the driver's own answer call stack, which freezes the
    # journey tick until someone clicks OK. This repeating timer fires in
    # nested modal loops too, so dismissal never depends on the stuck tick.
    def _popup_watchdog():
        try:
            _dismiss_updater()
            _dismiss_product_popups(state)
        except Exception:
            pass
        try:
            QTimer.singleShot(500, _popup_watchdog)
        except Exception:
            pass

    def _beat(note):
        try:
            extra = {}
            try:
                from aqt import mw as _mw
                from aqt.qt import QApplication as _QApp
                extra["mw_state"] = getattr(_mw, "state", "?")
                reviewer = getattr(_mw, "reviewer", None)
                extra["reviewer_state"] = getattr(reviewer, "state", "?") if reviewer else None
                extra["top"] = sorted({w.objectName() or type(w).__name__
                                       for w in _QApp.topLevelWidgets()})[:20]
                extra["seeded"] = bool(state.get("seeded"))
                try:
                    extra["requested"] = _mw.col.get_config(
                        "ankiscape_mode_requested", "MISSING") if _mw.col else None
                except Exception as exc2:
                    extra["requested"] = f"ERR {exc2!r}"[:100]
                dialogs = []
                for w in _QApp.topLevelWidgets():
                    try:
                        if w.isVisible() and type(w).__name__ in (
                                "QMessageBox", "QDialog", "QErrorMessage"):
                            texts = []
                            try:
                                from aqt.qt import QLabel as _QL
                                for lab in w.findChildren(_QL):
                                    text = lab.text()
                                    if text:
                                        texts.append(text[:160])
                            except Exception:
                                pass
                            try:
                                from aqt.qt import (QAbstractButton as _QB,
                                                    QTextEdit as _QTE)
                                for btn in w.findChildren(_QB):
                                    if btn.text():
                                        texts.append("[btn] " + btn.text()[:80])
                                for ted in w.findChildren(_QTE):
                                    if ted.toPlainText():
                                        texts.append(
                                            ted.toPlainText()[:400])
                            except Exception:
                                pass
                            dialogs.append({"kind": type(w).__name__,
                                            "name": w.objectName(),
                                            "windowTitle": str(w.windowTitle())[:120],
                                            "labels": texts[:8]})
                    except Exception:
                        continue
                extra["dialogs"] = dialogs[:4]
                try:
                    import ankiscape as _pkg
                    _rt = _pkg._runtime_mod.get_runtime()
                    extra["adapter"] = getattr(_rt.active_adapter, "name", "?")
                    extra["profile_loaded"] = _rt.profile_loaded
                    _eng = _pkg._EVOLVED_CTX.get("engine")
                    extra["engine"] = bool(_eng)
                    extra["recent"] = len(_eng.state.recent) if _eng else -1
                    if _eng is not None:
                        extra["eng_id"] = id(_eng)
                        extra["eng_game"] = (_eng.cfg.game_uuid or "")[:8]
                        extra["eng_processed"] = len(_eng.state.processed_keys)
                        try:
                            _ops = _eng.journal._conn.execute(
                                "select count(*) from operations").fetchone()[0]
                        except Exception:
                            _ops = -1
                        extra["eng_ops"] = _ops
                    extra["undo_fn"] = getattr(getattr(_mw, "undo", None),
                                              "__name__", "?")
                    # Direct reconcile probe: same product path the mw.undo
                    # wrapper calls. Distinguishes wrapper wiring faults from
                    # engine input faults.
                    if _eng is not None and _mw.col is not None:
                        _db = _mw.col.db
                        _detail = {}
                        for _k, (_rid, _cid) in list(_eng.state.recent.items())[:5]:
                            try:
                                _present = _db.scalar(
                                    "select 1 from revlog where id = ?",
                                    int(_rid)) is not None
                            except Exception as _exc:
                                _present = f"ERR { _exc!r}"[:120]
                            _detail[_k[:8]] = {"rid": _rid, "present": _present}
                        extra["recent_detail"] = _detail
                except Exception as exc3:
                    extra["runtime_probe"] = repr(exc3)[:200]
            except Exception as exc:
                extra["beat_error"] = repr(exc)[:200]
            with open(_out_path("heartbeat.json"), "w", encoding="utf-8") as fh:
                json.dump({"run_id": RUN_ID, "journey": JOURNEY, "phase": PHASE,
                           "ticks": state["ticks"], "answered": state["answered"],
                           "state_id": id(state),
                           "calls": state.get("calls", 0),
                           "show_calls": state.get("show_calls", 0),
                           "answer_calls": state.get("answer_calls", 0),
                           "show_diag": state.get("show_diag"),
                           "stage": state.get("stage"),
                           "drive_note": state.get("drive_note"),
                           "trace": list(state.get("trace", [])),
                           "seeded": bool(state.get("seeded")),
                           "chooser_done": bool(state.get("chooser_done")),
                           "awaiting": state.get("awaiting", 0),
                           "retries": state.get("retries", 0),
                           "steps": len(RESULT["steps"]),
                           "last_step": (RESULT["steps"][-1] if RESULT["steps"]
                                         else None),
                           "note": note, "time": time.time(), **extra}, fh)
        except Exception:
            pass

    def tick():
        try:
            state["ticks"] += 1
            if state["ticks"] == 1:
                _beat("first_tick")
            if state["ticks"] > 1200:  # ~120 s watchdog per phase
                return _fail("watchdog: journey did not finish in time")
            if state["ticks"] % 150 == 0:
                _shot(f"stuck-{state['ticks']}")
            poll()
        except SystemExit:
            raise
        except Exception:
            _fail("driver exception: " + traceback.format_exc()[-2000:])
        else:
            _beat("poll_ok")
            QTimer.singleShot(100, tick)

    def poll():
        _dismiss_updater()
        _dismiss_product_popups(state)
        fn = _PHASE_POLLS.get((JOURNEY, PHASE))
        if fn is None:
            return _fail(f"unknown journey phase {(JOURNEY, PHASE)}")
        fn(state)

    QTimer.singleShot(1500, tick)
    try:
        QTimer.singleShot(2000, _popup_watchdog)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Shared building blocks (real inputs, read-only inspection otherwise)
# --------------------------------------------------------------------------

def _dismiss_updater():
    """Close Anki's 'Anki Updated' notifier if present. Anything else modal is
    left alone (screenshotted via heartbeat) so product errors stay visible."""
    try:
        from aqt.qt import QApplication, QLabel, QMessageBox
        for widget in QApplication.topLevelWidgets():
            try:
                if not (isinstance(widget, QMessageBox) and widget.isVisible()):
                    continue
                texts = [widget.windowTitle()] + [
                    lab.text() for lab in widget.findChildren(QLabel)]
                if any("Anki Updated" in (text or "") for text in texts):
                    widget.close()
            except Exception:
                continue
    except Exception:
        pass


def _dismiss_product_popups(state):
    """Click OK on expected Classic celebration dialogs (Level Up!,
    Achievement Unlocked!). These are normal product UI on the review path —
    a human clicks through them; the driver must too, or the modal nested
    loop wedges the run. Each dismissal is recorded (visibility, not
    masking); anything unexpected still wedges loudly into the watchdog."""
    try:
        from aqt.qt import QApplication, QDialog, QPushButton
        for widget in QApplication.topLevelWidgets():
            try:
                if not (isinstance(widget, QDialog) and widget.isVisible()):
                    continue
                title = str(widget.windowTitle() or "")
                if title not in ("Level Up!", "Achievement Unlocked!"):
                    continue
                ok = None
                for btn in widget.findChildren(QPushButton):
                    try:
                        if btn.text() == "OK":
                            ok = btn
                            break
                    except Exception:
                        continue
                if ok is not None:
                    ok.click()
                    n = state.get("popups_dismissed", 0) + 1
                    state["popups_dismissed"] = n
                    _step("product_popup_dismissed", True, f"{title} (#{n})")
            except Exception:
                continue
    except Exception:
        pass


def _find_chooser():
    # Only a VISIBLE dialog counts: accepted dialogs may linger hidden until
    # Qt collects them, and must not be "accepted" again forever.
    try:
        from aqt.qt import QApplication
        for widget in QApplication.topLevelWidgets():
            try:
                if (widget.objectName() == "ankiscape-mode-chooser"
                        and widget.isVisible()):
                    return widget
            except Exception:
                continue
    except Exception:
        pass
    return None


def _accept_chooser(chooser, want="evolved"):
    # Never fake-pass: if the radio is missing we report it and retry.
    try:
        from aqt.qt import QRadioButton, QWidget
        target = ("ankiscape-chooser-evolved" if want == "evolved"
                  else "ankiscape-chooser-classic")
        radio = None
        for child in chooser.findChildren(QWidget):
            try:
                if child.objectName() == target:
                    radio = child
                    break
            except Exception:
                continue
        if not isinstance(radio, QRadioButton):
            _step("chooser_radio", False,
                  f"{want} radio not found under {type(chooser).__name__}")
            return False
        radio.setChecked(True)
        _step("chooser_radio", True, f"want={want} checked={radio.isChecked()}")
        chooser.accept()
        return True
    except Exception as exc:
        _step("chooser_accept", False, repr(exc))
        return False


def _poll_chooser(state, want="evolved"):
    """Returns True once no chooser is open. Fails on unexpected choosers."""
    if state.get("chooser_done"):
        return True
    # A previous launch (or phase) may already have persisted the choice:
    # never wait for a dialog that has served its purpose.
    try:
        from aqt import mw as _mw2
        if _mw2.col is not None:
            existing = _mw2.col.get_config("ankiscape_mode_requested", None)
            if existing in ("classic", "evolved"):
                state["chooser_done"] = True
                if existing != want:
                    _step("chooser_already_set", False,
                          f"persisted {existing!r}, journey wants {want!r}")
                    _shot("wrong-mode")
                    _finish(1)
                _step("chooser_already_set", True, f"got {existing!r}")
                return True
    except SystemExit:
        raise
    except Exception:
        pass
    chooser = _find_chooser()
    if chooser is None:
        return bool(state.get("chooser_seen"))
    _step("chooser_shown", True)
    _shot("chooser")
    state["chooser_seen"] = True
    if _accept_chooser(chooser, want):
        _step(f"chooser_{want}_accepted", True)
        state["chooser_done"] = True
        return True
    return False


def _seed_deck(state, deck="E2E Deck", count=5, prefix="E2E"):
    from aqt import mw
    col = mw.col
    if col is None:
        return False
    try:
        deck_id = col.decks.id(deck)
        col.decks.select(deck_id)
        model = col.models.by_name("Basic")
        if model is None:
            return False
        existing = col.find_cards(f'deck:"{deck}"')
        need = count - len(existing)
        for i in range(max(0, need)):
            note = col.new_note(model)
            note["Front"] = f"{prefix} front {len(existing) + i}"
            note["Back"] = f"{prefix} back {len(existing) + i}"
            col.add_note(note, deck_id)
        state["seeded"] = True
        _step("seeded", True, f"{deck}={len(existing) + max(0, need)}")
        return True
    except Exception as exc:
        _step("seed_error", False, repr(exc))
        return False


def _poll_answer(state, deck="E2E Deck", target=5, ease=3):
    """Counter-driven answering (legacy). Anki schedules asynchronously, so
    new flows must gate on product truth instead: _answer_until_awards for
    Evolved (journal ops appear only after scheduling commits) and
    _answer_until_revlog for Classic."""
    return _drive_answer(state, ease) and state.get("answered", 0) >= target


def _trace(state, msg):
    # Ring for the heartbeat plus an append-only JSONL log: phase relaunches
    # and watchdog kills must never lose the event history.
    line = f"{state.get('ticks', '?')}:{msg}"
    try:
        ring = state.setdefault("trace", [])
        ring.append(line)
        del ring[:-8]
    except Exception:
        pass
    try:
        with open(_out_path("trace.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _drive_answer(state, ease=3):
    """Navigate, reveal, and invoke one real answer when settled. Returns
    True if _answerCard was invoked this tick. Single-flight: at most one
    outstanding call — the caller gates on observable effects."""
    from aqt import mw
    _trace(state, "drive-enter")
    if state.get("awaiting"):
        state["drive_note"] = "awaiting"
        _trace(state, "awaiting-set")
        return False
    try:
        current = getattr(mw, "state", "")
        if current != "review":
            # Retry the navigation periodically: a wedged reviewer may swallow
            # a single moveToState, and latching forever would hang the run.
            # (No reviewer object may exist yet outside review state.)
            ticks = state.get("nav_ticks", 0) + 1
            state["nav_ticks"] = ticks
            if ticks == 1 or ticks % 50 == 0:
                mw.moveToState("review")
            state["drive_note"] = f"navigating:{current}"
            _trace(state, f"nav:{current}")
            return False
        state["nav_ticks"] = 0
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            state["drive_note"] = "no-reviewer"
            _trace(state, "no-reviewer")
            return False
        rst = getattr(reviewer, "state", "?") if current == "review" else "?"
        if state.get("awaiting") and (current != "review" or rst != "answer"):
            # The outstanding call landed (reviewer moved on); free the
            # single-flight slot. Gated helpers additionally require the
            # persisted effect before advancing.
            state["awaiting"] = 0
            state["awaiting_ticks"] = 0
        if current != "review":
            # Retry the navigation periodically: a wedged reviewer may swallow
            # a single moveToState, and latching forever would hang the run.
            ticks = state.get("nav_ticks", 0) + 1
            state["nav_ticks"] = ticks
            if ticks == 1 or ticks % 50 == 0:
                mw.moveToState("review")
            return False
        state["nav_ticks"] = 0
        if not _reviewer_settled():
            state["drive_note"] = "unsettled"
            _trace(state, "unsettled")
            return False
        rst = getattr(reviewer, "state", "")
        if rst == "question":
            show = getattr(reviewer, "_showAnswer", None)
            if callable(show):
                before = (getattr(mw, "state", "?"), getattr(reviewer, "state", "?"))
                try:
                    show()
                    after = (getattr(mw, "state", "?"), getattr(reviewer, "state", "?"))
                    state["show_calls"] = state.get("show_calls", 0) + 1
                except Exception as exc:
                    after = f"RAISED {exc!r}"[:160]
                state["show_diag"] = f"{before} -> {after}"
                state["drive_note"] = "show-called"
            else:
                state["drive_note"] = "show-missing"
            return False
        if rst == "answer":
            card = getattr(reviewer, "card", None)
            answer = getattr(reviewer, "_answerCard", None)
            if card is not None and callable(answer):
                state["awaiting"] = state.get("calls", 0) + 1
                answer(ease)  # Good, through the real reviewer path
                state["calls"] = state.get("calls", 0) + 1
                state["answer_calls"] = state.get("answer_calls", 0) + 1
                state["drive_note"] = "answer-called"
                return True
            state["drive_note"] = f"no-card-or-answer:{card is not None}"
            _trace(state, f"no-card:{card is not None}")
            return False
        _trace(state, f"rst-other:{rst}")
        state["drive_note"] = f"rst-other:{rst}"
        return False
    except Exception as exc:
        _step("answer_error", False, repr(exc))
    _trace(state, "drive-end-unreached")
    return False


def _answer_until_awards(state, want_awards, deck="E2E Deck", ease=3, what="awards"):
    """Answer until the journal holds want_awards review_awards. One flight at
    a time; a call whose effect never lands gets ONE retry, then loud fail."""
    try:
        current = len([o for o in _journal_ops() if o["kind"] == "review_award"])
    except Exception:
        current = -1
    if current >= want_awards:
        state["awaiting"] = 0
        state["retries"] = 0
        return True
    # Forward progress frees the single-flight slot even before the total is
    # reached; otherwise all answers serialize behind one 60 s retry window.
    if "last_awards" not in state:
        state["last_awards"] = current
    if current > state["last_awards"]:
        state["awaiting"] = 0
        state["awaiting_ticks"] = 0
        state["last_awards"] = current
    if not state.get("awaiting"):
        if state.get("retries", 0) > 1:
            _step(what, False,
                  f"award never persisted after calls (journal={current})")
            return None
        _drive_answer(state, ease)
        return False
    # A call is outstanding: only conclude it is lost after a long window.
    state["awaiting_ticks"] = state.get("awaiting_ticks", 0) + 1
    if state["awaiting_ticks"] > 150:
        state["awaiting"] = 0
        state["awaiting_ticks"] = 0
        state["retries"] = state.get("retries", 0) + 1
    return False


def _answer_until_revlog(state, want_rows, deck="E2E Deck", ease=3, what="revlog"):
    """Classic equivalent: gate on revlog row count (no journal in Classic)."""
    current = _revlog_count()
    if current >= want_rows:
        state["awaiting"] = 0
        state["retries"] = 0
        return True
    if "last_revlog" not in state:
        state["last_revlog"] = current
    if current > state["last_revlog"]:
        state["awaiting"] = 0
        state["awaiting_ticks"] = 0
        state["last_revlog"] = current
    if not state.get("awaiting"):
        if state.get("retries", 0) > 1:
            _step(what, False, f"revlog stuck at {current}, want {want_rows}")
            return None
        _trace(state, f"gate:revlog={current}:drive")
        _drive_answer(state, ease)
        return False
    _trace(state, f"gate:revlog={current}:wait")
    state["awaiting_ticks"] = state.get("awaiting_ticks", 0) + 1
    if state["awaiting_ticks"] > 150:
        state["awaiting"] = 0
        state["awaiting_ticks"] = 0
        state["retries"] = state.get("retries", 0) + 1
    return False


def _reviewer_settled():
    """True when review can proceed: either outside the reviewer (stale
    reviewer.state is meaningless there) or the reviewer is out of
    transition. Humans cannot click faster than this; the driver must not
    fire the next op mid-flight."""
    try:
        from aqt import mw
        if getattr(mw, "state", "") != "review":
            return True
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            return True
        return getattr(reviewer, "state", "?") != "transition"
    except Exception:
        return False


def _poll_settle(state):
    """Generic settle gate: advances to state['settle_next'] once the
    reviewer leaves transition; recovers (deckBrowser round-trip) a bounded
    number of times, then fails loudly."""
    state["polls"] = state.get("polls", 0) + 1
    if _reviewer_settled():
        state["polls"] = 0
        state["recoveries"] = 0
        state["stage"] = state.get("settle_next", state.get("stage"))
        return
    if state["polls"] > 150:
        if state.get("recoveries", 0) >= 3:
            _step("reviewer_settled", False, "stuck in transition after op")
            return
        state["recoveries"] = state.get("recoveries", 0) + 1
        state["polls"] = 0
        try:
            from aqt import mw
            mw.moveToState("deckBrowser")
        except Exception:
            pass


def _revlog_count():
    try:
        from aqt import mw
        return mw.col.db.scalar("select count(*) from revlog") or 0
    except Exception:
        return -1


def _journal_path():
    import glob
    from aqt import mw
    found = glob.glob(os.path.join(_base_dir(), mw.pm.name,
                                   "ankiscape-evolved", "*", "game.sqlite3"))
    return found[0] if len(found) == 1 else ""


def _journal_ops():
    import json as _json
    import sqlite3
    path = _journal_path()
    if not path:
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT op_id, kind, payload_json FROM operations"
            " ORDER BY lamport, device_id, device_seq, op_id").fetchall()
    finally:
        conn.close()
    return [{"op_id": r[0], "kind": r[1], "payload": _json.loads(r[2])} for r in rows]


def _wait_journal_kinds(state, kinds, what):
    """Gate on product truth: wait until the journal holds the expected op
    multiset (Anki schedules asynchronously; polling revlog timing races).
    Returns True once matched, False after the budget with a recorded step."""
    state["polls"] = state.get("polls", 0) + 1
    try:
        have = sorted(o["kind"] for o in _journal_ops())
    except Exception:
        have = []
    if have == sorted(kinds):
        state["polls"] = 0
        _step(what, True, f"kinds={have}")
        return True
    if state.get("polls", 0) > 150:
        _step(what, False, f"have={have} want={sorted(kinds)}")
        return None  # failed
    return False  # keep waiting


def _game_uuid():
    from aqt import mw
    pointer = mw.col.get_config("ankiscape_evolved_player_data", None) or {}
    return pointer.get("game_uuid", "")


def _projection():
    import ankiscape.evolved.data as _data
    import ankiscape.evolved.reducer as _reducer
    import json as _json
    import sqlite3
    conn = sqlite3.connect(_journal_path())
    try:
        rows = conn.execute(
            "SELECT op_id, device_id, device_seq, lamport, kind, payload_json"
            " FROM operations").fetchall()
    finally:
        conn.close()
    full = [{"op_id": r[0], "game_uuid": _game_uuid(), "device_id": r[1],
             "device_seq": r[2], "lamport": r[3], "kind": r[4],
             "payload": _json.loads(r[5])} for r in rows]
    return _reducer.replay(full, _data.load_rules(), _game_uuid())


def _award_count():
    try:
        return len([o for o in _journal_ops() if o["kind"] == "review_award"])
    except Exception:
        return -1


def _classic_snapshot():
    from aqt import mw
    snap = {}
    for key in ("ankiscape_player_data", "ankiscape_current_skill"):
        try:
            snap[key] = json.dumps(mw.col.get_config(key, None), sort_keys=True)
        except Exception as exc:
            snap[key] = f"ERR {exc!r}"
    return snap


def _save_snapshot(name, snap):
    try:
        with open(os.path.join(_base_dir(), name), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
    except Exception:
        pass


def _load_snapshot(name):
    try:
        with open(os.path.join(_base_dir(), name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------
# Journey: fresh
# --------------------------------------------------------------------------

def _poll_fresh(state):
    if not _poll_chooser(state, "evolved"):
        return
    if not state.get("seeded"):
        _seed_deck(state, count=5)
        return
    result = _answer_until_awards(state, 5, what="five_awards")
    if result is None:
        return
    if not result:
        return
    _step("five_awards", True)
    _verify_fresh(state)


def _verify_fresh(state):
    from aqt import mw
    col = mw.col
    ok = True

    def _assert(name, cond, detail=""):
        nonlocal ok
        _step(name, cond, detail)
        if not cond:
            ok = False

    try:
        requested = col.get_config("ankiscape_mode_requested", None)
        _assert("requested_mode", requested == "evolved", f"got {requested!r}")
    except Exception as exc:
        _assert("requested_mode", False, repr(exc))
    try:
        # Answered new cards enter LEARNING (1m/10m steps), so they remain
        # technically due. The honest assertion: no card is still NEW.
        remaining = col.find_cards('deck:"E2E Deck" is:new')
        _assert("all_introduced", len(remaining) == 0, f"new={len(remaining)}")
    except Exception as exc:
        _assert("all_introduced", False, repr(exc))
    try:
        ops = _journal_ops()
        kinds = [o["kind"] for o in ops]
        _assert("five_review_awards", kinds == ["review_award"] * 5,
                f"kinds={kinds}")
    except Exception as exc:
        _assert("journal_read", False, repr(exc))
    try:
        from aqt.qt import QApplication
        huds = [w for w in QApplication.topLevelWidgets()
                if w.objectName() in ("ankiscape-evolved-hud",)]
        _assert("hud_noted", True, f"evolved_huds={len(huds)}")
    except Exception as exc:
        _assert("hud_noted", False, repr(exc))
    try:
        import sys
        pkg = sys.modules.get("ankiscape")
        opener = getattr(pkg, "runtime_menu_opener", None) if pkg else None
        _assert("menu_opener_present", callable(opener))
    except Exception as exc:
        _assert("menu_opener_present", False, repr(exc))
    _shot("reviewer_done")
    _finish(0 if ok else 1)


# --------------------------------------------------------------------------
# Journey: upgrade (Classic -> Evolved -> Classic across relaunches)
# --------------------------------------------------------------------------

def _poll_upgrade_1(state):
    if not _poll_chooser(state, "classic"):
        return
    if not state.get("fixture"):
        try:
            from aqt import mw
            with open(os.path.join(_base_dir(), "classic-fixture.json"),
                      encoding="utf-8") as fh:
                fixture = json.load(fh)
            mw.col.set_config("ankiscape_player_data", fixture["player_data"])
            mw.col.set_config("ankiscape_current_skill",
                              fixture.get("current_skill", "Mining"))
            state["fixture"] = True
            _step("classic_fixture", True)
        except Exception as exc:
            _step("classic_fixture", False, repr(exc))
            return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=3, prefix="UPG")
        return
    # A real upgrade restarts into the seeded data (product load path reads
    # the config into globals); simulate exactly that with a relaunch.
    _shot("upgrade-seeded")
    _finish_phase(2)


def _poll_upgrade_2(state):
    # Fresh process: Classic must have loaded the seeded fixture itself.
    from aqt import mw
    if _find_chooser() is not None:
        _step("no_unexpected_chooser", False, "chooser appeared in phase 2")
        return
    if not state.get("loaded"):
        try:
            got = (mw.col.get_config("ankiscape_player_data", {}) or {}).get(
                "mining_exp", None)
            if got != 50000:
                _step("classic_loaded", False, f"mining_exp={got!r}")
                return
            _step("classic_loaded", True)
            state["loaded"] = True
            state["classic_before"] = got
            state["revlog_before"] = _revlog_count()
        except Exception as exc:
            _step("classic_loaded", False, repr(exc))
            return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=3, prefix="UPG")
        return
    from aqt import mw as _mw2
    result = _answer_until_revlog(state, state["revlog_before"] + 3,
                                  what="classic_three_answers")
    if result is None:
        return
    if not result:
        return
    _step_once(state, "answered3", "classic_three_answers", True)
    try:
        after = (_mw2.col.get_config("ankiscape_player_data", {}) or {}).get(
            "mining_exp", 0)
        if after <= state["classic_before"]:
            _step("classic_awards", False,
                  f"before={state['classic_before']} after={after}")
            return
        _step("classic_awards", True,
              f"mining_exp {state['classic_before']} -> {after}")
        _save_snapshot("classic-snapshot.json", _classic_snapshot())
        _mw2.col.set_config("ankiscape_mode_requested", "evolved")
        _step("switch_to_evolved", True)
    except Exception as exc:
        _step("classic_verify", False, repr(exc))
        return
    _shot("upgrade-classic-done")
    _finish_phase(3)


def _poll_upgrade_3(state):
    from aqt import mw
    if _find_chooser() is not None:
        _step("no_unexpected_chooser", False, "chooser appeared in phase 3")
        return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=5, prefix="UPG")
        return
    if not state.get("classic_checked"):
        try:
            snap = _load_snapshot("classic-snapshot.json")
            now = _classic_snapshot()
            _step("classic_untouched",
                  now == snap, "Classic keys changed under Evolved!" if now != snap else "")
            if now != snap:
                return
            state["classic_checked"] = True
        except Exception as exc:
            _step("classic_untouched", False, repr(exc))
            return
    state.setdefault("awards_before", _award_count())
    result = _answer_until_awards(state, state["awards_before"] + 2,
                                  what="evolved_two_awards")
    if result is None:
        return
    if not result:
        return
    _step("evolved_two_awards", True)
    try:
        ops = _journal_ops()
        awards = [o for o in ops if o["kind"] == "review_award"]
        if len(awards) != state["awards_before"] + 2:
            _step("evolved_fresh_awards", False, f"awards={len(awards)}")
            return
        _step("evolved_fresh_awards", True)
        proj = _projection()
        mining_xp = proj["xp_micro"].get("mining", 0)
        if mining_xp <= 0 or mining_xp > 2000 * 1000000:
            _step("evolved_fresh_state", False, f"mining_xp_micro={mining_xp}")
            return
        _step("evolved_fresh_state", True, f"mining_xp_micro={mining_xp}")
        if _classic_snapshot() != _load_snapshot("classic-snapshot.json"):
            _step("classic_still_untouched", False, "Evolved reviews touched Classic!")
            return
        _step("classic_still_untouched", True)
        mw.col.set_config("ankiscape_mode_requested", "classic")
    except Exception as exc:
        _step("evolved_verify", False, repr(exc))
        return
    _shot("upgrade-evolved-done")
    _finish_phase(4)


def _poll_upgrade_4(state):
    from aqt import mw
    if _find_chooser() is not None:
        _step("no_unexpected_chooser", False, "chooser appeared in phase 4")
        return
    if not state.get("checked"):
        try:
            snap = _load_snapshot("classic-snapshot.json")
            now = _classic_snapshot()
            if now != snap:
                _step("classic_preserved", False, "Classic state changed across modes!")
                return
            _step("classic_preserved", True)
            state["classic_before"] = (
                mw.col.get_config("ankiscape_player_data", {}) or {}).get("mining_exp", 0)
            state["checked"] = True
        except Exception as exc:
            _step("classic_preserved", False, repr(exc))
            return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=6, prefix="UPG")
        return
    state.setdefault("revlog_before", _revlog_count())
    result = _answer_until_revlog(state, state["revlog_before"] + 1,
                                  what="classic_resume_answer")
    if result is None:
        return
    if not result:
        return
    _step("classic_resume_answer", True)
    try:
        after = (mw.col.get_config("ankiscape_player_data", {}) or {}).get("mining_exp", 0)
        _step("classic_resumed", after > state["classic_before"],
              f"{state['classic_before']} -> {after}")
    except Exception as exc:
        _step("classic_resumed", False, repr(exc))
        return
    _shot("upgrade-classic-back")
    _finish(0 if not RESULT["errors"] else 1)


# --------------------------------------------------------------------------
# Journey: undo (real mw.undo/mw.redo, retract/restore/re-answer)
# --------------------------------------------------------------------------

def _poll_undo(state):
    if not _poll_chooser(state, "evolved"):
        return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=3, prefix="UNDO")
        return
    from aqt import mw
    stage = state.get("stage", "answer")
    if stage == "answer":
        result = _answer_until_awards(state, 1, what="one_award")
        if result is None:
            return
        if not result:
            return
        _step("one_award", True)
        try:
            proj = _projection()
            state["xp1"] = proj["xp_micro"].get("mining", 0)
            if state["xp1"] <= 0:
                _step("xp_positive", False, f"xp={state['xp1']}")
                return
            _step("xp_positive", True, f"xp_micro={state['xp1']}")
            state["settle_next"] = "do_undo"
            state["stage"] = "settle"
            state["polls"] = 0
        except Exception as exc:
            _step("undo_answer", False, repr(exc))
        return
    if stage == "settle":
        _poll_settle(state)
        return
    if stage == "do_undo":
        try:
            if not callable(getattr(mw, "undo", None)):
                _step("mw_undo_present", False, "mw.undo missing")
                return
            mw.undo()  # REAL Anki Undo
            state["stage"] = "retracted"
            state["polls"] = 0
        except Exception as exc:
            _step("undo_answer", False, repr(exc))
        return
    if stage == "retracted":
        # Post-commit hook reconciles asynchronously; gate on the op.
        result = _wait_journal_kinds(
            state, ["review_award", "review_retract"], "retraction_recorded")
        if result is None:
            return
        if not result:
            return
        try:
            proj = _projection()
            if proj["xp_micro"].get("mining", -1) != 0:
                _step("xp_retracted", False,
                      f"mining={proj['xp_micro'].get('mining')}")
                return
            _step("xp_retracted", True)
        except Exception as exc:
            _step("xp_retracted", False, repr(exc))
            return
        state["settle_next"] = "do_redo"
        state["stage"] = "settle"
        state["polls"] = 0
        return
    if stage == "do_redo":
        try:
            if not callable(getattr(mw, "redo", None)):
                _step("mw_redo_present", False, "mw.redo missing")
                return
            mw.redo()  # REAL Anki Redo
            state["stage"] = "restored"
            state["polls"] = 0
        except Exception as exc:
            _step("undo_redo", False, repr(exc))
        return
    if stage == "restored":
        result = _wait_journal_kinds(
            state, ["review_award", "review_restore", "review_retract"],
            "restore_recorded")
        if result is None:
            return
        if not result:
            return
        try:
            proj = _projection()
            if proj["xp_micro"].get("mining", -1) != state["xp1"]:
                _step("xp_restored", False,
                      f"got={proj['xp_micro'].get('mining')} want={state['xp1']}")
                return
            _step("xp_restored", True)
        except Exception as exc:
            _step("xp_restored", False, repr(exc))
            return
        state["settle_next"] = "do_undo2"
        state["stage"] = "settle"
        state["polls"] = 0
        return
    if stage == "do_undo2":
        try:
            mw.undo()  # retract again so the replacement can earn once
            state["stage"] = "retracted2"
            state["polls"] = 0
        except Exception as exc:
            _step("undo_redo2", False, repr(exc))
        return
    if stage == "retracted2":
        result = _wait_journal_kinds(
            state, ["review_award", "review_restore",
                    "review_retract", "review_retract"],
            "retraction2_recorded")
        if result is None:
            return
        if not result:
            return
        _shot("undo-retracted")
        _finish_phase(2)


def _poll_undo_2(state):
    # Fresh process: the retracted award persisted; the replacement answer
    # earns once against the reconciled state.
    from aqt import mw
    if _find_chooser() is not None:
        _step("no_unexpected_chooser", False, "chooser appeared in phase 2")
        return
    if not state.get("checked"):
        try:
            ops = _journal_ops()
            kinds = sorted(o["kind"] for o in ops)
            if "review_retract" not in kinds or "review_award" not in kinds:
                _step("retract_persisted", False, f"kinds={kinds}")
                return
            _step("retract_persisted", True)
            state["checked"] = True
        except Exception as exc:
            _step("retract_persisted", False, repr(exc))
            return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=3, prefix="UNDO")
        return
    if not _reviewer_settled():
        # Fresh process: a lingering transition here is a real problem.
        state["polls"] = state.get("polls", 0) + 1
        if state["polls"] > 200:
            _step("reviewer_settled", False, "reviewer never settled in phase 2")
        return
    state["polls"] = 0
    state.setdefault("awards_before", _award_count())
    result = _answer_until_awards(state, state["awards_before"] + 1,
                                  what="replacement_answer")
    if result is None:
        return
    if not result:
        return
    _step("replacement_answer", True)
    try:
        ops = _journal_ops()
        kinds = sorted(o["kind"] for o in ops)
        if kinds != ["review_award", "review_award", "review_restore",
                     "review_retract", "review_retract"]:
            _step("op_history", False, f"kinds={kinds}")
            return
        _step("op_history", True)
        proj = _projection()
        total = proj["xp_micro"].get("mining", 0)
        if total <= 0:
            _step("replacement_earns_once", False, f"mining={total}")
            return
        _step("replacement_earns_once", True, f"mining_xp_micro={total}")
    except Exception as exc:
        _step("undo_verify", False, repr(exc))
        return
    _shot("undo-done")
    _finish(0 if not RESULT["errors"] else 1)


# --------------------------------------------------------------------------
# Journey: catchup (synthetic late mobile history + preset routing + dedup)
# --------------------------------------------------------------------------

def _poll_catchup(state):
    if not _poll_chooser(state, "evolved"):
        return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=4, prefix="CU")
        return
    from aqt import mw
    col = mw.col
    stage = state.get("stage", "direct")
    if stage == "direct":
        result = _answer_until_awards(state, 2, what="two_direct_awards")
        if result is None:
            return
        if not result:
            return
        _step("two_direct_awards", True)
        try:
            # Route catch-up through the PRODUCT preset path (timestamped
            # preset op), then synthesize two phone reviews NEWER than the
            # preset so the preset applies to them (per contract, the preset
            # in force when the review happened wins). Synthetic rows are
            # labeled as such; they exercise reconciliation, not device sync.
            import ankiscape
            engine = ankiscape._EVOLVED_CTX.get("engine")
            if engine is None:
                _step("engine_present", False, "no Evolved engine bound")
                return
            from ankiscape.evolved.presets import apply_preset
            applied = apply_preset(engine, engine.journal, col, "fishing")
            if not applied.get("ok"):
                _step("preset_apply", False, applied.get("error", "?"))
                return
            _step("preset_apply", True, "fishing")
            import time as _time
            now_ms = int(_time.time() * 1000)
            cards = col.find_cards('deck:"E2E Deck"')
            answered = {r[1] for r in col.db.all(
                "SELECT id, cid FROM revlog ORDER BY id DESC LIMIT 10")}
            unanswered = [cid for cid in cards if cid not in answered]
            if len(unanswered) < 2:
                _step("catchup_setup", False, f"unanswered={len(unanswered)}")
                return
            for offset, cid in zip((1000, 2000), unanswered[:2]):
                col.db.execute(
                    "INSERT INTO revlog(id, cid, usn, ease, ivl, lastIvl,"
                    " factor, time, type) VALUES(?, ?, -1, 3, 1, 1, 2500,"
                    " 5000, 1)", now_ms + offset, cid)
            state["stage"] = "scan"
            _step("late_history_inserted", True)
        except Exception as exc:
            _step("catchup_setup", False, repr(exc))
        return
    if stage == "scan":
        try:
            import ankiscape
            engine = ankiscape._EVOLVED_CTX.get("engine")
            if engine is None:
                _step("engine_present", False, "no Evolved engine bound")
                return
            from ankiscape.evolved.catchup import run_catchup
            result = run_catchup(col, engine, engine.journal, full=True)
            if result.get("made") != 2:
                _step("catchup_two_awards", False, f"made={result.get('made')}")
                return
            _step("catchup_two_awards", True)
            proj = _projection()
            if proj["xp_micro"].get("fishing", 0) <= 0:
                _step("preset_routing", False,
                      f"fishing={proj['xp_micro'].get('fishing')}")
                return
            _step("preset_routing", True, "catch-up went to Fishing preset")
            again = run_catchup(col, engine, engine.journal, full=True)
            if again.get("made") != 0:
                _step("catchup_dedup", False, f"remade={again.get('made')}")
                return
            _step("catchup_dedup", True)
            state["stage"] = "direct_after"
        except Exception as exc:
            _step("catchup_scan", False, repr(exc))
        return
    if stage == "direct_after":
        # Answering a catch-up-credited card for real reuses the same review
        # identity when its revlog row matches... a genuinely new answer is a
        # new row and earns; assert no double-credit for the same row by
        # re-running the scan instead (already covered) and finish on counts.
        try:
            ops = _journal_ops()
            awards = [o for o in ops if o["kind"] == "review_award"]
            if len(awards) != 4:
                _step("four_total_awards", False, f"awards={len(awards)}")
                return
            _step("four_total_awards", True)
            proj = _projection()
            total = sum(proj["xp_micro"].values())
            if total <= 0:
                _step("catchup_xp_positive", False, f"total={total}")
                return
            _step("catchup_xp_positive", True, f"total_xp_micro={total}")
        except Exception as exc:
            _step("catchup_verify", False, repr(exc))
            return
        _shot("catchup-done")
        _finish(0 if not RESULT["errors"] else 1)


_PHASE_POLLS = {
    ("fresh", 1): _poll_fresh,
    ("upgrade", 1): _poll_upgrade_1,
    ("upgrade", 2): _poll_upgrade_2,
    ("upgrade", 3): _poll_upgrade_3,
    ("upgrade", 4): _poll_upgrade_4,
    ("undo", 1): _poll_undo,
    ("undo", 2): _poll_undo_2,
    ("catchup", 1): _poll_catchup,
}


try:
    from anki.hooks import addHook

    def _on_profile_loaded():
        try:
            import faulthandler
            fh = open(os.path.join(_base_dir(), "e2e-faulthandler.log"),
                      "w", encoding="utf-8")
            # If the main thread ever blocks, this dump names the frame.
            # Short timeout: a healthy phase always finishes or quits first
            # (cancelled in _quit), so any dump here is a real wedge.
            faulthandler.dump_traceback_later(45, file=fh)
        except Exception:
            pass
        from aqt.qt import QTimer
        QTimer.singleShot(0, run)

    addHook("profileLoaded", _on_profile_loaded)
except Exception:
    pass
