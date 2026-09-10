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
    # Separate windows (Evolved shell, dialogs) are not part of mw.grab();
    # capture every visible top-level window with an object name too.
    try:
        from aqt.qt import QApplication
        for widget in QApplication.topLevelWidgets():
            try:
                if not widget.isVisible():
                    continue
                obj = widget.objectName()
                if not obj or obj in ("",) or widget is None:
                    continue
                from aqt import mw as _mw
                if widget is _mw:
                    continue
                path = _out_path(f"{name}-{obj}.png")
                widget.grab().save(path)
                RESULT["screenshots"].append(path)
            except Exception:
                continue
    except Exception:
        pass


def _finish(exit_code):
    RESULT["finished"] = time.time()
    try:
        from aqt.qt import qVersion
        RESULT["qt_version"] = str(qVersion())
    except Exception:
        RESULT["qt_version"] = ""
    try:
        with open(_out_path("assertions.json"), "w", encoding="utf-8") as fh:
            json.dump(RESULT, fh, indent=2)
    except Exception:
        pass
    _quit(exit_code)


def _finish_phase(next_phase):
    """End a non-final phase: request relaunch into next_phase, then quit.

    The request carries phase pass/fail so an intermediate phase's failed
    steps fail the whole journey instead of vanishing on relaunch.
    """
    try:
        with open(os.path.join(_base_dir(), "relaunch.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"next_phase": int(next_phase), "run_id": RUN_ID,
                       "ok": not RESULT["errors"],
                       "failed_steps": [s["name"] for s in RESULT["steps"]
                                        if not s.get("ok")]}, fh)
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
    masking); anything unexpected still wedges loudly into the watchdog.

    SCOPE GUARD: only dialogs the CURRENT journey expects. The dialogs
    journey drives account screens whose object names must never be
    auto-dismissed here — an OK click from this watchdog lands in the
    dialog's error re-exec and wedges the chain (diagnosed 2026-09-09:
    register dialog filled+accepted but on_submit never ran).
    """
    if JOURNEY == "dialogs":
        return
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


def _find_shell():
    """Visible nonmodal Evolved shell window (singleton)."""
    try:
        from aqt.qt import QApplication
        for widget in QApplication.topLevelWidgets():
            try:
                if (widget.objectName() == "ankiscape-evolved-shell"
                        and widget.isVisible()):
                    return widget
            except Exception:
                continue
    except Exception:
        pass
    return None


def _shell_child(shell, name):
    try:
        from aqt.qt import QWidget
        for child in shell.findChildren(QWidget):
            try:
                if child.objectName() == name and child.isVisible():
                    return child
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_shell(name):
    shell = _find_shell()
    if shell is None:
        return False
    child = _shell_child(shell, name)
    if child is None:
        return False
    try:
        child.click()
        return True
    except Exception:
        return False


def _click_rail(section):
    shell = _find_shell()
    if shell is None:
        return False
    try:
        from aqt.qt import QWidget
        for child in shell.findChildren(QWidget):
            try:
                if (child.objectName() == "ankiscape-rail-button"
                        and child.property("section") == section
                        and child.isVisible()):
                    child.click()
                    from aqt.qt import QApplication
                    QApplication.processEvents()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _open_shell_via_menu():
    """Real entry point: AnkiScape menu routing (runtime_menu_opener)."""
    try:
        import ankiscape
        opener = getattr(ankiscape, "runtime_menu_opener", None)
        if callable(opener):
            opener()
            return True
    except Exception as exc:
        _step("shell_open_error", False, repr(exc))
    return False


def _onboarding_complete():
    try:
        from aqt import mw
        data = mw.col.get_config("ankiscape_evolved_onboarding", {}) or {}
        return bool(isinstance(data, dict) and data.get("complete"))
    except Exception:
        return False


def _drive_onboarding(state, want_skill="mining"):
    """Drive the guided setup through the real shell widgets. Returns True
    once setup commits; False while progressing; None on budget exhaustion."""
    if _onboarding_complete():
        state["onboarding_done"] = True
    if state.get("onboarding_done"):
        if state.get("onboarding_verified"):
            return True
        state["onboarding_verified"] = True
        _step("onboarding_complete", True)
        return True
    shell = _find_shell()
    if shell is None:
        state["onboarding_ticks"] = state.get("onboarding_ticks", 0) + 1
        if state["onboarding_ticks"] % 20 == 0:
            _open_shell_via_menu()
        if state["onboarding_ticks"] > 150:
            _step("onboarding_shell", False, "shell never appeared")
            return None
        return False
    if not state.get("onboarding_seen"):
        state["onboarding_seen"] = True
        _step("onboarding_shell", True)
        _shot("onboarding-welcome")
    skill_btn = _shell_child(shell, f"ankiscape-onboarding-skill-{want_skill}")
    if skill_btn is not None:
        skill_btn.click()
        _step("onboarding_skill", True, want_skill)
        _shot("onboarding-resource")
        return False
    primary = _shell_child(shell, "ankiscape-onboarding-primary")
    if primary is None:
        return False
    step_key = f"onboarding_click_{state.get('onboarding_clicks', 0)}"
    if state.get(step_key):
        # Already clicked this round; wait for the UI to advance.
        state["onboarding_wait"] = state.get("onboarding_wait", 0) + 1
        if state["onboarding_wait"] > 40:
            _step("onboarding_advance", False, "step did not advance")
            return None
        return False
    state[step_key] = True
    state["onboarding_clicks"] = state.get("onboarding_clicks", 0) + 1
    text = ""
    try:
        text = str(primary.text())
    except Exception:
        pass
    primary.click()
    _step("onboarding_click", True, text)
    if "Start studying" in text:
        _shot("onboarding-explain")
    return _onboarding_complete()


def _finish_evolved_verification(state, marker="ui"):
    from aqt import mw
    ok = not RESULT["errors"]
    try:
        requested = mw.col.get_config("ankiscape_mode_requested", None)
        _step("mode_evolved", requested == "evolved", f"got {requested!r}")
    except Exception as exc:
        _step("mode_evolved", False, repr(exc))
        ok = False
    _shot(f"{marker}-done")
    _finish(0 if ok else 1)


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
            card_id = getattr(getattr(reviewer, "card", None), "id", None)
            if state.get("ready_card") != card_id:
                if not state.get("page_probe_pending"):
                    state["page_probe_pending"] = True
                    state["page_probe_ticks"] = 0

                    def ready(value):
                        state["page_probe_pending"] = False
                        if value:
                            state["ready_card"] = card_id
                    reviewer.web.evalWithCallback(
                        "typeof _showAnswer === 'function' && !!document.getElementById('qa')", ready)
                else:
                    # A lost evalWithCallback must not wedge the run forever:
                    # re-issue the (idempotent) probe after a bounded wait.
                    state["page_probe_ticks"] = state.get(
                        "page_probe_ticks", 0) + 1
                    if state["page_probe_ticks"] > 50:
                        state["page_probe_pending"] = False
                        state["page_probe_ticks"] = 0
                        _trace(state, "page-probe-retry")
                state["drive_note"] = "waiting-for-review-page"
                return False
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


def _reviewer_empty():
    try:
        from aqt import mw
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None:
            return True
        card = getattr(reviewer, "card", None)
        if card is not None:
            return False
        # Reviewer with no card: either transitioning or on congrats.
        return True
    except Exception:
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
    from aqt import mw
    game = _game_uuid()
    path = os.path.join(mw.pm.profileFolder(), "ankiscape-evolved", game, "game.sqlite3")
    return path if game and os.path.isfile(path) else ""


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
        _step(what, False,
              f"have={have} want={sorted(kinds)} revlog={_revlog_count()}")
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

def _find_upgrade_dialog():
    try:
        from aqt.qt import QApplication
        for widget in QApplication.topLevelWidgets():
            try:
                if (widget.objectName() == "ankiscape-upgrade-dialog"
                        and widget.isVisible()):
                    return widget
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_upgrade(name):
    dlg = _find_upgrade_dialog()
    if dlg is None:
        return False
    try:
        from aqt.qt import QWidget
        for child in dlg.findChildren(QWidget):
            try:
                if child.objectName() == name:
                    child.click()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Journey: fresh (guided setup -> training -> awards)
# --------------------------------------------------------------------------

def _poll_fresh(state):
    if not state.get("onboarding_done"):
        if _drive_onboarding(state, "mining") is not True:
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
        policies = {o["payload"].get("reward_policy") for o in ops
                    if o["kind"] == "review_award"}
        _assert("new_policy_marker", policies == {2}, f"policies={policies}")
    except Exception as exc:
        _assert("journal_read", False, repr(exc))
    try:
        activated = (col.get_config("ankiscape_evolved_player_data", {}) or {}).get(
            "activated_at", 0)
        _assert("activation_stamped", int(activated) > 0, f"activated_at={activated}")
    except Exception as exc:
        _assert("activation_stamped", False, repr(exc))
    try:
        from aqt.qt import QApplication
        huds = [w for w in QApplication.topLevelWidgets()
                if w.objectName() in ("ankiscape-evolved-hud",)]
        _assert("hud_anchored", len(huds) == 0,
                f"floating_huds={len(huds)} (must be anchored in layout)")
    except Exception as exc:
        _assert("hud_anchored", False, repr(exc))
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
# Journey: upgrade (Classic -> prompt -> immediate Try Evolved -> Classic)
# --------------------------------------------------------------------------

def _poll_upgrade_1(state):
    # Fresh-install routing opens onboarding; this collection is actually a
    # Classic 2.0.2 upgrade, so seed the fixture and request Classic.
    if not _find_shell():
        _open_shell_via_menu()
    if not state.get("fixture"):
        try:
            from aqt import mw
            with open(os.path.join(_base_dir(), "classic-fixture.json"),
                      encoding="utf-8") as fh:
                fixture = json.load(fh)
            mw.col.set_config("ankiscape_player_data", fixture["player_data"])
            mw.col.set_config("ankiscape_current_skill",
                              fixture.get("current_skill", "Mining"))
            mw.col.set_config("ankiscape_mode_requested", "classic")
            state["fixture"] = True
            state["classic_before"] = (fixture["player_data"] or {}).get(
                "mining_exp", 0)
            _step("classic_fixture", True)
        except Exception as exc:
            _step("classic_fixture", False, repr(exc))
            return
    if not state.get("seeded"):
        _seed_deck(state, deck="E2E Deck", count=3, prefix="UPG")
        return
    _shot("upgrade-seeded")
    _finish_phase(2)


def _poll_upgrade_2(state):
    # Classic active with real progress; no setup interference.
    from aqt import mw
    if _find_shell() is not None:
        _step("no_shell_in_classic", False, "Evolved shell opened in Classic")
        return
    if _find_upgrade_dialog() is not None:
        _step("no_upgrade_prompt_with_request", False, "prompt on explicit Classic")
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
    result = _answer_until_revlog(state, state["revlog_before"] + 3,
                                  what="classic_three_answers")
    if result is None:
        return
    if not result:
        return
    _step("classic_three_answers", True)
    try:
        after = (mw.col.get_config("ankiscape_player_data", {}) or {}).get(
            "mining_exp", 0)
        if after <= state["classic_before"]:
            _step("classic_awards", False,
                  f"before={state['classic_before']} after={after}")
            return
        _step("classic_awards", True,
              f"mining_exp {state['classic_before']} -> {after}")
        _save_snapshot("classic-snapshot.json", _classic_snapshot())
        # A returning user who has not chosen: clear the request so the
        # upgrade prompt appears with real Classic progress present.
        mw.col.set_config("ankiscape_mode_requested", None)
        _step("request_cleared", True)
    except Exception as exc:
        _step("classic_verify", False, repr(exc))
        return
    _shot("upgrade-classic-done")
    _finish_phase(3)


def _poll_upgrade_3(state):
    from aqt import mw
    stage = state.get("stage", "prompt")
    if stage == "prompt":
        dlg = _find_upgrade_dialog()
        if dlg is None:
            state["stage_ticks"] = state.get("stage_ticks", 0) + 1
            if state["stage_ticks"] > 100:
                _step("upgrade_prompt", False, "prompt never appeared")
            return
        state["stage_ticks"] = 0
        _step("upgrade_prompt", True)
        _shot("upgrade-prompt")
        if not _click_upgrade("ankiscape-upgrade-try"):
            _step("upgrade_try_click", False, "button missing")
            return
        _step("upgrade_try_click", True)
        state["stage"] = "onboarding"
        return
    if stage == "onboarding":
        # Try Evolved commits in the SAME visit: setup opens without restart.
        if not state.get("onboarding_started"):
            state["onboarding_started"] = True
            state["onboarding_ticks"] = 0
        if _drive_onboarding(state, "mining") is not True:
            return
        _step("immediate_evolved_without_restart", True)
        state["stage"] = "reviews"
        return
    if stage == "reviews":
        if not state.get("seeded"):
            _seed_deck(state, deck="E2E Deck", count=5, prefix="UPG")
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
            proj = _projection()
            mining_xp = proj["xp_micro"].get("mining", 0)
            if mining_xp <= 0:
                _step("evolved_fresh_state", False, f"mining_xp_micro={mining_xp}")
                return
            _step("evolved_fresh_state", True, f"mining_xp_micro={mining_xp}")
            if _classic_snapshot() != _load_snapshot("classic-snapshot.json"):
                _step("classic_still_untouched", False, "Evolved reviews touched Classic!")
                return
            _step("classic_still_untouched", True)
        except Exception as exc:
            _step("evolved_verify", False, repr(exc))
            return
        _shot("upgrade-evolved-done")
        _finish_phase(4)


def _poll_upgrade_4(state):
    # Later immediate mode switch (no restart) from Settings > Advanced.
    from aqt import mw
    stage = state.get("stage", "open_shell")
    if stage == "open_shell":
        try:
            mw.moveToState("deckBrowser")
        except Exception:
            pass
        if not _find_shell():
            _open_shell_via_menu()
        if not state.get("shell_step"):
            state["shell_step"] = True
        if _find_shell() is None:
            return
        if not _click_rail("settings"):
            return
        _shot("settings-advanced")
        state["stage"] = "switch"
        return
    if stage == "switch":
        shell = _find_shell()
        if shell is None:
            return
        tabs = _find_child(shell, "ankiscape-settings-tabs")
        if tabs is not None:
            try:
                if tabs.currentIndex() != 3:
                    tabs.setCurrentIndex(3)  # Advanced tab
                    return
            except Exception:
                pass
        if not _click_shell("ankiscape-mode-switch"):
            state["switch_ticks"] = state.get("switch_ticks", 0) + 1
            if state["switch_ticks"] > 80:
                _step("mode_switch_click", False, "button missing")
                _finish(1)
            return
        _step("mode_switch_click", True)
        state["stage"] = "verify_classic"
        return
    if stage == "verify_classic":
        try:
            import ankiscape
            rt = ankiscape._runtime_mod.get_runtime()
            adapter = getattr(rt.active_adapter, "name", "?")
            if adapter != "classic":
                state["verify_ticks"] = state.get("verify_ticks", 0) + 1
                if state["verify_ticks"] > 60:
                    _step("immediate_switch_classic", False,
                          f"adapter={adapter}")
                return
            _step("immediate_switch_classic", True)
        except Exception as exc:
            _step("immediate_switch_classic", False, repr(exc))
            return
        snapshot = _load_snapshot("classic-snapshot.json")
        if _classic_snapshot() != snapshot:
            _step("classic_preserved_on_switch", False, "Classic state drifted")
            return
        _step("classic_preserved_on_switch", True)
        state["stage"] = "classic_review"
        return
    if stage == "classic_review":
        if not state.get("seeded"):
            _seed_deck(state, deck="E2E Deck", count=6, prefix="UPG")
            return
        state.setdefault("revlog_before", _revlog_count())
        result = _answer_until_revlog(state, state["revlog_before"] + 1,
                                      what="classic_after_switch")
        if result is None:
            return
        if not result:
            return
        _step("classic_after_switch", True)
        try:
            after = (mw.col.get_config("ankiscape_player_data", {}) or {}).get(
                "mining_exp", 0)
            before = (state.get("classic_before")
                      or (snapshot_mining_exp(_load_snapshot("classic-snapshot.json"))))
            _step("classic_resumed", after > 0, f"mining_exp={after}")
            _ = before
        except Exception as exc:
            _step("classic_resumed", False, repr(exc))
            return
        _shot("upgrade-classic-back")
        _finish(0 if not RESULT["errors"] else 1)


def snapshot_mining_exp(snapshot):
    try:
        data = json.loads(snapshot.get("ankiscape_player_data", "{}"))
        return data.get("mining_exp", 0)
    except Exception:
        return 0


# --------------------------------------------------------------------------
# Journey: undo (real mw.undo/mw.redo, retract/restore/re-answer)
# --------------------------------------------------------------------------

def _poll_undo(state):
    if not state.get("onboarding_done"):
        if _drive_onboarding(state, "mining") is not True:
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
        # As soon as the retraction lands (undo committed), redo like a human
        # would; then wait for the round trip and prove the projection.
        state["polls"] = state.get("polls", 0) + 1
        kinds = sorted(o["kind"] for o in _journal_ops())
        if "review_retract" in kinds:
            _step("retraction_recorded", True, f"kinds={kinds}")
            _issue_redo(state)
            state["stage"] = "restored_wait"
            state["polls"] = 0
            return
        if state["polls"] > 150:
            _step("retraction_recorded", False, f"kinds={kinds}")
            _finish(1)
        return
    if stage == "restored_wait":
        result = _wait_journal_kinds(
            state, ["review_award", "review_retract", "review_restore"],
            "restore_recorded")
        if result is None:
            _finish(1)
            return
        if not result:
            return
        if state.get("redo_error"):
            _step("restore_recorded", False, state["redo_error"])
            _finish(1)
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
            _finish(1)
            return
        if not result:
            return
        _shot("undo-retracted")
        _finish_phase(2)


def _issue_redo(state):
    from aqt import mw
    try:
        if not callable(getattr(mw, "redo", None)):
            state["redo_error"] = "mw.redo missing"
            return
        mw.redo()  # REAL Anki Redo
    except Exception as exc:
        state["redo_error"] = repr(exc)[:160]


def _poll_undo_2(state):
    # Fresh process: the retracted award persisted; the replacement answer
    # earns once against the reconciled state.
    from aqt import mw
    if _find_upgrade_dialog() is not None:
        _step("no_upgrade_prompt", False, "upgrade prompt in phase 2")
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
    if not state.get("onboarding_done"):
        if _drive_onboarding(state, "mining") is not True:
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


# --------------------------------------------------------------------------
# Journey: dialogs (live Qt exercising of menu + account screens)
# --------------------------------------------------------------------------

def _poll_dialogs(state):
    """Open every Qt surface for real; assert structure, then close.

    Exercises the shipped dialog constructors (menu tabs, register, login,
    recovery, code) through real Qt widgets — never screenshots of mocks.
    Modal dialogs are opened non-blockingly where possible; exec()-based
    account dialogs are constructed widget-by-widget via the same code
    path but verified structurally (open + object names + close) since the
    driver cannot type into a nested modal loop unattended.
    """
    from aqt import mw
    from aqt.qt import QApplication
    stage = state.get("stage", "menu")
    if stage == "menu":
        if not state.get("onboarding_done"):
            if _drive_onboarding(state, "mining") is not True:
                return
        try:
            import ankiscape as _pkg
            opener = getattr(_pkg, "runtime_menu_opener", None)
            if not callable(opener):
                _step("menu_opener_present", False, "missing")
                return
            _step("menu_opener_present", True)
            if not state.get("menu_invoked"):
                state["menu_invoked"] = True
                opener()
            shell = _find_shell()
            if shell is not None:
                if not state.get("menu_shot"):
                    state["menu_shot"] = True
                    _step("menu_opens", True)
                    _shot("menu")
                # Five rail sections + settings cog, all keyboard-accessible.
                from aqt.qt import QWidget as _QWidget
                sections = []
                for child in shell.findChildren(_QWidget):
                    try:
                        if child.objectName() == "ankiscape-rail-button":
                            sections.append(str(child.property("section")))
                    except Exception:
                        continue
                want = {"training", "skills", "bank", "achievements",
                        "hiscores", "settings"}
                if want.issubset(set(sections)):
                    _step_once(state, "rail", "menu_rail_sections", True,
                               ",".join(sorted(sections)))
                else:
                    _step("menu_rail_sections", False, f"sections={sections}")
                    _finish(1)
                    return
                # Hiscores is built lazily: open it, then verify its controls.
                _click_rail("hiscores")
                login_btn = _find_child(shell, "ankiscape-hiscores-login")
                sync_btn = _find_child(shell, "ankiscape-sync-button")
                if login_btn is not None or sync_btn is not None:
                    _step_once(state, "hiscorectl", "menu_hiscores_controls",
                               True,
                               "logged-out" if login_btn is not None else "logged-in")
                    _shot("menu-hiscores")
                else:
                    _step("menu_hiscores_controls", False,
                          "neither login nor sync control visible")
                    _finish(1)
                    return
                shell.hide()
                state["stage"] = "account_dialogs"
                return
            state["menu_ticks"] = state.get("menu_ticks", 0) + 1
            if state["menu_ticks"] % 20 == 0:
                opener()
            if state["menu_ticks"] > 100:
                _step("menu_opens", False, "shell never appeared")
                _finish(1)
            return
        except Exception as exc:
            _step("menu_exercise", False, repr(exc))
        return
    if stage == "account_dialogs":
        # Account dialogs are exec()-modal: construct each through its
        # shipped builder with a stub on_submit, then drive the modal via
        # singleShot automators (fill fields, click OK) so the FULL path
        # including on_submit runs for real.
        #
        # CRITICAL Qt constraint (learned the hard way): NOTHING may block
        # between show_*_dialog() and its exec() — not even dialog
        # construction on another tick. The previous design (singleShot(0)
        # to open, fill from the 100 ms poll tick) deadlocked: the poll's
        # re-entrant fill ran while exec() was starting, and the dialog sat
        # open with no timer able to fire inside the confused modal loop.
        # So each dialog is fully driven from ONE automator chain: open on
        # singleShot, then fill+click on follow-up singleShots (which DO
        # fire inside the nested modal loop), then chain the next dialog
        # from on_submit itself.
        try:
            from aqt.qt import QTimer as _QT
            if not state.get("acct_started"):
                state["acct_started"] = True
                state["acct_index"] = 0
                _QT.singleShot(0, lambda: _drive_account_dialog_hooked(state, 0))
            if state.get("acct_done"):
                _shot("dialogs-done")
                _finish(0 if not RESULT["errors"] else 1)
                return
            if state.get("acct_ticks", 0) > 400:
                _step("account_dialogs", False,
                      f"stalled at index {state.get('acct_index')}")
        except Exception as exc:
            _step("account_dialogs", False, repr(exc))
        return


def _drive_account_dialog_hooked(state, idx):
    """Exercise real OK buttons, including the two-stage recovery form."""
    from aqt import mw
    from aqt.qt import QApplication, QLineEdit, QTimer, QDialogButtonBox
    import ankiscape.evolved.ui.dialogs as dialogs
    if idx >= 3:
        state["acct_done"] = True
        return
    state["acct_index"] = idx
    values = {
        "ankiscape-register-username": "e2e_user",
        "ankiscape-register-email": "e2e@example.com",
        "ankiscape-register-password": "example-password",
        "ankiscape-login-identity": "e2e_user",
        "ankiscape-login-password": "example-password",
        "ankiscape-recovery-email": "e2e@example.com",
        "ankiscape-email-code": "123456",
        "ankiscape-recovery-password": "example-new-password",
    }
    def fill():
        dlg = QApplication.activeModalWidget()
        if dlg is None or not dlg.objectName().startswith("ankiscape-"):
            QTimer.singleShot(100, fill)
            return
        for field in dlg.findChildren(QLineEdit):
            if field.objectName() in values:
                field.setText(values[field.objectName()])
        buttons = dlg.findChild(QDialogButtonBox)
        if dlg.objectName() == "ankiscape-recovery-request":
            QTimer.singleShot(200, fill)
        buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    QTimer.singleShot(250, fill)
    submit = lambda fields: _account_submitted(state, idx, dict(fields))
    if idx == 0:
        dialogs.show_register_dialog(mw, submit)
    elif idx == 1:
        dialogs.show_login_dialog(mw, submit)
    else:
        dialogs.show_recovery_dialog(mw, lambda email: {"ok": True}, submit)


def _account_submitted(state, idx, fields):
    """Runs INSIDE the dialog's exec() as its on_submit. Record, verify,
    advance: the dialog closes itself after we return {"ok": True}."""
    from aqt.qt import QTimer as _QT4
    keys = ["register_fields", "login_fields", "recovery_fields"]
    state[keys[idx]] = fields
    checks = [
        fields.get("username") == "e2e_user"
        and "e2e@example.com" in fields.get("email", ""),
        fields.get("identity") == "e2e_user",
        fields.get("email") == "e2e@example.com"
        and fields.get("code") == "123456",
    ]
    names = ["register_submit", "login_submit", "recovery_submit"]
    _step(names[idx], bool(checks[idx]), "fields validated (credentials omitted)")
    _QT4.singleShot(800, lambda: _drive_account_dialog_hooked(state, idx + 1))
    return {"ok": True}


# --------------------------------------------------------------------------
# Journeys: ui-onboarding / ui-training / ui-settings / ui-review /
# ui-lifecycle (the 3.0 shell journeys)
# --------------------------------------------------------------------------

def _find_child(widget, object_name, *, cls_name=""):
    try:
        from aqt.qt import QWidget
        for child in widget.findChildren(QWidget):
            try:
                if child.objectName() != object_name:
                    continue
                if cls_name and type(child).__name__ != cls_name:
                    continue
                if child.isVisible():
                    return child
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_slot(display):
    from aqt.qt import QApplication
    QApplication.processEvents()
    shell = _find_shell()
    if shell is None:
        return False
    try:
        from aqt.qt import QWidget
        for child in shell.findChildren(QWidget):
            try:
                if child.objectName() != "ankiscape-item-slot" or not child.isVisible():
                    continue
                tip = str(child.toolTip() or child.accessibleName() or "")
                if tip.startswith(display):
                    callback = getattr(child, "clicked", None)
                    if callable(callback):
                        callback(display)
                    else:
                        child.setFocus()
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _set_combo(shell, object_name, text):
    combo = _find_child(shell, object_name)
    if combo is None:
        return False
    try:
        combo.setCurrentText(str(text))
        return True
    except Exception:
        return False


def _config(key, default=None):
    try:
        from aqt import mw
        return mw.col.get_config(key, default)
    except Exception:
        return default


def _poll_ui_onboarding(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        result = _drive_onboarding(state, "woodcutting")
        if result is not True:
            return
        from .regressions import run as interaction_regressions
        try:
            interaction_regressions(_step, _shot)
        except Exception as exc:
            _step("interaction_regression_exception", False, repr(exc))
            raise
        _step("ui_onboarding_complete", True)
        state["stage"] = "verify"
        return
    if stage == "verify":
        skill = _config("ankiscape_evolved_current_skill", "")
        resource = _config("ankiscape_evolved_current_woodcutting", "")
        pointer = _config("ankiscape_evolved_player_data", {}) or {}
        draft = _config("ankiscape_evolved_onboarding", {}) or {}
        ok = (skill == "woodcutting" and resource == "Tree"
              and int(pointer.get("activated_at", 0) or 0) > 0
              and bool(draft.get("complete")))
        _step("ui_onboarding_persisted", ok,
              f"skill={skill} resource={resource} activated="
              f"{pointer.get('activated_at')} complete={draft.get('complete')}")
        if not ok:
            return
        # Training is active: real reviews credit immediately.
        _seed_deck(state, count=2, prefix="UIONB")
        state["stage"] = "review"
        return
    if stage == "review":
        result = _answer_until_awards(state, 1, what="ui_onboarding_award")
        if result is None:
            return
        if not result:
            return
        _step("ui_onboarding_award", True)
        _shot("ui-onboarding-done")
        _finish(0 if not RESULT["errors"] else 1)


def _poll_ui_training(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _open_shell_via_menu()
        state["stage"] = "skills"
        return
    if stage == "skills":
        if not _find_shell():
            return
        if not _click_rail("skills"):
            return
        _shot("ui-training-skills")
        before = _config("ankiscape_evolved_current_mining", "")
        # Preview a different unlocked tier without committing.
        if not _click_slot("Clay"):
            state["skill_ticks"] = state.get("skill_ticks", 0) + 1
            if state["skill_ticks"] > 60:
                _step("ui_training_preview", False, "Clay slot missing")
            return
        after = _config("ankiscape_evolved_current_mining", "")
        _step("ui_training_preview_no_commit", before == after,
              f"{before!r} -> {after!r}")
        # Locked tier cannot be trained.
        _click_slot("Runite ore")
        train = _find_child(_find_shell(), "ankiscape-train-button")
        locked_ok = train is not None and not train.isEnabled()
        _step("ui_training_locked_disabled", locked_ok)
        if not locked_ok:
            return
        # Train a different unlocked resource: commit + return home.
        _click_slot("Clay")
        train = _find_child(_find_shell(), "ankiscape-train-button")
        if train is None:
            return
        train.click()
        state["stage"] = "verify_train"
        return
    if stage == "verify_train":
        if _config("ankiscape_evolved_current_mining", "") != "Clay":
            state["train_ticks"] = state.get("train_ticks", 0) + 1
            if state["train_ticks"] > 60:
                _step("ui_training_commit", False, "selection not persisted")
            return
        _shot("ui-training-training")
        _step("ui_training_commit", True)
        _shot("ui-training-home")
        # Bank + achievements screens render with real data paths.
        _click_rail("bank")
        _shot("ui-training-bank")
        _click_rail("achievements")
        _shot("ui-training-achievements")
        state["stage"] = "navigation_focus"
        state["navigation_index"] = 0
        _find_shell().activateWindow()
        return
    if stage == "navigation_focus":
        sections = ("skills", "bank", "achievements", "hiscores", "guide", "settings", "training")
        shell = _find_shell()
        index = state["navigation_index"]
        # Re-assert activation each tick: macOS can refuse a programmatic
        # activateWindow() while another app is frontmost, and screenshots
        # taken just before can steal it back. Judge only after a bounded
        # retry, and accept either an active window or focused shell.
        try:
            from aqt.qt import QApplication, Qt
            QApplication.setActiveWindow(shell)
            shell.raise_()
            shell.activateWindow()
            shell.setFocus(Qt.FocusReason.OtherFocusReason)
            QApplication.processEvents()
        except Exception:
            pass
        if index:
            focused = shell.isActiveWindow() or shell.hasFocus()
            if not focused and state.get("focus_attempts", 0) < 15:
                state["focus_attempts"] = state.get("focus_attempts", 0) + 1
                return
            state["focus_attempts"] = 0
            _step("navigation_focus_" + sections[index-1], focused)
        if index == len(sections):
            _finish(0 if not RESULT["errors"] else 1)
            return
        shell.set_section(sections[index])
        if sections[index] == "guide":
            _step("guide_accessible_from_rail", "guide" in shell._screens)
            _shot("ui-skill-guide")
        state["navigation_index"] += 1


def _poll_ui_settings(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _open_shell_via_menu()
        state["stage"] = "open"
        return
    if stage == "open":
        if not _find_shell():
            return
        if not _click_rail("settings"):
            _click_shell("ankiscape-rail-button")
            return
        state["stage"] = "change"
        return
    if stage == "change":
        shell = _find_shell()
        if shell is None:
            return
        _set_combo(shell, "ankiscape-setting-ui-scale", "125")
        position = _find_child(shell, "ankiscape-setting-hud-position")
        if position is not None:
            try:
                position.setCurrentText("top")
            except Exception:
                pass
        reduced = _find_child(shell, "ankiscape-setting-reduced-motion")
        if reduced is not None:
            try:
                reduced.setChecked(True)
            except Exception:
                pass
        _shot("ui-settings")
        state["stage"] = "verify"
        return
    if stage == "verify":
        scale = _config("ankiscape_evolved_ui_scale", 100)
        pos = _config("ankiscape_evolved_hud_position", "bottom")
        reduced = _config("ankiscape_evolved_reduced_motion", False)
        ok = (int(scale) == 125 and pos == "top" and bool(reduced))
        _step("ui_settings_persisted", ok,
              f"scale={scale} position={pos} reduced={reduced}")
        # Account page renders logged-out benefits without crashing.
        tabs = _find_child(_find_shell(), "ankiscape-settings-tabs")
        if tabs is not None:
            try:
                tabs.setCurrentIndex(3)  # Advanced
                _shot("ui-settings-advanced")
                tabs.setCurrentIndex(1)
                _shot("ui-settings-account")
            except Exception:
                pass
        _finish(0 if not RESULT["errors"] else 1)


def _poll_ui_review(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _seed_deck(state, count=4, prefix="UIREV")
        state["stage"] = "paused"
        return
    if stage == "paused":
        # Production with no materials: persisted, zero reward, HUD pause.
        from aqt import mw
        if mw.col.get_config("ankiscape_evolved_current_smithing", "") != "Bronze bar":
            mw.col.set_config("ankiscape_evolved_current_smithing", "Bronze bar")
        if mw.col.get_config("ankiscape_evolved_current_skill", "") != "smithing":
            mw.col.set_config("ankiscape_evolved_current_skill", "smithing")
        result = _answer_until_awards(state, 1, what="ui_review_paused_op")
        if result is None:
            return
        if not result:
            return
        ops = _journal_ops()
        awards = [o for o in ops if o["kind"] == "review_award"]
        if not awards:
            _step("ui_review_paused_persisted", False, "no award op")
            return
        _step("ui_review_paused_persisted", True)
        proj = _projection()
        smithing = proj["xp_micro"].get("smithing", 0)
        _step("ui_review_paused_zero", smithing == 0, f"smithing={smithing}")
        hud = getattr(mw, "ankiscape_evolved_hud", None)
        anchored = False
        try:
            anchored = (hud is not None and hud.parent() is not None
                        and not hud.isWindow() and hud.isVisible())
        except Exception:
            anchored = False
        _step("ui_review_hud_anchored", anchored)
        _shot("ui-review-paused")
        state["stage"] = "reward"
        return
    if stage == "reward":
        from aqt import mw
        mw.col.set_config("ankiscape_evolved_current_skill", "mining")
        mw.col.set_config("ankiscape_evolved_current_mining", "Rune essence")
        state.setdefault("awards_before", _award_count())
        result = _answer_until_awards(state, state["awards_before"] + 2,
                                      what="ui_review_rewards")
        if result is None:
            return
        if not result:
            return
        _step("ui_review_rewards", True)
        proj = _projection()
        _step("ui_review_mining_positive",
              proj["xp_micro"].get("mining", 0) > 0)
        _shot("ui-review-reward")
        state["stage"] = "finish_deck"
        return
    if stage == "finish_deck":
        from aqt import mw
        # Answer the remaining cards; the completion hook stamps the recap.
        if getattr(mw, "ankiscape_last_recap", None):
            recap = mw.ankiscape_last_recap or {}
            _step("ui_review_recap", bool(recap.get("text")),
                  str(recap.get("text", ""))[:160])
            _shot("ui-review-recap")
            _finish(0 if not RESULT["errors"] else 1)
            return
        state["finish_ticks"] = state.get("finish_ticks", 0) + 1
        reviewer = getattr(mw, "reviewer", None)
        rst = str(getattr(reviewer, "state", ""))
        if state.get("awaiting") and rst != "answer":
            state["awaiting"] = 0  # the last call landed; free the slot
        if _reviewer_settled() and not state.get("awaiting"):
            card = getattr(reviewer, "card", None) if reviewer is not None else None
            if card is None:
                if state["finish_ticks"] > 200:
                    _step("ui_review_recap", False, "completion never triggered")
                    _shot("ui-review-no-recap")
                    _finish(1)
                return
            # Easy graduates learning cards immediately, so the deck can
            # actually complete inside one journey.
            _drive_answer(state, 4)
            return
        if state["finish_ticks"] > 300:
            _step("ui_review_recap", False, "completion never triggered")
            _finish(1)


def _poll_ui_lifecycle(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        state["stage"] = "singleton"
        return
    if stage == "singleton":
        _open_shell_via_menu()
        _open_shell_via_menu()
        try:
            from aqt.qt import QApplication
            shells = [w for w in QApplication.topLevelWidgets()
                      if w.objectName() == "ankiscape-evolved-shell"]
            _step("ui_lifecycle_singleton", len(shells) == 1,
                  f"shells={len(shells)}")
        except Exception as exc:
            _step("ui_lifecycle_singleton", False, repr(exc))
        state["stage"] = "reject_in_review"
        return
    if stage == "reject_in_review":
        from aqt import mw
        if not state.get("seeded"):
            _seed_deck(state, count=2, prefix="UILC")
            return
        try:
            mw.moveToState("review")
        except Exception:
            pass
        state["stage"] = "wait_review"
        return
    if stage == "wait_review":
        from aqt import mw
        if getattr(mw, "state", "") != "review":
            state["review_wait"] = state.get("review_wait", 0) + 1
            if state["review_wait"] > 100:
                _step("ui_lifecycle_review_rejected", False,
                      "reviewer never opened")
                _finish(1)
            return
        try:
            import ankiscape
            result = ankiscape._switch_mode_now("classic")
            rt = ankiscape._runtime_mod.get_runtime()
            ok = (isinstance(result, dict) and not result.get("ok")
                  and getattr(rt.active_adapter, "name", "") == "evolved")
            _step("ui_lifecycle_review_rejected", ok, str(result)[:120])
        except Exception as exc:
            _step("ui_lifecycle_review_rejected", False, repr(exc))
        state["stage"] = "switch_classic"
        return
    if stage == "switch_classic":
        from aqt import mw
        try:
            mw.moveToState("deckBrowser")
        except Exception:
            pass
        try:
            import ankiscape
            result = ankiscape._switch_mode_now("classic")
            rt = ankiscape._runtime_mod.get_runtime()
            ok = (isinstance(result, dict) and result.get("ok")
                  and getattr(rt.active_adapter, "name", "") == "classic")
            _step("ui_lifecycle_switch_classic", ok, str(result)[:120])
            _step("ui_lifecycle_shell_released",
                  _find_shell() is None)
        except Exception as exc:
            _step("ui_lifecycle_switch_classic", False, repr(exc))
        state["stage"] = "switch_back"
        return
    if stage == "switch_back":
        try:
            import ankiscape
            result = ankiscape._switch_mode_now("evolved")
            rt = ankiscape._runtime_mod.get_runtime()
            ok = (isinstance(result, dict) and result.get("ok")
                  and getattr(rt.active_adapter, "name", "") == "evolved")
            _step("ui_lifecycle_switch_back", ok, str(result)[:120])
            _step("ui_lifecycle_training_preserved",
                  _config("ankiscape_evolved_current_mining", "") != "")
        except Exception as exc:
            _step("ui_lifecycle_switch_back", False, repr(exc))
        _shot("ui-lifecycle-done")
        _finish(0 if not RESULT["errors"] else 1)


# --------------------------------------------------------------------------
# Journey: ui-art (every bundled asset decoded in real Qt; every tab visited)
# --------------------------------------------------------------------------

ART_SECTIONS = ("training", "skills", "bank", "achievements", "hiscores",
                "guide", "settings")
ART_SCREEN_NAMES = {
    "training": "ankiscape-screen-training",
    "skills": "ankiscape-screen-skills",
    "bank": "ankiscape-screen-bank",
    "achievements": "ankiscape-screen-achievements",
    "hiscores": "ankiscape-screen-hiscores",
    "settings": "ankiscape-screen-settings",
    "guide": "ankiscape-guide",
}


def _art_manifest():
    import json
    import ankiscape
    path = os.path.join(os.path.dirname(ankiscape.__file__), "assets",
                        "manifest.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _addon_tree_snapshot():
    """Hash of the installed add-on tree (excluding bytecode caches)."""
    import hashlib
    import ankiscape
    root = os.path.dirname(ankiscape.__file__)
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for name in files:
            if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
    return out


def _install_addon_network_guard(state):
    """Deny and record any HTTP(S) connection attempted from ankiscape code.

    The image path must be fully offline; anything from the add-on itself is
    recorded, refused, and fails the journey. Connections from Anki or the
    driver are left alone so the check cannot lie about unrelated traffic.
    """
    import socket
    import sys as _sys
    attempts = state.setdefault("net_attempts", [])

    def _from_addon():
        frame = _sys._getframe()
        while frame is not None:
            name = str(frame.f_globals.get("__name__", ""))
            if name == "ankiscape" or name.startswith("ankiscape."):
                return True
            frame = frame.f_back
        return False

    def _wrap(real):
        def wrapper(self, address, *args, **kwargs):
            if isinstance(address, tuple) and len(address) > 1 \
                    and address[1] in (80, 443) and _from_addon():
                attempts.append(str(address))
                raise ConnectionRefusedError(
                    "AnkiScape offline test: image network denied")
            return real(self, address, *args, **kwargs)
        return wrapper

    try:
        socket.socket.connect = _wrap(socket.socket.connect)
        socket.socket.connect_ex = _wrap(socket.socket.connect_ex)
        _step("art_network_guard", True)
    except Exception as exc:
        _step("art_network_guard", False, repr(exc))


def _pixmap_has_visible_alpha(pix):
    if pix.isNull():
        return False
    image = pix.toImage()
    width, height = image.width(), image.height()
    if width < 1 or height < 1:
        return False
    step_x = max(1, width // 48)
    step_y = max(1, height // 48)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            if image.pixelColor(x, y).alpha() > 8:
                return True
    return False


def _art_contact_sheet(entries, out_path):
    from aqt.qt import QImage, QPainter, QPixmap, Qt
    scale, cell, cols = 3, 200, 6
    rows = max(1, (len(entries) + cols - 1) // cols)
    sheet = QImage(cols * cell, rows * cell, QImage.Format.Format_ARGB32)
    sheet.fill(Qt.GlobalColor.black)
    painter = QPainter(sheet)
    try:
        painter.setPen(Qt.GlobalColor.gray)
        for i, (abs_path, label) in enumerate(entries):
            pix = QPixmap(abs_path)
            if pix.isNull():
                continue
            scaled = pix.scaled(pix.width() * scale, pix.height() * scale,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.FastTransformation)
            x = (i % cols) * cell + (cell - scaled.width()) // 2
            y = (i // cols) * cell + 6
            painter.drawPixmap(x, y, scaled)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText((i % cols) * cell + 4,
                             (i // cols) * cell + cell - 20,
                             str(label)[-26:])
            painter.setPen(Qt.GlobalColor.gray)
    finally:
        painter.end()
    return sheet.save(out_path)


def _run_art_checks(state):
    from aqt.qt import QPixmap
    import ankiscape
    from ankiscape.evolved import assets as assets_mod
    from ankiscape.evolved.ui.widgets import icon_pixmap

    base = os.path.dirname(ankiscape.__file__)
    manifest = _art_manifest()
    records = [r for r in manifest.get("files", []) if isinstance(r, dict)]
    by_path = {str(r.get("path")): r for r in records}
    fallback_sha = str(by_path.get("icon/fallback_missing.png", {}).get(
        "sha256") or "")

    problems = []
    contact = []
    for record in records:
        if record.get("kind") in ("font", "sound"):
            continue
        rel = str(record.get("path") or "")
        full = os.path.join(base, rel)
        pix = QPixmap(full)
        if not _pixmap_has_visible_alpha(pix):
            problems.append(f"undecodable/blank: {rel}")
            continue
        contact.append((full, rel))
        if record.get("kind") in ("skill", "nav", "ore", "log", "bar", "gem",
                                  "craft", "fish_raw", "fish_cooked"):
            if fallback_sha and record.get("sha256") == fallback_sha:
                problems.append(f"known art is the fallback: {rel}")
            if record.get("bytes") and os.path.getsize(full) != record["bytes"]:
                problems.append(f"byte drift: {rel}")
    _step("art_manifest_decodes", not problems, "; ".join(problems[:4]))

    mapping_problems = []

    def check_asset(key, rel, *, require_verified):
        record = by_path.get(rel)
        allowed = ("verified",) if require_verified else ("verified", "original")
        if record is None or record.get("status") not in allowed:
            mapping_problems.append(f"{key}->{rel} not a supported record")
            return
        if fallback_sha and record.get("sha256") == fallback_sha:
            mapping_problems.append(f"{key}->{rel} is the fallback")
            return
        pix = QPixmap(os.path.join(base, rel))
        if pix.isNull():
            mapping_problems.append(f"{key}->{rel} will not decode")
            return
        for size in (24, 28, 48):
            scaled = icon_pixmap(os.path.join(base, rel), size)
            if scaled is None or scaled.isNull():
                mapping_problems.append(f"{rel} null at {size}")
                continue
            if scaled.width() > size or scaled.height() > size:
                mapping_problems.append(f"{rel} exceeds {size}px slot")
            if pix.width() and pix.height():
                want = pix.height() / pix.width()
                got = scaled.height() / scaled.width()
                if abs(got - want) > 0.08:
                    mapping_problems.append(f"{rel} aspect drift at {size}")

    for display, name in assets_mod.ITEM_FILES.items():
        rel = f"{assets_mod._folder_for(name)}/{name}"
        check_asset(display, rel, require_verified=True)
    for skill, rel in assets_mod.SKILL_ICONS.items():
        check_asset(skill, rel, require_verified=True)
    for section, rel in assets_mod.NAV_ICONS.items():
        check_asset(section, rel, require_verified=False)
    _step("art_semantic_mapping", not mapping_problems,
          "; ".join(mapping_problems[:4]))

    fish = ("shrimp", "sardine", "trout", "tuna", "lobster", "swordfish",
            "monkfish", "shark", "anglerfish")
    same = [name for name in fish
            if QPixmap(os.path.join(base, f"fish/{name}.png")).toImage()
            == QPixmap(os.path.join(base, f"fish/cooked_{name}.png")).toImage()]
    _step("art_fish_variants_distinct", not same, ",".join(same))
    gems = (("sapphire", "Sapphire"), ("emerald", "Emerald"),
            ("ruby", "Ruby"), ("diamond", "Diamond"))
    same = [gem for gem, cut in gems
            if QPixmap(os.path.join(base, f"gems/{gem}.png")).toImage()
            == QPixmap(os.path.join(base,
                                    f"crafteditems/{cut}.png")).toImage()]
    _step("art_gem_variants_distinct", not same, ",".join(same))

    try:
        from ankiscape.evolved.data import load_rules
        from ankiscape.evolved.ui.guide import guide_pages
        pages = guide_pages(load_rules())
        short = [title for title, body in pages.items() if len(body) < 100]
        credits = pages.get("Credits & assets", "")
        ok = (not short and "Jagex Ltd" in credits
              and "oldschool.runescape.wiki" in credits)
        _step("art_guide_pages_render", ok,
              f"pages={len(pages)} short={short}")
    except Exception as exc:
        _step("art_guide_pages_render", False, repr(exc))

    try:
        sheet_path = _out_path("art-contact-sheet.png")
        ok = _art_contact_sheet(contact, sheet_path)
        if ok:
            RESULT["screenshots"].append(sheet_path)
        _step("art_contact_sheet", bool(ok), f"{len(contact)} assets")
    except Exception as exc:
        _step("art_contact_sheet", False, repr(exc))


def _poll_ui_art(state):
    stage = state.get("stage", "setup")
    if stage == "setup":
        if not state.get("guard_installed"):
            state["guard_installed"] = True
            _install_addon_network_guard(state)
            state["install_snapshot"] = _addon_tree_snapshot()
        if _drive_onboarding(state, "mining") is not True:
            return
        state["stage"] = "fixture"
        return
    if stage == "fixture":
        try:
            import ankiscape
            engine = ankiscape._EVOLVED_CTX.get("engine")
            if engine is None:
                return
            if not state.get("art_fixture"):
                with open(os.path.join(_base_dir(), "art-fixture.json"),
                          encoding="utf-8") as fh:
                    data = json.load(fh)
                game = engine.cfg.game_uuid
                ops = [dict(op, game_uuid=game)
                       for op in data.get("operations", [])]
                counts = engine.journal.import_game(
                    game, {"operations": ops, "observations": []})
                engine.invalidate_projection()
                state["art_fixture"] = True
                _step("art_fixture_imported", True,
                      f"{counts.get('operations', len(ops))} ops")
            if not _find_shell():
                _open_shell_via_menu()
                return
            state["stage"] = "tabs"
            state["tab_index"] = 0
        except Exception as exc:
            _step("art_fixture", False, repr(exc))
            state["stage"] = "tabs"
            state["tab_index"] = 0
        return
    if stage == "tabs":
        index = int(state.get("tab_index", 0))
        if index >= len(ART_SECTIONS):
            state["stage"] = "checks"
            return
        section = ART_SECTIONS[index]
        shell = _find_shell()
        if shell is None:
            state["shell_ticks"] = state.get("shell_ticks", 0) + 1
            if state["shell_ticks"] > 80:
                _step("art_shell", False, "shell never appeared")
                state["stage"] = "checks"
            return
        state["shell_ticks"] = 0
        if not _click_rail(section):
            state["tab_ticks"] = state.get("tab_ticks", 0) + 1
            if state["tab_ticks"] > 80:
                _step(f"art_tab_{section}", False, "rail button missing")
                state["tab_index"] = index + 1
                state["tab_ticks"] = 0
            return
        from aqt.qt import QApplication
        QApplication.processEvents()
        screen = _shell_child(shell, ART_SCREEN_NAMES[section])
        if screen is None:
            state["tab_ticks"] = state.get("tab_ticks", 0) + 1
            if state["tab_ticks"] > 60:
                _step(f"art_tab_{section}", False, "screen widget missing")
                state["tab_index"] = index + 1
                state["tab_ticks"] = 0
            return
        state["tab_ticks"] = 0
        _shot(f"ui-art-{section}")
        _step(f"art_tab_{section}", True,
              f"{screen.objectName()} {screen.width()}x{screen.height()}")
        state["tab_index"] = index + 1
        return
    if stage == "checks":
        if not state.get("art_checks_done"):
            _run_art_checks(state)
            _verify_art_environment(state)
            state["art_checks_done"] = True
        _shot("ui-art-done")
        _finish_phase(2)


def _verify_art_environment(state):
    """Offline + immutability assertions for the installed add-on."""
    attempts = state.get("net_attempts") or []
    _step("art_no_image_network", not attempts,
          f"attempts={attempts[:3]}")
    before = state.get("install_snapshot") or {}
    after = _addon_tree_snapshot()
    if before:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after)
                         if before[k] != after[k])
        ok = not added and not removed and not changed
        _step("art_install_unchanged", ok,
              f"added={added[:3]} removed={removed[:3]} "
              f"changed={changed[:3]}")
    else:
        _step("art_install_unchanged", False, "no pre-resolution snapshot")


def _poll_ui_art_2(state):
    """Restart phase: graceful account state + unchanged offline art."""
    stage = state.get("stage", "open")
    if stage == "open":
        if not state.get("guard_installed"):
            state["guard_installed"] = True
            _install_addon_network_guard(state)
            state["install_snapshot"] = _addon_tree_snapshot()
        if not _find_shell():
            state["shell_ticks"] = state.get("shell_ticks", 0) + 1
            if state["shell_ticks"] % 20 == 0:
                _open_shell_via_menu()
            if state["shell_ticks"] > 120:
                _step("art_restart_shell", False, "shell never appeared")
                _finish(1)
            return
        state["shell_ticks"] = 0
        try:
            import ankiscape
            info = ankiscape._evolved_account_info()
            ok = not bool(info.get("logged_in"))
            _step("art_restart_graceful_logged_out", ok, str(info)[:120])
        except Exception as exc:
            _step("art_restart_graceful_logged_out", False, repr(exc))
        state["stage"] = "tabs"
        state["tab_index"] = 0
        return
    if stage == "tabs":
        index = int(state.get("tab_index", 0))
        if index >= len(ART_SECTIONS):
            state["stage"] = "checks"
            return
        section = ART_SECTIONS[index]
        shell = _find_shell()
        if shell is None:
            return
        if not _click_rail(section):
            state["tab_index"] = index + 1
            return
        from aqt.qt import QApplication
        QApplication.processEvents()
        screen = _shell_child(shell, ART_SCREEN_NAMES[section])
        _shot(f"ui-art-restart-{section}")
        _step(f"art_restart_tab_{section}", screen is not None)
        state["tab_index"] = index + 1
        return
    if stage == "checks":
        if not state.get("art_checks_done"):
            _run_art_checks(state)
            _verify_art_environment(state)
            state["art_checks_done"] = True
        _shot("ui-art-restart-done")
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
    ("dialogs", 1): _poll_dialogs,
    ("ui-onboarding", 1): _poll_ui_onboarding,
    ("ui-training", 1): _poll_ui_training,
    ("ui-settings", 1): _poll_ui_settings,
    ("ui-review", 1): _poll_ui_review,
    ("ui-lifecycle", 1): _poll_ui_lifecycle,
    ("ui-art", 1): _poll_ui_art,
    ("ui-art", 2): _poll_ui_art_2,
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
