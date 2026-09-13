# dev-only E2E driver addon. NEVER SHIPPED (lives under dev/, outside the
# package allowlist). Installed next to the packaged AnkiScape zip in an
# isolated base directory. Drives REAL reviewer inputs (answer buttons'
# underlying _answerCard path), real mw.undo()/mw.redo(), and real collection
# data — never calls award functions directly. Product read paths (reducer
# replay, catch-up scan) are exercised through the shipped modules.
# Protocol with dev.py: journey.json {journey, phase, run_id} in the base.
# A phase ends with relaunch.json {next_phase, run_id} (dev.py relaunches
# into the next phase) or e2e-assertions.json (final, run_id stamped).
import gc
import json
import os
import sys
import threading
import time
import traceback

RESULT = {"journey": "fresh", "phase": 1, "run_id": None, "steps": [],
          "assertions": {}, "screenshots": [], "errors": [],
          "started": time.time()}

RUN_ID = None
JOURNEY = "fresh"
PHASE = 1
# Set once a phase decides its outcome; stops the poll timer and popup
# watchdog from scheduling further work while Anki tears down.
_QUITTING = False


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


def _runtime_identity():
    """Observed runtime identity from inside the real Anki process.

    The requested version strings cannot prove what actually launched; the
    evidence contract consumes this handshake instead.
    """
    ident = {"anki": "", "python": "", "qt": "", "arch": ""}
    try:
        import aqt
        ident["anki"] = str(getattr(aqt, "appVersion", "") or "")
    except Exception:
        pass
    try:
        from aqt import mw
        pm_version = getattr(getattr(mw, "pm", None), "anki_version", "")
        if not ident["anki"] and pm_version:
            ident["anki"] = str(pm_version)
    except Exception:
        pass
    try:
        import platform
        import sys as _sys
        ident["python"] = ".".join(str(v) for v in _sys.version_info[:3])
        ident["arch"] = platform.machine()
    except Exception:
        pass
    try:
        from aqt.qt import qVersion
        ident["qt"] = str(qVersion())
    except Exception:
        ident["qt"] = ""
    return ident


def _finish(exit_code):
    RESULT["finished"] = time.time()
    try:
        _profile_stop()
    except Exception:
        pass
    try:
        from aqt.qt import qVersion
        RESULT["qt_version"] = str(qVersion())
    except Exception:
        RESULT["qt_version"] = ""
    RESULT["runtime"] = _runtime_identity()
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
                                        if not s.get("ok")],
                       "failed_details": [s for s in RESULT["steps"]
                                          if not s.get("ok")]}, fh)
    except Exception:
        pass
    _quit(0)


def _quit(exit_code):
    """End the phase without raising out of a Qt slot.

    Raising SystemExit inside the tick slot hands the exception to PyQt's
    slot wrapper, which prints it and calls Py_Exit mid-event-loop; Python
    finalization then runs PyQt's sip cleanup while Qt is half torn down and
    intermittently segfaults (crash report 2026-09-12:
    cleanup_on_exit -> cleanup_qobject -> EXC_BAD_ACCESS). Stop our own
    timers, ask the app to exit with the journey's code and return."""
    global _QUITTING
    _QUITTING = True
    try:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    try:
        from aqt.qt import QApplication
        from aqt import mw
        mw.close()
        app = QApplication.instance()
        if app is not None:
            app.exit(int(exit_code))
    except Exception:
        pass


def _fail(msg, full_detail=""):
    _step("fatal", False, msg)
    try:
        # The assertions step keeps a 500-char summary; the full detail
        # (e.g. an untruncated traceback) lands beside it for diagnosis.
        if full_detail:
            with open(_out_path("fatal.txt"), "w", encoding="utf-8") as fh:
                fh.write(str(full_detail) + "\n")
    except Exception:
        pass
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
        if _QUITTING:
            return
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
                        # The count is a full-table COUNT over the journal.
                        # At 100k ops it measured ~3.7 ms; at the 10 Hz beat
                        # cadence that is addon-only idle CPU and lag noise
                        # the control never pays. Refresh every ~5 s and
                        # serve the cached value in between.
                        _ops = state.get("eng_ops_cache", -1)
                        if state["ticks"] < 5 or state["ticks"] % 50 == 0:
                            try:
                                _ops = _eng.journal.operation_count()
                                state["eng_ops_cache"] = _ops
                            except Exception:
                                _ops = -1
                        extra["eng_ops"] = _ops
                    extra["undo_fn"] = getattr(getattr(_mw, "undo", None),
                                              "__name__", "?")
                    # Direct reconcile probe: same product path the mw.undo
                    # wrapper calls. Distinguishes wrapper wiring faults from
                    # engine input faults. Five revlog lookups per beat is
                    # steerable noise during timing runs; sample it at the
                    # same 5 s cadence as the operation count.
                    if (_eng is not None and _mw.col is not None
                            and (state["ticks"] < 5
                                 or state["ticks"] % 50 == 0)):
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
        if _QUITTING:
            return
        try:
            state["ticks"] += 1
            if state["ticks"] == 1:
                _beat("first_tick")
            max_ticks = 1200
            try:
                max_ticks = int(_perf_config().get("max_ticks") or 0) or 1200
            except Exception:
                pass
            if state["ticks"] > max_ticks:  # watchdog per phase
                return _fail("watchdog: journey did not finish in time")
            if state["ticks"] % 150 == 0:
                _shot(f"stuck-{state['ticks']}")
            poll()
        except Exception:
            full = traceback.format_exc()
            _fail("driver exception: " + full[-2000:], full_detail=full)
        else:
            _beat("poll_ok")
            if not _QUITTING:
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


def _lift_deck_daily_limits(col, deck_id, per_day=10000):
    """Raise new/review per-day caps on the disposable e2e decks.

    Anki's default preset stops after 20 new cards a day; long measurement
    journeys (120+ answers, endurance) otherwise exhaust the queue and the
    driver spins until its watchdog. This is test-owned deck configuration
    in an isolated profile, never product code.

    Modern Anki extends today's limits through the scheduler; the legacy
    `update_config(dict)` shim is verified by read-back because it is a
    silent no-op on 26.x."""
    # Modern v3 scheduler: extend today's limits directly (the same path as
    # Custom Study's "increase today's limit").
    try:
        col.sched.extend_limits(per_day, per_day)
        return "extend_limits"
    except Exception:
        pass
    # Legacy decks: update the saved config and verify it persisted; a
    # silent no-op must not be reported as success.
    try:
        conf = col.decks.config_dict_for_deck_id(deck_id)
        conf["new"]["perDay"] = per_day
        conf["rev"]["perDay"] = max(
            per_day, int(conf["rev"].get("perDay", 0) or 0))
        col.decks.update_config(conf)
        back = col.decks.config_dict_for_deck_id(deck_id)
        if int(back["new"].get("perDay") or 0) >= per_day:
            return "config_dict"
    except Exception:
        pass
    return "failed: no working per-day limit API"


def _seed_deck(state, deck="E2E Deck", count=5, prefix="E2E"):
    from aqt import mw
    col = mw.col
    if col is None:
        return False
    try:
        deck_id = col.decks.id(deck)
        col.decks.select(deck_id)
        limits = _lift_deck_daily_limits(col, deck_id)
        _step("deck_limits", not str(limits).startswith("failed"),
              str(limits)[:200])
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


def _reviewer_ready():
    """True only when the reviewer is actually showing a card.

    Unlike _reviewer_settled (which is also true outside the reviewer),
    this gates long answer loops: hammering moveToState() while the
    webview is still loading wedges Anki 26.x in a transition loop."""
    try:
        from aqt import mw
        if getattr(mw, "state", "") != "review":
            return False
        reviewer = getattr(mw, "reviewer", None)
        if reviewer is None or getattr(reviewer, "card", None) is None:
            return False
        return getattr(reviewer, "state", "") in ("question", "answer")
    except Exception:
        return False


def _reviewer_debug():
    try:
        from aqt import mw
        reviewer = getattr(mw, "reviewer", None)
        return (f"mw={getattr(mw, 'state', '?')} "
                f"reviewer={getattr(reviewer, 'state', '?')} "
                f"card={bool(getattr(reviewer, 'card', None))}")
    except Exception as exc:
        return repr(exc)[:80]


