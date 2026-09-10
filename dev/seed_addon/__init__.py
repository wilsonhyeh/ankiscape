# dev-only scenario seeder. NEVER SHIPPED (outside the package allowlist,
# installed only into isolated .dev bases by `dev.py launch`). Seeds synthetic
# decks/cards plus Classic or Evolved fixture state through real Anki APIs,
# then records seed-done.json and stays out of the way. Skips E2E profiles
# (the E2E driver seeds those itself) and any base without a seed.json.
import json
import os
import time
import traceback

SEED = None


def _base_dir():
    from aqt import mw
    try:
        base = mw.pm.baseFolder()
        if base:
            return base
    except Exception:
        pass
    return os.path.dirname(mw.pm.profileFolder())


def _note(name, detail):
    try:
        SEED.setdefault("log", []).append({"name": name, "detail": str(detail)[:300]})
    except Exception:
        pass


def run():
    global SEED
    from aqt import mw
    try:
        profile = mw.pm.name
    except Exception:
        return
    if profile.startswith("e2e-"):
        return  # E2E driver owns those profiles
    base = _base_dir()
    seed_path = os.path.join(base, "seed.json")
    if not os.path.exists(seed_path):
        return
    if os.path.exists(os.path.join(base, "seed-done.json")):
        return
    try:
        with open(seed_path, encoding="utf-8") as fh:
            SEED = json.load(fh)
    except (OSError, ValueError) as exc:
        SEED = {"log": [{"name": "seed_read", "detail": repr(exc)[:300]}]}
        _write_done(base)
        return
    if SEED.get("profile") != profile:
        return
    try:
        _apply(mw, base, SEED)
    except Exception:
        _note("seed_exception", traceback.format_exc()[-1500:])
    _write_done(base)
    try:
        from aqt.utils import tooltip
        tooltip(f"[DEV {SEED.get('scenario', '?')}] isolated playground — synthetic data only")
    except Exception:
        pass


def _write_done(base):
    try:
        with open(os.path.join(base, "seed-done.json"), "w", encoding="utf-8") as fh:
            json.dump(SEED, fh, indent=2)
    except Exception:
        pass


def _apply(mw, base, seed):
    from aqt import mw as _mw
    col = mw.col
    scenario = seed.get("scenario", "fresh")
    _seed_cards(col, seed.get("cards", 20))
    if seed.get("classic"):
        _seed_classic(col, seed["classic"])
        _note("classic", f"skill={seed['classic'].get('current_skill')}")
    if seed.get("evolved"):
        _seed_evolved(mw, base, col, seed["evolved"])
        _note("evolved", f"game={seed['evolved'].get('game_uuid', '?')[:8]} "
                         "journal pre-built by dev.py")
    _note("scenario", scenario)


def _seed_cards(col, count):
    model = col.models.by_name("Basic")
    if model is None:
        _note("cards", "no Basic model")
        return
    deck_id = col.decks.id("Dev Deck")
    col.decks.select(deck_id)
    have = len(col.find_cards('deck:"Dev Deck"'))
    for i in range(max(0, count - have)):
        note = col.new_note(model)
        note["Front"] = f"Dev front {i}"
        note["Back"] = f"Dev back {i}"
        col.add_note(note, deck_id)
    _note("cards", f"dev_deck_total={have + max(0, count - have)}")


def _seed_classic(col, classic):
    col.set_config("ankiscape_player_data", classic["player_data"])
    col.set_config("ankiscape_current_skill", classic.get("current_skill", "Mining"))
    col.set_config("ankiscape_mode_requested", "classic")


def _seed_evolved(mw, base, col, evolved):
    # Col config only. The journal file itself is pre-built by dev.py (pure
    # Python, no Anki needed) so profile load stays fast even for endgame.
    game_uuid = evolved["game_uuid"]
    col.set_config("ankiscape_mode_requested", "evolved")
    col.set_config("ankiscape_evolved_player_data",
                   {"version": 1, "game_uuid": game_uuid,
                    "activated_at": evolved.get("activated_at", 0),
                    "snapshot_revision": 0,
                    "preset": {"skill": evolved.get("preset", "mining"),
                               "effective_ts": evolved.get("activated_at", 0)}})
    col.set_config("ankiscape_evolved_device_id", evolved.get("device_id", "dev-seed"))
    for skill, resource in evolved.get("selections", {}).items():
        col.set_config(f"ankiscape_evolved_current_{skill}", resource)
    col.set_config("ankiscape_evolved_catchup_preset", evolved.get("preset", "mining"))


try:
    from anki.hooks import addHook
    addHook("profileLoaded", lambda: run())
except Exception:
    pass
