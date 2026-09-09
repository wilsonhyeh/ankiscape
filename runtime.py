# runtime.py - One active adapter per profile (Qt-free core).
"""Profile generation + adapter dispatch (contract A).

Root initialization installs dispatch hooks once without reading a collection.
This module owns:
  - profile generation (invalidated on profile close before releasing widgets)
  - active adapter (ClassicAdapter or EvolvedAdapter)
  - requested mode (what the next start should use)

Every review/overview/deck-browser/JS/menu callback routes through the
Runtime object. No active profile means no game action.

Background results must match both profile generation and authenticated user
before applying. Network workers receive immutable payloads only.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

try:
    from . import mode as _mode  # Anki add-on package import
except ImportError:  # run_tests.py top-level import
    import mode as _mode  # type: ignore[no-redef]

_generation_counter = itertools.count(1)


class BaseAdapter:
    name = "base"

    def on_profile_load(self, ctx: Dict[str, Any]) -> None:
        pass

    def on_profile_close(self, ctx: Dict[str, Any]) -> None:
        pass

    def on_review_answer(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None

    def on_menu(self, ctx: Dict[str, Any]):
        return None

    def owner_label(self) -> str:
        return self.name


class ClassicAdapter(BaseAdapter):
    """Invokes legacy gameplay via injected callbacks (keeps formulas frozen)."""

    name = "classic"

    def __init__(self, legacy: Optional[Dict[str, Callable]] = None):
        self.legacy = dict(legacy or {})

    def on_review_answer(self, ctx):
        fn = self.legacy.get("on_review_answer")
        if fn is None:
            return None
        return fn(ctx)

    def on_menu(self, ctx):
        fn = self.legacy.get("on_menu")
        if fn is None:
            return None
        return fn(ctx)


class EvolvedAdapter(BaseAdapter):
    """Owns Evolved state, menu, HUD, journal and jobs for one profile."""

    name = "evolved"

    def __init__(self, game_uuid: Optional[str] = None, journal_path: Optional[str] = None):
        self.game_uuid = game_uuid
        self.journal_path = journal_path
        self.user_id: Optional[str] = None
        self._closed = False

    def bind_user(self, user_id: Optional[str]) -> None:
        self.user_id = user_id

    def check_result(self, generation: int, user_id: Optional[str], current_generation: int) -> bool:
        """A background result applies only if generation + user match."""
        if self._closed:
            return False
        if generation != current_generation:
            return False
        return (self.user_id or None) == (user_id or None)

    def on_profile_close(self, ctx=None) -> None:
        # Clear credentials; release widgets via ctx callbacks if provided.
        self.user_id = None
        self._closed = True
        if isinstance(ctx, dict):
            release = ctx.get("release_widgets")
            if callable(release):
                try:
                    release()
                except Exception:
                    pass


@dataclass
class Runtime:
    generation: int = 0
    requested_mode: str = _mode.CLASSIC
    active_adapter: BaseAdapter = field(default_factory=ClassicAdapter)
    profile_loaded: bool = False

    def begin_profile(self, requested_mode: str, adapter: Optional[BaseAdapter] = None) -> int:
        self.generation = next(_generation_counter)
        self.requested_mode = _mode.normalize_requested(requested_mode)
        if adapter is not None:
            self.active_adapter = adapter
        else:
            self.active_adapter = (
                EvolvedAdapter() if self.requested_mode == _mode.EVOLVED else ClassicAdapter()
            )
        self.profile_loaded = True
        try:
            self.active_adapter.on_profile_load({})
        except Exception:
            pass
        return self.generation

    def end_profile(self) -> int:
        """Invalidate generation BEFORE releasing widgets (contract A)."""
        old = self.generation
        self.generation = 0
        self.profile_loaded = False
        try:
            self.active_adapter.on_profile_close({})
        except Exception:
            pass
        self.active_adapter = ClassicAdapter()
        return old

    def route_review_answer(self, ctx: Dict[str, Any]):
        if not self.profile_loaded:
            return None
        try:
            return self.active_adapter.on_review_answer(ctx)
        except Exception as exc:
            # Game exceptions must not break scheduling; surface diagnostics.
            return {"ok": False, "error": repr(exc), "diagnostics": True}

    def is_result_fresh(self, generation: int, user_id: Optional[str]) -> bool:
        if not self.profile_loaded:
            return False
        adapter = self.active_adapter
        if isinstance(adapter, EvolvedAdapter):
            return adapter.check_result(generation, user_id, self.generation)
        return generation == self.generation


_RUNTIME = Runtime()


def get_runtime() -> Runtime:
    return _RUNTIME


def reset_runtime_for_tests() -> Runtime:
    global _RUNTIME
    _RUNTIME = Runtime()
    return _RUNTIME