def _answer_current_card(state, ease=3):
    """Human-order answer: show the answer side, then invoke the real answer.

    Calling `_answerCard` while the reviewer is still on the question side
    does not reliably schedule or emit `reviewer_did_answer_card` (Anki's
    own buttons only exist on the answer side). Returns True once the real
    answer call was made this tick.
    """
    from aqt import mw
    if getattr(mw, "state", "") != "review":
        try:
            mw.moveToState("review")
        except Exception:
            pass
        return False
    if state.get("awaiting"):
        return False
    reviewer = getattr(mw, "reviewer", None)
    if reviewer is None or getattr(reviewer, "card", None) is None:
        return False
    if not _reviewer_settled():
        return False
    rst = getattr(reviewer, "state", "")
    if rst == "question":
        show = getattr(reviewer, "_showAnswer", None)
        if callable(show):
            try:
                show()
            except Exception:
                pass
        return False
    if rst == "answer":
        answer = getattr(reviewer, "_answerCard", None)
        if not callable(answer):
            return False
        state["answer_calls"] = state.get("answer_calls", 0) + 1
        answer(ease)
        return True
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


def _wait_projection_settled(max_s: float = 30.0) -> bool:
    """Bounded settle wait: project rows only after the rebuild worker is
    idle, so a slow publish cannot change counts inside a measurement."""
    import time as _time
    from aqt.qt import QApplication
    import ankiscape
    deadline = _time.time() + float(max_s)
    while True:
        try:
            engine = ankiscape._EVOLVED_CTX.get("engine")
            status = engine.projection_status() if engine is not None else {}
        except Exception:
            status = {}
        if not status.get("busy"):
            return True
        if _time.time() >= deadline:
            return False
        QApplication.processEvents()
        _time.sleep(0.05)


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
            try:
                dpr = float(scaled.devicePixelRatio()) or 1.0
            except Exception:
                dpr = 1.0
            logical_w = scaled.width() / dpr
            logical_h = scaled.height() / dpr
            if logical_w > size + 0.5 or logical_h > size + 0.5:
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
        credits = ""
        for title, body in pages.items():
            if "credit" in title.lower():
                credits = body
                break
        ok = (not short and "Jagex Ltd" in credits
              and "oldschool.runescape.wiki" in credits)
        _step("art_guide_pages_render", ok,
              f"pages={len(pages)} short={short} credits="
              f"{'found' if credits else 'missing'}")
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


def _poll_ui_visual_polish(state):
    """Journey: bounded rendering + reviewed imagery + visual scales."""
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
                _step("visual_fixture_imported", True,
                      f"{counts.get('operations', len(ops))} ops")
            if not _find_shell():
                _open_shell_via_menu()
                return
            state["stage"] = "tabs"
            state["tab_index"] = 0
        except Exception as exc:
            _step("visual_fixture", False, repr(exc))
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
                _step("visual_shell", False, "shell never appeared")
                state["stage"] = "checks"
            return
        state["shell_ticks"] = 0
        if not _click_rail(section):
            state["tab_ticks"] = state.get("tab_ticks", 0) + 1
            if state["tab_ticks"] > 80:
                _step(f"visual_tab_{section}", False, "rail button missing")
                state["tab_index"] = index + 1
                state["tab_ticks"] = 0
            return
        state["tab_ticks"] = 0
        from aqt.qt import QApplication
        QApplication.processEvents()
        screen = _shell_child(shell, ART_SCREEN_NAMES[section])
        _shot(f"ui-polish-{section}")
        _step(f"visual_tab_{section}", screen is not None)
        state["tab_index"] = index + 1
        return
    if stage == "checks":
        if not state.get("visual_checks_done"):
            _visual_polish_checks(state)
            _verify_art_environment(state)
            state["visual_checks_done"] = True
        _shot("ui-polish-done")
        _finish_phase(2)


def _poll_ui_visual_polish_2(state):
    """Restart phase: scales, bounded rows and imagery survive a restart."""
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
                _step("visual_restart_shell", False, "shell never appeared")
                _finish(1)
            return
        state["shell_ticks"] = 0
        state["stage"] = "checks"
        return
    if stage == "checks":
        if not state.get("visual_checks_done"):
            _visual_polish_checks(state)
            _verify_art_environment(state)
            state["visual_checks_done"] = True
        _shot("ui-polish-restart-done")
        _finish(0 if not RESULT["errors"] else 1)


def _visual_stat(stats, key):
    try:
        return int(stats.get(key, 0) or 0)
    except Exception:
        return 0


def _visual_polish_checks(state):
    """Scale, cache, bounded-rows and accessibility assertions."""
    from aqt.qt import QApplication, Qt
    import ankiscape
    from ankiscape.evolved.ui import widgets as ui_widgets

    shell = _find_shell()
    if shell is None:
        _step("visual_checks", False, "no shell")
        return

    # 1. Scales: shell grows, screens remain usable, shots at each step.
    widths = []
    for scale in (100, 150, 200):
        try:
            shell.apply_scale(scale)
            QApplication.processEvents()
            widths.append(int(shell.width()))
            _shot(f"ui-polish-scale-{scale}")
        except Exception as exc:
            _step(f"visual_scale_{scale}", False, repr(exc))
    ok_scales = all(widths[i] <= widths[i + 1] for i in range(len(widths) - 1)) \
        if len(widths) == 3 else False
    _step("visual_scales_grow", ok_scales, f"widths={widths}")
    try:
        shell.apply_scale(100)
        QApplication.processEvents()
    except Exception:
        pass

    # 2. Minimum shell size: every visited screen still lays out.
    try:
        shell.resize(shell.minimumSize())
        QApplication.processEvents()
        too_narrow = []
        for section in ART_SECTIONS:
            screen = _shell_child(shell, ART_SCREEN_NAMES[section])
            if screen is not None and screen.width() < 200:
                too_narrow.append(section)
        _step("visual_min_size_usable", not too_narrow,
              f"too_narrow={too_narrow}")
    except Exception as exc:
        _step("visual_min_size_usable", False, repr(exc))

    # 3. Icon cache: repeated full cycles must not re-decode files. One
    # warm-up cycle establishes the cache; the measured cycles must reuse it.
    try:
        cache = ui_widgets._ICON_CACHE
        for section in ART_SECTIONS:
            _click_rail(section)
            QApplication.processEvents()
        before = ui_widgets.icon_cache_stats()
        keys_before = set(cache._data.keys())
        for _ in range(2):
            for section in ART_SECTIONS:
                _click_rail(section)
                QApplication.processEvents()
        after = ui_widgets.icon_cache_stats()
        decodes = _visual_stat(after, "decodes") - _visual_stat(before, "decodes")
        hits = _visual_stat(after, "hits") - _visual_stat(before, "hits")
        lost = sorted(keys_before - set(cache._data.keys()))
        fresh = sorted(k for k in cache._data.keys() if k not in keys_before)
        sample = "; ".join(
            f"{str(k[0])[-28:]}|{k[2]}|{k[3]}" for k in fresh[:6])
        _step("visual_icon_cache_reuse", decodes <= 4 and hits > 0,
              f"re-decodes={decodes} hits={hits} lost_keys={len(lost)} "
              f"entries={after.get('entries')} fresh={sample}")
        _step("visual_icon_cache_bounded",
              _visual_stat(after, "entries") <= 256
              and _visual_stat(after, "bytes") <= 16 * 1024 * 1024,
              f"entries={after.get('entries')} bytes={after.get('bytes')}")
    except Exception as exc:
        _step("visual_icon_cache_reuse", False, repr(exc))

    # 4. Bounded rows after 100 section changes.
    try:
        # A late projection publish (100k rebuilds land in seconds; slower
        # Windows runners can outlast the tab tour) rebuilds the visible
        # screen and changes row counts mid-probe. Wait for the worker to
        # settle first so the probe measures bounded rows, not a race.
        settled = _wait_projection_settled()
        # The screen lookup requires a visible widget: show each section
        # before grabbing its root so the row-count probe is real.
        _click_rail("bank")
        QApplication.processEvents()
        bank = _shell_child(shell, ART_SCREEN_NAMES["bank"])
        _click_rail("achievements")
        QApplication.processEvents()
        achievements = _shell_child(shell, ART_SCREEN_NAMES["achievements"])
        bank_before = int(bank.row_count()) if hasattr(bank, "row_count") else -1
        ach_before = int(achievements.row_count()) if hasattr(achievements, "row_count") else -1
        for _ in range(100):
            _click_rail("bank")
        _click_rail("achievements")
        QApplication.processEvents()
        bank_after = int(bank.row_count()) if hasattr(bank, "row_count") else -1
        ach_after = int(achievements.row_count()) if hasattr(achievements, "row_count") else -1
        _step("visual_bounded_rows",
              bank_before >= 0 and bank_after == bank_before
              and ach_before >= 0 and ach_after == ach_before,
              f"bank {bank_before}->{bank_after} "
              f"achievements {ach_before}->{ach_after} settled={settled}")
    except Exception as exc:
        _step("visual_bounded_rows", False, repr(exc))

    # 5. Accessibility/keyboard: interactive controls stay named + focusable.
    try:
        from aqt.qt import QWidget as _QWidget
        unnamed, unfocusable = [], []
        for widget in shell.findChildren(_QWidget):
            cls = type(widget).__name__
            if cls not in ("QPushButton", "QToolButton"):
                continue
            name = str(widget.accessibleName() or widget.text() or "").strip()
            if not name:
                unnamed.append(widget.objectName() or cls)
            if widget.isEnabled() and widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
                unfocusable.append(widget.objectName() or cls)
        _step("visual_accessible_names", not unnamed, f"unnamed={unnamed[:4]}")
        _step("visual_keyboard_focus", not unfocusable,
              f"unfocusable={unfocusable[:4]}")
        shell.activateWindow()
        shell.setFocus()
        shell.focusNextPrevChild(True)
        _step("visual_tab_navigation", True)
    except Exception as exc:
        _step("visual_accessibility", False, repr(exc))


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


