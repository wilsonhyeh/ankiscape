# evolved/projection_worker.py - One off-main-thread projection worker per game.
"""Owns projection rebuilds so the accepted-answer path and UI reads never
replay history.

Contract:
  - One worker per loaded game, with its own SQLite connection.
  - At most one running rebuild plus one pending target revision: the wake
    event coalesces to a dirty high-water mark, never a growing op queue.
  - Publishes only completed immutable projections, monotonically by revision.
  - Fast path extends the derived checkpoint by applying only new unique
    direct awards/skips in canonical tail order; anything else (late
    insertions, retracts, catch-ups, presets, rule/schema changes, claim
    conflicts) triggers a full authoritative rebuild in the worker.
  - The last good projection stays published while a rebuild runs; failure is
    reported in status and never replaces the published state.
  - stop() joins with a bounded timeout; the thread owns and closes its
    connection, so a slow rebuild can never block profile close.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from .journal import Journal
from .reducer import (
    build_checkpoint, extend_checkpoint, watermark_tuple,
)

PUBLISH_INTERVAL_S = 0.1  # at most 10 UI publishes/second while busy
CHECKPOINT_EVERY = 100
CHECKPOINT_INTERVAL_S = 10.0


class ProjectionWorker:
    def __init__(self, journal_path: str, config, *,
                 reader_factory: Optional[Callable[[str], Journal]] = None,
                 publish_interval: float = PUBLISH_INTERVAL_S,
                 checkpoint_every: int = CHECKPOINT_EVERY,
                 checkpoint_interval: float = CHECKPOINT_INTERVAL_S):
        self.journal_path = journal_path
        self.cfg = config
        self._reader_factory = reader_factory or (lambda path: Journal(path))
        self._publish_interval = float(publish_interval)
        self._checkpoint_every = int(checkpoint_every)
        self._checkpoint_interval = float(checkpoint_interval)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._latest: Optional[Dict[str, Any]] = None
        self._published_revision = -1
        self._checkpoint: Optional[Dict[str, Any]] = None
        self._last_publish = 0.0
        self._last_checkpoint = 0.0
        self._status: Dict[str, Any] = {
            "revision": 0, "busy": False, "failed": "", "loading": True,
            "published_at": 0.0, "passes": 0}
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "ProjectionWorker":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ankiscape-projection", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> bool:
        """Bounded join; returns True when the thread finished in time."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def notify_dirty(self) -> None:
        """Signal new durable work; coalesces to one pending pass."""
        self._wake.set()

    def set_generation(self, generation: int) -> None:
        # Generation ownership lives with the caller (runtime/profile); the
        # worker itself only publishes pull-visible state, so there is no
        # cross-generation callback to invalidate. Kept for interface parity.
        self._status["generation"] = int(generation)

    # --------------------------------------------------------------- reads

    def latest(self) -> Optional[Dict[str, Any]]:
        return self._latest

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def is_busy(self) -> bool:
        return bool(self.status().get("busy"))

    # ---------------------------------------------------------------- loop

    def _run(self) -> None:
        journal = None
        try:
            journal = self._reader_factory(self.journal_path)
            while not self._stop.is_set():
                self._wake.wait()
                if self._stop.is_set():
                    break
                self._wake.clear()
                self._compute(journal)
        except Exception as exc:  # noqa: BLE001 - status only, never crash Anki
            with self._lock:
                self._status["failed"] = repr(exc)[:300]
                self._status["busy"] = False
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass

    def _compute(self, journal: Journal) -> None:
        with self._lock:
            self._status["busy"] = True
        try:
            checkpoint = self._build(journal)
            now = time.monotonic()
            wait = self._last_publish + self._publish_interval - now
            if wait > 0:
                self._stop.wait(wait)
            state = checkpoint.get("state") or {}
            revision = int(checkpoint.get("revision", 0) or 0)
            with self._lock:
                if revision >= self._published_revision:
                    self._latest = state
                    self._published_revision = revision
                    self._status.update({
                        "revision": revision, "loading": False,
                        "published_at": time.time(), "passes":
                            int(self._status.get("passes", 0)) + 1})
                self._last_publish = time.monotonic()
            self._maybe_save(journal, checkpoint)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status["failed"] = repr(exc)[:300]
        finally:
            with self._lock:
                self._status["busy"] = False

    def _build(self, journal: Journal) -> Dict[str, Any]:
        if self._checkpoint is None:
            self._checkpoint = journal.load_checkpoint(self.cfg.game_uuid)
        checkpoint = self._checkpoint
        if checkpoint:
            watermark = watermark_tuple(checkpoint.get("watermark"))
            if watermark is None and int(checkpoint.get("revision", 0) or 0) == 0:
                watermark = (-1, "", -1, "")
            if watermark is not None and \
                    journal.count_operations_through(watermark) == \
                    int(checkpoint.get("revision", 0) or 0):
                new_ops = journal.operations_after(watermark)
                extended = extend_checkpoint(checkpoint, new_ops,
                                             self.cfg.rules, self.cfg.game_uuid,
                                             key_known=journal.is_known_review_key_before)
                if extended is not None:
                    self._checkpoint = extended
                    return extended
        built = build_checkpoint(journal.all_operations(), self.cfg.rules,
                                 self.cfg.game_uuid)
        self._checkpoint = built
        return built

    def _maybe_save(self, journal: Journal, checkpoint: Dict[str, Any]) -> None:
        revision = int(checkpoint.get("revision", 0) or 0)
        last_saved = int(self._status.get("saved_revision", 0) or 0)
        now = time.monotonic()
        if revision - last_saved < self._checkpoint_every and \
                now - self._last_checkpoint < self._checkpoint_interval:
            return
        try:
            journal.save_checkpoint(self.cfg.game_uuid, revision, checkpoint)
            self._last_checkpoint = now
            with self._lock:
                self._status["saved_revision"] = revision
        except Exception:
            pass  # derived cache: losing it only costs a future rebuild

    def flush_checkpoint(self) -> None:
        """Persist the current checkpoint now (profile close / test helper)."""
        checkpoint = self._checkpoint
        if checkpoint is None:
            return
        journal = None
        try:
            journal = self._reader_factory(self.journal_path)
            journal.save_checkpoint(self.cfg.game_uuid,
                                    int(checkpoint.get("revision", 0) or 0),
                                    checkpoint)
        except Exception:
            pass
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    pass
