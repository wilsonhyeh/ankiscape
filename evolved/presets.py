# evolved/presets.py - Catch-up gathering preset (Qt-free).
"""Desktop-selected gathering preset (Mining/Woodcutting/Fishing, default
Mining): which skill automatically trains on catch-up reviews. Before changing
it, reconcile currently available history under the old preset; the change
itself is a timestamped catchup_preset operation so late-arriving preset
history can correct provisional outcomes deterministically."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict

from .ui.menu_model import validate_preset

PRESET_CONFIG_KEY = "ankiscape_evolved_catchup_preset"


def current_preset(col) -> str:
    try:
        return validate_preset(col.get_config(PRESET_CONFIG_KEY, "mining"))
    except Exception:
        return "mining"


def apply_preset(engine, journal, col, skill: str,
                 now_ts: int = 0) -> Dict[str, Any]:
    """Reconcile history, then record the preset change as an operation."""
    skill = validate_preset(skill)
    now_ts = int(now_ts) or int(time.time())
    try:
        from .catchup import run_catchup
        run_catchup(col, engine, journal, full=False)
    except Exception:
        pass
    op = {"op_id": str(uuid.uuid4()), "game_uuid": engine.cfg.game_uuid,
          "device_id": engine.cfg.device_id,
          "device_seq": journal.allocate_seq(engine.cfg.device_id),
          "lamport": engine.state.lamport + 1, "kind": "catchup_preset",
          "payload": {"skill": skill, "effective_ts": now_ts}}
    engine.state.lamport += 1
    try:
        journal.append_operation(op)
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:200]}
    try:
        col.set_config(PRESET_CONFIG_KEY, skill)
    except Exception:
        pass
    return {"ok": True, "skill": skill, "effective_ts": now_ts}