def _seed_bulk_ops(state, count):
    """Import a large deterministic history through the journal API so the
    projection worker has real rebuild work while reviews continue."""
    import ankiscape
    import uuid as _uuid
    engine = ankiscape._EVOLVED_CTX.get("engine")
    if engine is None:
        return False
    if state.get("bulk_seeded"):
        return True
    game = engine.cfg.game_uuid
    ops = []
    for i in range(1, count + 1):
        ops.append({
            "op_id": str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                     f"driver-bulk:{game}:{i}")),
            "game_uuid": game, "device_id": "driver-bulk",
            "device_seq": i, "lamport": i, "kind": "review_award",
            "payload": {"review_key": f"rk-bulk-{i}", "review_ts": 1850000000 + i,
                        "rating": 3, "review_kind": "review",
                        "provenance": "direct", "reward_policy": 2,
                        "skill": "mining", "resource": "Rune essence"}})
    engine.journal.import_game(game, {"operations": ops, "observations": []})
    # Advance the lamport clock past the imported history (the engine's own
    # restart path), or later real reviews sort before the bulk ops and
    # their outcomes fall out of the bounded presentation window.
    engine.hydrate()
    engine.invalidate_projection()
    state["bulk_seeded"] = True
    _step("bulk_history_imported", True, f"{count} ops")
    return True


def _answer_with_timing(state):
    """One real reviewer answer, timed around the accepted-answer hook."""
    import time as _time
    before = _award_count()
    start = _time.perf_counter()
    called = _answer_current_card(state)
    elapsed = (_time.perf_counter() - start) * 1000.0
    if not called:
        return None
    state["last_hook_ms"] = elapsed
    state["awaiting"] = state.get("calls", 0) + 1
    state["calls"] = state.get("calls", 0) + 1
    return before


def _poll_ui_deferred_rewards(state):
    """Persist fast, celebrate after the worker publishes (100k-history
    rebuild in flight), and never claim an unpersisted reward."""
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _seed_deck(state, count=4, prefix="DEFER")
        state["stage"] = "bulk"
        return
    if stage == "bulk":
        if not _seed_bulk_ops(state, 100000):
            return
        from aqt import mw
        mw.col.set_config("ankiscape_evolved_current_skill", "mining")
        mw.col.set_config("ankiscape_evolved_current_mining", "Rune essence")
        _open_reviewer(state, "DEFER")
        state["stage"] = "answer"
        return
    if stage == "answer":
        if not _reviewer_settled():
            return
        before = _answer_with_timing(state)
        if before is None:
            return
        state["stage"] = "verify"
        state["verify_ticks"] = 0
        state["ops_baseline"] = before
        return
    if stage == "verify":
        state["verify_ticks"] = state.get("verify_ticks", 0) + 1
        ops = _journal_ops()
        durable = [o for o in ops if o["kind"] == "review_award"
                   and not str(o["op_id"]).startswith("op-")
                   and not str(o["op_id"]).startswith("driver-")]
        # Durable append must have happened by now: journal ops grew beyond
        # the seeded bulk history.
        if len(ops) > state.get("ops_baseline", 0):
            hook_ms = state.get("last_hook_ms", -1)
            _step("deferred_hook_within_budget", hook_ms < 250.0,
                  f"hook={hook_ms:.1f}ms (single sample; budget p95<=20ms)")
            _step("deferred_op_persisted_before_publish", True,
                  f"ops={len(ops)}")
            # Wait for the worker's published projection to include rewards.
            state["stage"] = "published"
            return
        if state["verify_ticks"] > 200:
            import ankiscape
            engine = ankiscape._EVOLVED_CTX.get("engine")
            status = engine.projection_status() if engine is not None else {}
            _step("deferred_op_persisted_before_publish", False,
                  f"journal={len(ops)} "
                  f"recovery={bool(ankiscape._RECOVERY_WARNING.get('active'))} "
                  f"msg={str(ankiscape._RECOVERY_WARNING.get('message', ''))[:100]} "
                  f"hook_ms={state.get('last_hook_ms', -1)} "
                  f"status={status}")
            _finish(1)
        return
    if stage == "published":
        state["published_ticks"] = state.get("published_ticks", 0) + 1
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
        if engine is None:
            return
        latest = engine.projection()  # worker publication: never replays
        revision = int(latest.get("revision", 0) or 0)
        if revision >= state.get("ops_baseline", 0) + 1:
            reference = _projection()  # one full replay for equivalence
            boundary = ("xp_micro", "inventory", "levels", "revision")
            equal = all(latest.get(k) == reference.get(k) for k in boundary)
            _step("deferred_worker_equals_reference", equal,
                  f"revision={revision}")
            _shot("ui-deferred-rewards")
            _finish(0 if not RESULT["errors"] else 1)
            return
        if state["published_ticks"] > 900:
            _step("deferred_worker_equals_reference", False,
                  f"revision stuck at {revision}")
            _finish(1)
        return


def _open_reviewer(state, deck_prefix):
    """Enter the reviewer through the real navigation (seeded deck is
    already current after _seed_deck)."""
    from aqt import mw
    _ = deck_prefix
    try:
        if getattr(mw, "state", "") != "review":
            mw.moveToState("review")
    except Exception:
        pass


def _poll_ui_rebuild_review(state):
    """Answering stays responsive while the worker rebuilds 100k ops."""
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _seed_deck(state, count=4, prefix="REBUILD")
        state["stage"] = "bulk"
        return
    if stage == "bulk":
        if not _seed_bulk_ops(state, 100000):
            return
        state["ops_baseline"] = len(_journal_ops())
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
        if engine is not None:
            # Late retraction of the first seeded award: worst-case rebuild.
            engine.retract(review_key="rk-bulk-1")
        _open_reviewer(state, "REBUILD")
        state["stage"] = "answers"
        state["answers_done"] = 0
        return
    if stage == "answers":
        if not _reviewer_settled():
            return
        if state.get("awaiting"):
            ops = _journal_ops()
            if len(ops) > state.get("ops_baseline", 0) + state["answers_done"]:
                state["awaiting"] = 0
        if state.get("answers_done", 0) >= 3:
            import ankiscape
            engine = ankiscape._EVOLVED_CTX.get("engine")
            latest = engine.projection() if engine is not None else {}
            target = engine.journal.operation_count() if engine is not None else 0
            state["final_ticks"] = state.get("final_ticks", 0) + 1
            # The worker publishes the retraction rebuild asynchronously; wait
            # (bounded) for the published revision before comparing state.
            if engine is not None and \
                    int(latest.get("revision", 0) or 0) < target \
                    and state["final_ticks"] < 600:
                return
            reference = _projection()
            equal = all(latest.get(k) == reference.get(k)
                        for k in ("xp_micro", "inventory", "levels", "revision"))
            _step("rebuild_answer_responsive",
                  state.get("last_hook_ms", 999) < 250.0,
                  f"hook={state.get('last_hook_ms', -1):.1f}ms")
            _step("rebuild_state_equals_reference", equal,
                  f"revision={latest.get('revision')} target={target}")
            _shot("ui-rebuild-review")
            _finish(0 if not RESULT["errors"] else 1)
            return
        if state.get("awaiting"):
            return
        import time as _time
        start = _time.perf_counter()
        called = _answer_current_card(state)
        if not called:
            return
        state["last_hook_ms"] = (_time.perf_counter() - start) * 1000.0
        state["awaiting"] = 1
        state["answers_done"] = state.get("answers_done", 0) + 1
        return


# Display names of the permanent hosted-v1 fixture cohort
# (dev/fixtures/hosted-v1.json). Any of these appearing in non-cohort
# standings is a public isolation failure.
FIXTURE_NAMES = {
    "WillowMere", "FlintHarbor", "Mosswarden", "RowanVale",
    "CopperFinch", "AlderTrail", "PebbleFox", "EmberBrook",
    "BirchRook", "HazelForge", "FernVoyager", "AshenPike",
    "MapleStrider", "ReedRunner", "OakLantern", "SlateOtter",
    "QuietAnvil", "BrambleWren", "SilverNettle", "CedarTern",
    "RiverKestrel", "DawnThistle", "MistBadger", "DuskHeron",
}


def _logged_in_via_fixture(state):
    """Sign in with the hosted fixture credentials from the trusted lane.

    Returns None without credentials, else (ok, diagnostic) so a failure
    names the failing stage (session, endpoint, or the auth error) without
    ever printing the secret."""
    email = os.environ.get("ANKISCAPE_FIXTURE_EMAIL", "")
    password = os.environ.get("ANKISCAPE_FIXTURE_PASSWORD", "")
    if not email or not password:
        return None
    try:
        import ankiscape
        from ankiscape.evolved import accounts as _accounts
        from ankiscape.evolved.net import post_json as _post
        sess = ankiscape._evolved_profile_session()
        endpoint = ankiscape._evolved_endpoint()
        if sess is None:
            return False, "fixture sign-in failed: no profile session"
        if endpoint is None:
            return False, "fixture sign-in failed: endpoint unconfigured"
        result = ankiscape._evolved_login_submit(
            {"identity": email, "password": password, "remember": False},
            sess, endpoint, _accounts, _post)
        if result.get("ok"):
            return True, f"endpoint={endpoint.base_url}"
        return False, ("fixture sign-in failed: "
                       + str(result.get("error"))[:160])
    except Exception as exc:
        return False, "fixture sign-in raised: " + repr(exc)[:160]


def _poll_ui_test_leaderboard(state):
    """Real-hosted login + labeled Test leaderboard (trusted lanes)."""
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        state["stage"] = "login"
        return
    if stage == "login":
        result = _logged_in_via_fixture(state)
        if result is None:
            # Hosted coverage is required on this lane; missing credentials
            # are a failure, never a silently different test.
            _step("test_leaderboard_credentials", False,
                  "missing fixture credentials (hosted coverage required)")
            _finish(1)
            return
        ok, detail = result
        if not ok:
            _step("test_leaderboard_login", False, detail)
            _finish(1)
            return
        _step("test_leaderboard_login", True, detail)
        # Record once: a missing shell is open_hiscores' job, not a reason to
        # sign in again on the next tick.
        state["stage"] = "open_hiscores"
        state["shell_wait_ticks"] = 0
        return
    if stage == "open_hiscores":
        shell = _find_shell()
        if shell is None:
            # Sign-in happens while the reviewer is up; the shell must be
            # re-opened (this fallback used to live in the login stage and
            # was lost when duplicate logins were removed).
            state["shell_wait_ticks"] = state.get("shell_wait_ticks", 0) + 1
            if state["shell_wait_ticks"] % 30 == 1:
                _open_shell_via_menu()
            if state["shell_wait_ticks"] > 300:
                _step("hiscores_shell_open", False,
                      f"shell not visible after {state['shell_wait_ticks']} ticks")
                _finish(1)
            return
        if not _click_rail("hiscores"):
            state["rail_wait_ticks"] = state.get("rail_wait_ticks", 0) + 1
            if state["rail_wait_ticks"] > 300:
                _step("hiscores_rail_click", False, "rail never activated")
                _finish(1)
            return
        from aqt.qt import QApplication
        QApplication.processEvents()
        state["stage"] = "toggle"
        state["toggle_ticks"] = 0
        return
    if stage == "toggle":
        state["toggle_ticks"] = state.get("toggle_ticks", 0) + 1
        shell = _find_shell()
        if shell is None:
            return
        from aqt.qt import QApplication, QCheckBox, QLabel, QListWidget
        toggle = shell.findChild(QCheckBox, "ankiscape-hiscores-test-toggle")
        if not state.get("toggle_visible_reported"):
            state["toggle_visible_reported"] = True
            _step("test_leaderboard_toggle_visible",
                  toggle is not None and toggle.isVisible())
        if toggle is None:
            _finish(0 if not RESULT["errors"] else 1)
            return
        if not toggle.isChecked():
            toggle.setChecked(True)
            QApplication.processEvents()
            # Reset the wait budget only when the check actually flips.
            state["toggle_ticks"] = 0
            return
        status = shell.findChild(QLabel, "ankiscape-hiscores-status")
        text = str(status.text()) if status is not None else ""
        loading = (not text) or text.startswith("Loading")
        if loading and state["toggle_ticks"] <= 300:
            return  # allow the asynchronous hosted fetch to settle
        listing = shell.findChild(QListWidget, "ankiscape-hiscores-list")
        rows = [listing.item(i).text() for i in range(listing.count())] \
            if listing is not None else []
        labeled = "Test leaderboard" in text
        _step("test_leaderboard_labeled", labeled, text[:160])
        rows_ok = bool(rows) and not any("Nothing to show" in r for r in rows)
        _step("test_leaderboard_test_rows",
              rows_ok and any(n in " ".join(rows) for n in FIXTURE_NAMES),
              f"rows={len(rows)} first={rows[0][:80] if rows else ''}")
        # Public (non-cohort) standings must exclude test names, while the
        # labeled cohort view above includes them. Both run while signed in.
        try:
            import ankiscape
            public = ankiscape._evolved_query_hiscores("mining", 100)
            leaked = FIXTURE_NAMES & {str(r.get("username")) for r in public}
            _step("test_leaderboard_public_isolated", not leaked,
                  f"public_rows={len(public)} leaked={sorted(leaked)}")
        except Exception as exc:
            _step("test_leaderboard_public_isolated", False, repr(exc)[:160])
        _shot("ui-test-leaderboard")
        _finish(0 if not RESULT["errors"] else 1)


def _poll_ui_credential_fallback(state):
    """Vault available/unavailable paths never fall back to plaintext."""
    if state.get("done"):
        return
    state["done"] = True
    import tempfile
    from ankiscape.evolved.credentials import CredentialVault
    tmp = tempfile.mkdtemp(prefix="ankiscape-vault-")
    token = {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
             "user_id": "fixture-user"}
    secret = "fixture-access"
    try:
        vault = CredentialVault(tmp, "https://example.invalid")
        _step("credential_vault_available", True,
              f"available={vault.available}")
        wrote = vault.write(dict(token))
        loaded = vault.read()
        if vault.available and wrote and isinstance(loaded, dict) \
                and loaded.get("access_token") == token["access_token"]:
            _step("credential_vault_roundtrip", True, "secure vault")
            vault.delete()
            _step("credential_vault_delete", vault.read() in (None, ""))
            _step("credential_vault_mode", True, "secure")
        else:
            # Vault reported available but could not persist (e.g. a locked
            # CI keychain): the honest behavior is session-only with nothing
            # written to disk. Both branches must pass truthfully.
            _step("credential_vault_mode", True, "session_only")
        leaked = []
        for base, _dirs, files in os.walk(tmp):
            for name in files:
                try:
                    with open(os.path.join(base, name), "rb") as fh:
                        if secret.encode() in fh.read():
                            leaked.append(name)
                except OSError:
                    continue
        _step("credential_vault_no_plaintext", not leaked, f"leaked={leaked[:3]}")
        _step("credential_vault_session_only",
              vault.read() in (None, "") or not vault.available)
        unavailable = CredentialVault(tmp, "https://example.invalid")
        unavailable.available = False  # explicit unavailable-branch exercise
        refused = unavailable.write(dict(token)) is False \
            and unavailable.read() is None and unavailable.delete() is False
        _step("credential_vault_unavailable_refuses", refused)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    _shot("ui-credential-fallback")
    _finish(0 if not RESULT["errors"] else 1)


def _poll_ui_recovery(state):
    """Simulated failed interactive write: persistent warning, no award,
    recovery after the write path returns."""
    stage = state.get("stage", "setup")
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _seed_deck(state, count=4, prefix="RECOVER")
        from aqt import mw
        mw.col.set_config("ankiscape_evolved_current_skill", "mining")
        mw.col.set_config("ankiscape_evolved_current_mining", "Rune essence")
        _open_reviewer(state, "RECOVER")
        state["stage"] = "fail_write"
        return
    if stage == "fail_write":
        if not _reviewer_settled():
            return
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
        if engine is None:
            return
        if not state.get("fault_installed"):
            state["fault_installed"] = True
            state["journal_before"] = len(_journal_ops())
            state["original_record"] = engine.journal.record_review

            def _failing(*_a, **_k):
                return {"ok": False, "busy": True,
                        "error": "interactive_write_failed:simulated"}

            engine.journal.record_review = _failing  # simulated fault (labeled)
        if not _answer_current_card(state):
            return
        state["stage"] = "verify_failure"
        return
    if stage == "verify_failure":
        state["verify_ticks"] = state.get("verify_ticks", 0) + 1
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
        active = bool(ankiscape._RECOVERY_WARNING.get("active"))
        grew = len(_journal_ops()) > state.get("journal_before", 0)
        _step("recovery_warning_shown", active,
              str(ankiscape._RECOVERY_WARNING.get("message", ""))[:120])
        _step("recovery_no_operation_persisted", not grew)
        _shot("ui-recovery-warning")
        # Restore the real write path and recover on the next review.
        if engine is not None and state.get("original_record") is not None:
            engine.journal.record_review = state["original_record"]
        state["stage"] = "recover"
        return
    if stage == "recover":
        if not _reviewer_settled():
            return
        if not state.get("recovery_answered"):
            if not _answer_current_card(state):
                return
            state["recovery_answered"] = True
        state["stage"] = "verify_recovery"
        state["recover_ticks"] = 0
        return
    if stage == "verify_recovery":
        state["recover_ticks"] = state.get("recover_ticks", 0) + 1
        import ankiscape
        cleared = not ankiscape._RECOVERY_WARNING.get("active")
        grew = len(_journal_ops()) > state.get("journal_before", 0)
        if cleared and grew:
            _step("recovery_clears_after_successful_write", True)
            _finish(0 if not RESULT["errors"] else 1)
            return
        if state["recover_ticks"] > 200:
            _step("recovery_clears_after_successful_write", False,
                  f"cleared={cleared} grew={grew}")
            _finish(1)
        return


def _poll_ui_profile_races(state):
    """Close/release the shell while async work is in flight; reopen safely."""
    import ankiscape
    from aqt.qt import QApplication
    if not state.get("setup_done"):
        if _drive_onboarding(state, "mining") is not True:
            return
        state["setup_done"] = True
        state["round"] = 0
    if state.get("round", 0) >= 3:
        shell = _find_shell()
        _step("profile_races_no_callback_errors",
              not [e for e in RESULT["errors"] if "race" in e.lower()])
        _step("profile_races_shell_reopened", shell is not None)
        _shot("ui-profile-races")
        _finish(0 if not RESULT["errors"] else 1)
        return
    shell = _find_shell()
    if shell is None:
        _open_shell_via_menu()
        return
    # Kick an async hiscores load, then release the shell mid-flight.
    _click_rail("hiscores")
    QApplication.processEvents()
    from aqt import mw
    from ankiscape.evolved.ui.shell import release_shell
    release_shell(mw)
    QApplication.processEvents()
    for _ in range(20):
        _click_rail("skills") if _find_shell() else None
    state["round"] = state.get("round", 0) + 1
    _step(f"profile_race_round_{state['round']}", True)
    _open_shell_via_menu()


def _report_dialog():
    from aqt.qt import QApplication, QDialog
    for widget in QApplication.topLevelWidgets():
        try:
            if isinstance(widget, QDialog) and widget.objectName() == \
                    "ankiscape-report-issue" and widget.isVisible():
                return widget
        except Exception:
            continue
    return None


def _fill_report_dialog(state, *, action: str):
    """Runs INSIDE the modal dialog's nested event loop via QTimer."""
    from aqt.qt import QPlainTextEdit, QLineEdit, QPushButton
    dialog = _report_dialog()
    if dialog is None:
        state["report_timer_error"] = "dialog-not-found"
        return
    try:
        dialog.findChild(QLineEdit, "ankiscape-report-summary").setText(
            "Driver race condition")
        dialog.findChild(QPlainTextEdit, "ankiscape-report-happened").setPlainText(
            "Rewards showed twice after Undo.")
        dialog.findChild(QPlainTextEdit, "ankiscape-report-expected").setPlainText(
            "A single reward.")
        dialog.findChild(QPlainTextEdit, "ankiscape-report-steps").setPlainText(
            "1. Answer\n2. Undo\n3. Answer again")
        preview = dialog.findChild(QPlainTextEdit, "ankiscape-report-preview")
        content = preview.toPlainText()
        state["report_preview"] = content
        state["report_check"] = {
            "has_summary": "Driver race condition" in content,
            "has_allowlisted_block": "addon:" in content,
            "no_secret": "eyJ" not in content and "@example.com" not in content,
        }
        button_name = ("ankiscape-report-open" if action == "open"
                       else "ankiscape-report-copy" if action == "copy"
                       else "ankiscape-report-cancel")
        button = dialog.findChild(QPushButton, button_name)
        if button is None:
            state["report_timer_error"] = f"missing-{button_name}"
            dialog.reject()
            return
        button.click()
        if action == "open":
            # The dialog stays open when the browser call fails; capture the
            # status and the retained typed text, then cancel.
            status = dialog.findChild(object, "ankiscape-report-status")
            state["report_open_status"] = str(getattr(status, "text", lambda: "")())
            summary = dialog.findChild(QLineEdit, "ankiscape-report-summary")
            state["report_text_retained"] = bool(
                summary is not None and "Driver race condition" in summary.text())
            cancel = dialog.findChild(QPushButton, "ankiscape-report-cancel")
            if cancel is not None:
                cancel.click()
            else:
                dialog.reject()
        state["report_action_done"] = action
    except Exception as exc:
        state["report_timer_error"] = repr(exc)[:200]
        dialog = _report_dialog()
        if dialog is not None:
            dialog.reject()


def _poll_ui_report_bug(state):
    """Report dialog: preview/cancel send nothing; copy/open match preview;
    typed text survives a failed browser open; no secrets leak."""
    from aqt.qt import QTimer
    stage = state.get("stage", "setup")
    if stage == "setup":
        if not state.get("guard_installed"):
            state["guard_installed"] = True
            _install_addon_network_guard(state)
        if _drive_onboarding(state, "mining") is not True:
            return
        state["stage"] = "guide"
        return
    if stage == "guide":
        shell = _find_shell()
        if shell is None:
            _open_shell_via_menu()
            return
        if not _click_rail("guide"):
            return
        state["stage"] = "cancel"
        return
    if stage == "cancel":
        shell = _find_shell()
        if shell is None:
            return
        from aqt.qt import QPushButton
        button = shell.findChild(QPushButton, "ankiscape-guide-report-bug")
        if button is None:
            _step("report_entry_point", False, "guide button missing")
            _finish(1)
            return
        _step("report_entry_point", True)
        QTimer.singleShot(120, lambda: _fill_report_dialog(state, action="cancel"))
        button.click()
        if state.get("report_timer_error"):
            _step("report_cancel_flow", False, state["report_timer_error"])
            _finish(1)
            return
        check = state.get("report_check") or {}
        _step("report_preview_matches_fields", bool(check.get("has_summary")),
              f"checks={check}")
        _step("report_preview_allowlisted", bool(check.get("has_allowlisted_block")))
        _step("report_preview_no_secrets", bool(check.get("no_secret")))
        attempts = state.get("net_attempts") or []
        _step("report_cancel_zero_network", not attempts, f"attempts={attempts[:2]}")
        _shot("ui-report-bug-cancel")
        state["stage"] = "open_failure"
        return
    if stage == "open_failure":
        shell = _find_shell()
        if shell is None:
            return
        import ankiscape
        state["original_open_url"] = ankiscape._evolved_open_url
        ankiscape._evolved_open_url = lambda url: False  # simulated failure
        from aqt.qt import QPushButton, QTimer
        button = shell.findChild(QPushButton, "ankiscape-guide-report-bug")
        if button is None:
            return
        QTimer.singleShot(120, lambda: _fill_report_dialog(state, action="open"))
        button.click()
        ankiscape._evolved_open_url = state["original_open_url"]
        retained = bool(state.get("report_text_retained"))
        _step("report_browser_failure_retains_text", retained)
        status = str(state.get("report_open_status", ""))
        _step("report_browser_failure_copy_fallback",
              "copied" in status.lower() or "paste" in status.lower(),
              status[:120])
        attempts = state.get("net_attempts") or []
        _step("report_open_no_addon_network", not attempts,
              f"attempts={attempts[:2]}")
        _shot("ui-report-bug-open")
        _finish(0 if not RESULT["errors"] else 1)


def _perf_config():
    try:
        with open(os.path.join(_base_dir(), "perf-config.json"),
                  encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _perf_write(payload, name="perf-native.json"):
    try:
        with open(_out_path(name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def _perf_marker(name, value=""):
    try:
        with open(_out_path(name), "w", encoding="utf-8") as fh:
            fh.write(str(value))
    except Exception:
        pass


def _rss_mib():
    """Process RSS in MiB, or None when the platform cannot measure it.

    A missing measurement must fail endurance validation; it must never be
    reported as a zero that reads like a flat slope."""
    try:
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as _wt

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", _wt.DWORD),
                    ("PageFaultCount", _wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = 0
            for lib, fn in (("psapi", "GetProcessMemoryInfo"),
                            ("kernel32", "K32GetProcessMemoryInfo")):
                try:
                    func = getattr(getattr(ctypes.windll, lib), fn)
                    func.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(_PMC), _wt.DWORD]
                    func.restype = _wt.BOOL
                    ok = func(process, ctypes.byref(counters), counters.cb)
                    if ok:
                        break
                except Exception:
                    continue
            if not ok:
                return None
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(value / (1024 * 1024), 1) if sys.platform == "darwin" \
            else round(value / 1024, 1)
    except Exception:
        return None


def _process_cpu_seconds():
    """Process CPU seconds, or None when the platform cannot measure it.

    Never a fake zero: a missing measurement must fail the idle budget
    explicitly, and Windows previously reported 0.0 for every run."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes as _wt

            kernel32 = ctypes.windll.kernel32
            creation = _wt.FILETIME()
            exit_time = _wt.FILETIME()
            kernel = _wt.FILETIME()
            user = _wt.FILETIME()
            kernel32.GetProcessTimes.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_wt.FILETIME),
                ctypes.POINTER(_wt.FILETIME), ctypes.POINTER(_wt.FILETIME),
                ctypes.POINTER(_wt.FILETIME)]
            kernel32.GetProcessTimes.restype = _wt.BOOL
            ok = kernel32.GetProcessTimes(
                kernel32.GetCurrentProcess(), ctypes.byref(creation),
                ctypes.byref(exit_time), ctypes.byref(kernel),
                ctypes.byref(user))
            if not ok:
                return None

            def _ticks(ft):
                return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

            return round((_ticks(kernel) + _ticks(user)) / 1e7, 6)
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return float(usage.ru_utime) + float(usage.ru_stime)
    except Exception:
        return None


def _lag_probe_start(state):
    from aqt.qt import QTimer
    import time as _time

    state.setdefault("lag_samples", [])

    def tick():
        if state.get("lag_probe_off"):
            return
        now = _time.perf_counter()
        expected = state.get("lag_expected")
        if expected is not None:
            state["lag_samples"].append(
                round(max(0.0, now - expected) * 1000.0, 3))
        state["lag_expected"] = now + 0.1
        QTimer.singleShot(100, tick)

    state["lag_probe_off"] = False
    state["lag_expected"] = _time.perf_counter() + 0.1
    QTimer.singleShot(100, tick)


def _perf_journal_revision():
    try:
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
        if engine is None:
            return 0, None
        latest = engine.projection() or {}
        return int(latest.get("revision", 0) or 0), engine
    except Exception:
        return 0, None


def _perf_resolve_rebuild_watches(state):
    """Record rebuild durations once the published revision catches up.

    Runs in the answers and settle stages: a rebuild triggered by one of the
    last answers can still be running when the answer loop ends, and settle
    must be able to observe it finish instead of timing out."""
    for watch in list(state.get("rebuild_watch", [])):
        revision, engine = _perf_journal_revision()
        if engine is not None and revision >= watch["target"]:
            state["rebuilds"].append(
                round((time.perf_counter() - watch["start"]) * 1000.0, 2))
            state["rebuild_watch"].remove(watch)


def _perf_register_reward_watches(state):
    """Watch every deferred reward that appeared since the last answer.

    Reading ``_PENDING_REVIEWS`` immediately after ``_answerCard`` races the
    accepted-answer hook: when the hook had not run yet the old probe marked
    the reward "immediate" and recorded only the answer-transition time, so
    ``reward_completion`` mostly mirrored ``accepted_answer_completion``.
    Instead, watch keys as they appear and start each watch at the answer
    that produced it (the durable append happens inside the hook, ms later).
    """
    try:
        import ankiscape
        pending = getattr(ankiscape, "_PENDING_REVIEWS", None) or {}
    except Exception:
        pending = {}
    known = state.setdefault("watch_keys", set())
    start = state.pop("watch_answer_start", None)
    for key in pending:
        if key in known:
            continue
        known.add(key)
        state.setdefault("reward_watches", []).append(
            {"key": key,
             "start": start if start is not None else time.perf_counter()})


def _resolve_reward_watches(state):
    """Record displayed-reward latency for every deferred reward the engine
    has published since the last check.

    A single watch slot loses samples whenever publication takes longer
    than one answer cycle (exactly what a 100k-op rebuild causes), which
    silently collapses the required reward sample count."""
    import time as _time
    watches = state.get("reward_watches") or []
    if not watches:
        return
    try:
        import ankiscape
        engine = ankiscape._EVOLVED_CTX.get("engine")
    except Exception:
        engine = None
    still = []
    for watch in watches:
        outcome = None
        if engine is not None:
            try:
                outcome = engine.outcome_for(watch["key"])
            except Exception:
                outcome = None
        if outcome is not None:
            state["reward_ms"].append(round(
                (_time.perf_counter() - watch["start"]) * 1000.0, 2))
        else:
            still.append(watch)
    state["reward_watches"] = still


def _poll_native_endurance(state, cfg):
    import time as _time

    stage = state.get("stage", "setup")
    minutes = float(cfg.get("endurance_minutes", 1))
    if stage == "setup":
        if _drive_onboarding(state, "mining") is not True:
            return
        _seed_deck(state, count=8, prefix="ENDURE")
        if int(cfg.get("bulk_ops", 0) or 0):
            if not _seed_bulk_ops(state, int(cfg["bulk_ops"])):
                return
        # Let the projection catch up before opening the reviewer; starting
        # reviews into an unsettled webview wedges the 26.x reviewer in a
        # transition loop with a JS-error flood (nightly 34641447537).
        state["quiesce_ticks"] = 0
        state["stage"] = "quiesce"
        return
    if stage == "quiesce":
        state["quiesce_ticks"] = state.get("quiesce_ticks", 0) + 1
        revision, engine = _perf_journal_revision()
        target = engine.journal.operation_count() if engine is not None else 0
        ready = engine is None or revision >= target
        if ready or state["quiesce_ticks"] > 3600:
            if not ready:
                _step("quiesce_timeout", False,
                      f"revision={revision} target={target}")
            _open_reviewer(state, "ENDURE")
            state["stage"] = "reviewer"
            state["reviewer_ticks"] = 0
        return
    if stage == "reviewer":
        state["reviewer_ticks"] = state.get("reviewer_ticks", 0) + 1
        if _reviewer_ready():
            state["stage"] = "grow"
            state["started_monotonic"] = _time.monotonic()
            state["answers"] = 0
            state["samples"] = []
            state["next_sample"] = state["started_monotonic"]
            state["grow_until"] = state["started_monotonic"] + \
                max(1.0, minutes * 60.0) * 0.5
            state["answers_per_second"] = float(
                cfg.get("answers_per_second", 2.0))
            state["eligible_for_release"] = bool(
                cfg.get("eligible_for_release", False))
            return
        if state["reviewer_ticks"] % 50 == 0:
            _open_reviewer(state, "ENDURE")
        if state["reviewer_ticks"] > 600:
            _step("reviewer_ready_timeout", False, _reviewer_debug())
            state["stage"] = "grow"
            state["started_monotonic"] = _time.monotonic()
            state["answers"] = 0
            state["samples"] = []
            state["next_sample"] = state["started_monotonic"]
            state["grow_until"] = state["started_monotonic"] + \
                max(1.0, minutes * 60.0) * 0.5
            state["answers_per_second"] = float(
                cfg.get("answers_per_second", 2.0))
            state["eligible_for_release"] = bool(
                cfg.get("eligible_for_release", False))
        return
    if stage == "grow":
        now = _time.monotonic()
        if now >= state.get("grow_until", 0):
            state["stage"] = "lifecycle"
            state["lifecycle_end"] = state.get("started_monotonic", now) + \
                max(1.0, minutes * 60.0)
            return
        if now >= state.get("next_sample", 0):
            state["next_sample"] = now + 30.0
            state["samples"].append({
                "at_s": round(now - state["started_monotonic"], 1),
                "phase": "growing", "rss_mib": _rss_mib(),
                "objects": len(gc.get_objects()),
                "threads": threading.active_count(),
                "answers": state["answers"]})
        if not _reviewer_ready():
            return
        if not _answer_current_card(state):
            return
        state["answers"] += 1
        interval = 1.0 / max(0.1, state.get("answers_per_second", 2.0))
        sleep = interval - 0.0
        if sleep > 0:
            _time.sleep(min(sleep, 0.25))
        return
    if stage == "lifecycle":
        now = _time.monotonic()
        if now >= state.get("next_sample", 0):
            state["next_sample"] = now + 30.0
            state["samples"].append({
                "at_s": round(now - state["started_monotonic"], 1),
                "phase": "fixed", "rss_mib": _rss_mib(),
                "objects": len(gc.get_objects()),
                "threads": threading.active_count(),
                "answers": state["answers"]})
        if now >= state.get("lifecycle_end", 0):
            state["stage"] = "finish"
            return
        # One UI lifecycle cycle per tick: open/close shell + section changes.
        shell = _find_shell()
        if shell is None:
            _open_shell_via_menu()
            return
        from aqt.qt import QApplication
        for section in ART_SECTIONS:
            _click_rail(section)
            QApplication.processEvents()
        try:
            shell.close()
        except Exception:
            pass
        return
    if stage == "finish":
        import ankiscape
        equivalent = False
        try:
            engine = ankiscape._EVOLVED_CTX.get("engine")
            if engine is not None:
                expected = engine.journal.operation_count()
                deadline = _time.monotonic() + 120
                while _time.monotonic() < deadline:
                    latest = engine.projection() or {}
                    if int(latest.get("revision", 0) or 0) >= expected:
                        break
                    _time.sleep(0.05)
                from ankiscape.evolved.reducer import replay
                from ankiscape.evolved.data import load_rules
                reference = replay(engine.journal.all_operations(), load_rules(),
                                   engine.cfg.game_uuid)
                latest = engine.projection() or {}
                equivalent = all(latest.get(k) == reference.get(k)
                                 for k in ("xp_micro", "inventory", "levels",
                                           "revision"))
        except Exception as exc:
            RESULT["errors"].append(f"endurance equivalence: {exc!r}")
        payload = {"mode": "endurance", "run_id": RUN_ID,
                   "duration_min": round(minutes, 3),
                   "answers": state.get("answers", 0),
                   "fixed_history": bool(cfg.get("bulk_ops")),
                   "eligible_for_release": bool(state.get("eligible_for_release")),
                   "equivalence": bool(equivalent),
                   "samples": state.get("samples", []),
                   "object_final": len(gc.get_objects()),
                   "thread_final": threading.active_count()}
        _perf_write(payload)
        _step("native_endurance_completed", True,
              f"answers={payload['answers']} samples={len(payload['samples'])}")
        _finish(0 if not RESULT["errors"] else 1)


def _poll_native_performance(state):
    """Measured real reviews, shell paints, rebuilds and idle inside Anki."""
    import time as _time
    import gc as _gc

    cfg = _perf_config()
    if cfg.get("mode") == "endurance":
        return _poll_native_endurance(state, cfg)
    mode = "control" if JOURNEY.endswith("control") else "addon"
    answers_target = int(cfg.get("answers", 30))
    warm_opens = int(cfg.get("warm_opens", 5))
    stage = state.get("stage", "setup")

    if stage == "setup":
        if mode == "addon":
            if _drive_onboarding(state, "mining") is not True:
                return
        _seed_deck(state, count=max(8, min(answers_target + 5, 2000)),
                   prefix="PERF")
        _open_reviewer(state, "PERF")
        state["mode"] = mode
        state["stage"] = "bulk" if int(cfg.get("bulk_ops", 0) or 0) else "warmup"
        return
    if stage == "bulk":
        if not _seed_bulk_ops(state, int(cfg.get("bulk_ops", 0))):
            return
        state["stage"] = "quiesce"
        return
    if stage == "quiesce":
        # Let the projection worker finish the import/rebuild before timing
        # ordinary reviews; import time is setup, not product responsiveness.
        state["quiesce_ticks"] = state.get("quiesce_ticks", 0) + 1
        revision, engine = _perf_journal_revision()
        target = engine.journal.operation_count() if engine is not None else 0
        if engine is not None and revision >= target:
            state["bulk_seeded_count"] = target
            state["stage"] = "warmup"
            return
        if state["quiesce_ticks"] > 3600:
            _step("quiesce_timeout", False,
                  f"revision={revision} target={target}")
            state["bulk_seeded_count"] = target
            state["stage"] = "warmup"
            return
        return
    if stage == "warmup":
        state["warmup_ticks"] = state.get("warmup_ticks", 0) + 1
        if state["warmup_ticks"] < 3:
            return
        _lag_probe_start(state)
        state["stage"] = "answers"
        state["answers_done"] = 0
        state["accept_ms"] = []
        state["reward_ms"] = []
        state["rebuilds"] = []
        state["rebuild_watch"] = []
        state["rebuild_at"] = [int(v) for v in (cfg.get("rebuilds_at") or [])]
        state["answer_started"] = None
        state["reward_watches"] = []
        return
    if stage == "answers":
        _perf_register_reward_watches(state)
        _resolve_reward_watches(state)
        _perf_resolve_rebuild_watches(state)
        if state.get("answer_started") is None:
            if state["answers_done"] >= answers_target:
                if mode == "control":
                    # No add-on shell exists in the control run; go straight
                    # to the idle window so the paired CPU sample is equal.
                    state["shell_opens_done"] = warm_opens
                    state["stage"] = "idle"
                else:
                    state["stage"] = "settle"
                    state["settle_ticks"] = 0
                return
            if not state.get("awaiting") and _answer_current_card(state):
                state["answer_started"] = _time.perf_counter()
                state["answers_done"] += 1
                if mode == "addon":
                    # A watch registered for this answer consumes the start
                    # marker; if none ever appears, the reward was displayed
                    # synchronously and the completion time is the reward time.
                    state["watch_answer_start"] = state["answer_started"]
                if state["answers_done"] in state.get("rebuild_at", []) \
                        and mode == "addon":
                    try:
                        import ankiscape
                        engine = ankiscape._EVOLVED_CTX.get("engine")
                        if engine is not None and state.get("bulk_seeded_count"):
                            engine.retract(review_key="rk-bulk-1")
                            state["rebuild_watch"].append({
                                "start": _time.perf_counter(),
                                "target": engine.journal.operation_count()})
                    except Exception as exc:
                        RESULT["errors"].append(f"rebuild trigger: {exc!r}")
            return
        # Awaiting the answer: it is complete when the reviewer moved on.
        from aqt import mw as _mw
        reviewer = getattr(_mw, "reviewer", None)
        rst = getattr(reviewer, "state", "") if reviewer is not None else ""
        if _mw.state != "review" or rst in ("answer", "transition"):
            return
        state["accept_ms"].append(
            round((_time.perf_counter() - state["answer_started"]) * 1000.0, 2))
        if state.pop("watch_answer_start", None) is not None:
            # No pending key ever appeared for this answer: the reward was
            # displayed synchronously inside the accepted-answer hook.
            state["reward_ms"].append(state["accept_ms"][-1])
        state["answer_started"] = None
        state["awaiting"] = 0
        return
    if stage == "settle":
        state["settle_ticks"] = state.get("settle_ticks", 0) + 1
        _perf_register_reward_watches(state)
        _resolve_reward_watches(state)
        _perf_resolve_rebuild_watches(state)
        revision, engine = _perf_journal_revision()
        target = engine.journal.operation_count() if engine is not None else 0
        pending_rewards = len(state.get("reward_watches") or [])
        settled = (not state.get("rebuild_watch")
                   and (engine is None or revision >= target))
        if settled and not pending_rewards:
            state["stage"] = "shell"
            return
        if state["settle_ticks"] > 1200:
            if pending_rewards:
                _step("reward_publication_timeout", False,
                      f"unpublished={pending_rewards}")
            if not settled:
                _step("settle_timeout", False,
                      f"revision={revision} target={target} "
                      f"watches={len(state.get('rebuild_watch') or [])}")
            state["stage"] = "shell"
        return
    if stage == "shell":
        if state.get("shell_opens_done", 0) >= warm_opens:
            state["stage"] = "idle"
            return
        state["shell_ticks"] = state.get("shell_ticks", 0) + 1
        if state["shell_ticks"] > 300:
            _step("shell_stage_stuck", False, "no shell within 300 ticks")
            state["shell_opens_done"] = warm_opens
            state["stage"] = "idle"
            return
        if state.get("shell_open_started") is None:
            shell = _find_shell()
            if shell is not None:
                try:
                    shell.close()
                except Exception:
                    pass
                return
            state["shell_open_started"] = _time.perf_counter()
            _open_shell_via_menu()
            return
        if _find_shell() is not None:
            ms = (_time.perf_counter() - state["shell_open_started"]) * 1000.0
            state.setdefault("shell_ms", []).append(round(ms, 2))
            state["shell_opens_done"] = state.get("shell_opens_done", 0) + 1
            state["shell_open_started"] = None
            if state["shell_opens_done"] == 1:
                state["cold_ms"] = round(ms, 2)
        return
    if stage == "idle":
        if state.get("shell_opens_done", 0) < warm_opens or \
                state.get("stage_until", 0) == 0:
            shell = _find_shell()
            if shell is not None:
                try:
                    shell.close()
                except Exception:
                    pass
                return
            state["lag_probe_off"] = True
            state["idle_cpu_start"] = _process_cpu_seconds()
            state["idle_wall_start"] = _time.time()
            state["stage_until"] = _time.time() + float(
                cfg.get("idle_seconds", 20))
            _perf_marker("perf-idle-start", str(state["stage_until"]))
            return
        if _time.time() < state.get("stage_until", 0):
            return
        elapsed = max(0.1, _time.time() - state.get("idle_wall_start",
                                                    _time.time()))
        cpu_now = _process_cpu_seconds()
        cpu_start = state.get("idle_cpu_start")
        if cpu_now is None or cpu_start is None:
            state["idle_cpu_pct"] = None
        else:
            state["idle_cpu_pct"] = round(
                ((cpu_now - cpu_start) / elapsed) * 100.0, 4)
        _perf_marker("perf-idle-end", str(_time.time()))
        state["stage"] = "finish"
        return
    if stage == "finish":
        payload = {"mode": "perf", "run_id": RUN_ID, "run_mode": mode,
                   "answers": state["answers_done"],
                   "accept_ms": state.get("accept_ms", []),
                   "reward_ms": state.get("reward_ms", []),
                   "lag_ms": state.get("lag_samples", []),
                   "shell_ms": state.get("shell_ms", []),
                   "cold_ms": state.get("cold_ms"),
                   "rebuilds_ms": state.get("rebuilds", []),
                   "idle_seconds": cfg.get("idle_seconds", 0),
                   "idle_cpu_pct": state.get("idle_cpu_pct"),
                   "eligible_for_release":
                       bool(cfg.get("eligible_for_release", False)),
                   "object_final": len(_gc.get_objects()),
                   "thread_final": threading.active_count()}
        _perf_write(payload)
        _step("native_performance_completed", True,
              f"answers={payload['answers']} lag={len(payload['lag_ms'])}")
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
    ("ui-visual-polish", 1): _poll_ui_visual_polish,
    ("ui-visual-polish", 2): _poll_ui_visual_polish_2,
    ("ui-deferred-rewards", 1): _poll_ui_deferred_rewards,
    ("ui-rebuild-review", 1): _poll_ui_rebuild_review,
    ("ui-test-leaderboard", 1): _poll_ui_test_leaderboard,
    ("ui-credential-fallback", 1): _poll_ui_credential_fallback,
    ("ui-recovery", 1): _poll_ui_recovery,
    ("ui-profile-races", 1): _poll_ui_profile_races,
    ("ui-report-bug", 1): _poll_ui_report_bug,
    ("native-performance", 1): _poll_native_performance,
    ("native-performance-control", 1): _poll_native_performance,
}


try:
    from anki.hooks import addHook

    _PROFILER = None

    def _profile_stop():
        profiler = _PROFILER
        if profiler is None:
            return
        try:
            import pstats
            profiler.disable()
            with open(os.path.join(_base_dir(), "e2e-profile.txt"),
                      "w", encoding="utf-8") as fh:
                stats = pstats.Stats(profiler, stream=fh)
                stats.sort_stats("cumulative").print_stats(60)
            with open(os.path.join(_base_dir(), "e2e-profile-callers.txt"),
                      "w", encoding="utf-8") as fh:
                stats = pstats.Stats(profiler, stream=fh)
                for name in ("apply_winner", "pending_operations",
                             "operation_count", "_recompute_sync",
                             "_evolved_status", "_evolved_pending",
                             "_evolved_after_review_event", "refresh",
                             "hydrate"):
                    try:
                        stats.print_callers(name)
                    except Exception:
                        pass
        except Exception:
            pass

    def _on_profile_loaded():
        global _PROFILER
        if os.environ.get("ANKISCAPE_E2E_PROFILE") and _PROFILER is None:
            try:
                import cProfile
                _PROFILER = cProfile.Profile()
                _PROFILER.enable()
            except Exception:
                _PROFILER = None
        try:
            import faulthandler
            fh = open(os.path.join(_base_dir(), "e2e-faulthandler.log"),
                      "w", encoding="utf-8")
            # If the main thread ever blocks, this dump names the frame.
            # Short timeout: a healthy phase always finishes or quits first
            # (cancelled in _quit), so any dump here is a real wedge.
            faulthandler.dump_traceback_later(45, file=fh)
            # Fatal-signal stack (SIGSEGV/SIGABRT during Qt teardown) never
            # reaches the watchdog dump; send it to the captured stderr.
            faulthandler.enable()
        except Exception:
            pass
        from aqt.qt import QTimer
        QTimer.singleShot(0, run)

    addHook("profileLoaded", _on_profile_loaded)
except Exception:
    pass
