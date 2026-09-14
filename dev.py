#!/usr/bin/env python3
"""dev.py - One-command Anki playground + verification orchestrator.

All generated state goes under gitignored .dev/; reports under artifacts/.
Synthetic scenarios (`fresh`/`midgame`/`endgame`/`classic-upgrade`) run on
the local stack and never touch personal Anki data or production services.
The `user-*` scenarios are deliberate production rehearsals: the packaged
prod artifact in an isolated profile, pointed at the live backend.

Commands:
  python3 dev.py setup
  python3 dev.py launch --scenario fresh
  python3 dev.py launch --scenario user-new      # packaged prod artifact
  python3 dev.py reset --scenario midgame
  python3 dev.py test --suite python
  python3 dev.py test --suite backend
  python3 dev.py test --suite e2e --anki 26.8.1 --qt 6
  python3 dev.py verify --release
  python3 dev.py verify-evidence --matrix dev/matrix.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DEV_DIR = os.path.join(ROOT, ".dev")
ANKI_BASE = os.path.join(DEV_DIR, "anki")
MARKER = ".ankiscape-dev-marker"
ARTIFACTS = os.path.join(ROOT, "artifacts", "verification")

SCENARIOS = ("fresh", "midgame", "endgame", "classic-upgrade")
# Production rehearsals: shared isolated base/profile, packaged prod artifact,
# live backend. Wiped by user-new/user-upgrade; user-resume keeps state.
USER_SCENARIOS = ("user-new", "user-upgrade", "user-resume", "user-test")
USER_BASE = os.path.join(ANKI_BASE, "user")
USER_PROFILE = "dev-user"


def _fail(msg: str, hint: str = "") -> int:
    print(f"dev: ERROR: {msg}", file=sys.stderr)
    if hint:
        print(f"dev: hint: {hint}", file=sys.stderr)
    return 1


def _run(cmd, **kwargs):
    print(f"dev: $ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def cmd_setup(_args) -> int:
    os.makedirs(DEV_DIR, exist_ok=True)
    os.makedirs(ARTIFACTS, exist_ok=True)
    print(f"dev: python {sys.version.split()[0]} at {sys.executable}")
    # Dev-only lockfile install (never end-user deps).
    lock = os.path.join(ROOT, "dev", "requirements.lock")
    if os.path.exists(lock):
        print("dev: dev lockfile present; install with: pip3 install -r dev/requirements.lock")
    else:
        print("dev: no dev/requirements.lock yet (backend task pins it)")
    for tool, probe in (("supabase", ["supabase", "--version"]),
                        ("docker", ["docker", "info"])):
        try:
            proc = subprocess.run(probe, capture_output=True, text=True, timeout=30)
            ok = proc.returncode == 0
            print(f"dev: {tool}: {'ok' if ok else 'UNAVAILABLE'}")
            if not ok:
                print((proc.stderr or proc.stdout or "")[:500])
        except Exception as exc:
            print(f"dev: {tool}: UNAVAILABLE ({exc!r})")
    anki_app = "/Applications/Anki.app"
    print(f"dev: Anki.app: {'present' if os.path.exists(anki_app) else 'MISSING'}")
    print("dev: setup complete. Missing native deps are reported above, not hidden.")
    return 0


def _scenario_dir(scenario: str) -> str:
    return os.path.join(ANKI_BASE, scenario)


def _write_marker(scenario_dir: str, scenario: str) -> None:
    with open(os.path.join(scenario_dir, MARKER), "w", encoding="utf-8") as fh:
        fh.write(f"ankiscape-dev scenario={scenario} created={time.time()}\n")


def _check_marker(scenario_dir: str) -> bool:
    return os.path.isfile(os.path.join(scenario_dir, MARKER))


def _reject_personal_paths(path: str) -> bool:
    """True if path looks like a real Anki base (never delete those)."""
    realpath = os.path.realpath(path)
    home = os.path.expanduser("~")
    try:
        common = os.path.commonpath([realpath, os.path.realpath(DEV_DIR)])
        inside_dev = common == os.path.realpath(DEV_DIR)
    except ValueError:
        inside_dev = False
    if not inside_dev:
        return True
    for personal in (os.path.join(home, "Library", "Application Support", "Anki2"),
                     os.path.join(home, ".local", "share", "Anki2")):
        try:
            if os.path.commonpath([realpath, personal]) == personal:
                return True
        except ValueError:
            pass
    return False


def _norm_version(version: str):
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return (str(version),)


def _matches_version(requested: str, have: str) -> bool:
    want, got = _norm_version(requested), _norm_version(have)
    return len(got) >= len(want) and got[:len(want)] == want


def _pick_app(anki: str):
    """Pick a local Anki.app matching the requested version prefix.

    Returns (app_path, binary_path, installed_version) or ("", "", "?").
    """
    import plistlib

    candidates = ["/Applications/Anki.app", "/Applications/Anki 23.10.app"]
    first = ("", "", "?")
    for cand in candidates:
        exe = os.path.join(cand, "Contents", "MacOS", "Anki")
        if not os.path.exists(exe):
            continue
        try:
            with open(os.path.join(cand, "Contents", "Info.plist"), "rb") as fh:
                ver = plistlib.load(fh).get("CFBundleShortVersionString", "?")
        except OSError:
            ver = "?"
        if first[1] == "":
            first = (cand, exe, ver)
        if anki and _matches_version(anki, ver):
            return cand, exe, ver
    return ("", "", "?") if anki else first


def _refuse_running_anki(context: str) -> bool:
    """True when launch must not proceed. A running Anki would swallow -b/-p
    via single-instance forwarding and open the PERSONAL profile instead."""
    try:
        ps = subprocess.run(["pgrep", "-x", "Anki"], capture_output=True,
                            text=True, timeout=10)
        if (ps.stdout or "").strip():
            print(f"dev: ERROR: an Anki process is already running ({context})",
                  file=sys.stderr)
            print("dev: hint: close your personal Anki first; dev never drives it",
                  file=sys.stderr)
            return True
    except FileNotFoundError:
        pass
    return False


# Parent-interpreter locators that must never reach Anki's bundled Python.
# Leaking these into the child has produced
# "Unable to initialize Python interpreter: can't initialize sys standard
# streams" on macOS in the past. Locale (LANG/LC_*) is preserved.
_SCRUB_ENV_KEYS = ("PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__",
                   "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
                   "CONDA_PYTHON_EXE")


def _anki_env(extra: dict) -> dict:
    """Child env for launching the bundled Anki interpreter."""
    env = dict(os.environ)
    for key in _SCRUB_ENV_KEYS:
        env.pop(key, None)
    env.update(extra)
    return env


def _prod_env() -> dict:
    """Child env for production rehearsals.

    Never let a shell export flip the run into dev/LAN-local mode: with
    these unset, the baked prod endpoint wins exactly like an installed
    release.
    """
    env = _anki_env({})
    for key in ("ANKISCAPE_DEV", "ANKISCAPE_DEV_BACKEND",
                "ANKISCAPE_SUPABASE_URL", "ANKISCAPE_SUPABASE_ANON_KEY"):
        env.pop(key, None)
    return env


def _install_addon(addons_dir: str, *, symlink: bool, prebuilt: bool = False) -> str:
    """Install the packaged add-on (zip, like a user) or a source symlink
    (manual dev loop only). `prebuilt` skips the build and installs the
    current dist/ artifact (the caller already ensured it is current).
    Returns 'zip ...' or 'symlink'."""
    import zipfile

    dest = os.path.join(addons_dir, "ankiscape")
    if os.path.islink(dest) or os.path.exists(dest):
        if os.path.islink(dest):
            os.unlink(dest)
        else:
            shutil.rmtree(dest)
    if symlink:
        os.symlink(ROOT, dest)
        return "symlink"
    if not prebuilt:
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_addon.py")],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"package build failed: {(proc.stderr or proc.stdout)[:400]}")
    import json as _json
    with open(os.path.join(ROOT, "dist", "manifest.json"), encoding="utf-8") as fh:
        record = _json.load(fh)
    with zipfile.ZipFile(os.path.join(ROOT, "dist", record["archive"])) as archive:
        archive.extractall(dest)
    return f"zip {record['archive']} ({record['artifact_sha256'][:12]}...)"


def _resolve_runtime(anki: str, anki_bin: str = "", anki_actual: str = ""):
    """Resolve (app, binary, version) through dev/runtime_adapter.py, keeping
    the macOS Info.plist path and honoring an explicit --anki-bin."""
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location(
        "ankiscape_runtime_adapter", os.path.join(ROOT, "dev", "runtime_adapter.py"))
    adapter = _ilu.module_from_spec(spec)
    # Register before exec: Python 3.11 dataclasses resolve the defining
    # module through sys.modules (TypeError otherwise on @dataclass).
    sys.modules[spec.name] = adapter
    try:
        spec.loader.exec_module(adapter)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    if anki_bin:
        info = adapter.resolve_anki(anki, anki_bin, anki_actual)
        if info is None:
            return "", "", "?"
        ok, detail = adapter.verify_runtime(info, anki)
        if not ok:
            print(f"dev: runtime version check failed: {detail}", file=sys.stderr)
            return info.app, info.binary, f"unverified:{info.version}"
        return info.app or "", info.binary, info.version
    if adapter.host_os() == "macos":
        return _pick_app(anki)
    info = adapter.resolve_anki(anki)
    if info is None:
        return "", "", "?"
    return info.app or "", info.binary, info.version


def cmd_launch(args) -> int:
    scenario = args.scenario
    if scenario not in SCENARIOS + USER_SCENARIOS:
        return _fail(f"unknown scenario {scenario!r}",
                     f"choose from {SCENARIOS + USER_SCENARIOS}")
    if _refuse_running_anki(f"launch --scenario {scenario}"):
        return 1
    _app, anki_bin, installed = _resolve_runtime(
        getattr(args, "anki", ""), getattr(args, "anki_bin", ""))
    if not anki_bin or str(installed).startswith("unverified:"):
        return _fail(f"no verified Anki runtime matches "
                     f"{getattr(args, 'anki', '')!r}",
                     "install the target or pass --anki-bin")
    if scenario in USER_SCENARIOS:
        return cmd_user_journey(args, anki_bin, installed)
    sdir = _scenario_dir(scenario)
    if _reject_personal_paths(sdir):
        return _fail(f"refusing to use path outside .dev: {sdir}")
    if getattr(args, "fresh", False):
        shutil.rmtree(sdir, ignore_errors=True)
        if os.path.exists(sdir):
            return _fail(f"could not wipe {sdir}; a process still holds it")
    os.makedirs(sdir, exist_ok=True)
    _write_marker(sdir, scenario)
    profile = f"dev-{scenario}"
    _ensure_e2e_profile(sdir, profile)
    addons = os.path.join(sdir, "addons21")
    os.makedirs(addons, exist_ok=True)
    try:
        installed_kind = _install_addon(
            addons, symlink=bool(getattr(args, "symlink", False)))
    except RuntimeError as exc:
        return _fail(str(exc))
    # Dev-only seeder (never shipped).
    seed_dest = os.path.join(addons, "seed_addon")
    if os.path.exists(seed_dest):
        shutil.rmtree(seed_dest)
    shutil.copytree(os.path.join(ROOT, "dev", "seed_addon"), seed_dest)
    seed_payload = _scenario_seed(scenario, profile)
    with open(os.path.join(sdir, "seed.json"), "w", encoding="utf-8") as fh:
        json.dump(seed_payload, fh)
    if os.path.exists(os.path.join(sdir, "seed-done.json")):
        os.unlink(os.path.join(sdir, "seed-done.json"))
    if getattr(args, "no_anki", False):
        print(f"dev: scenario '{scenario}' prepared at {sdir} (Anki launch skipped)")
        _print_scenario_summary(seed_payload)
        return 0
    env = _anki_env({"ANKISCAPE_DEV": scenario,
                     "ANKISCAPE_DEV_BACKEND": os.environ.get("ANKISCAPE_DEV_BACKEND", "local")})
    print(f"dev: [DEV {scenario}] Anki {installed} ({installed_kind}) -b {sdir} -p {profile}")
    _print_scenario_summary(seed_payload)
    print("dev: synthetic data only; personal Anki untouched. Close Anki before reset.")
    try:
        proc = subprocess.Popen([anki_bin, "-b", sdir, "-p", profile], env=env)
    except Exception as exc:
        return _fail(f"could not launch Anki: {exc!r}")
    # Surface instant child death (e.g. bundled-interpreter init failure)
    # instead of detaching silently; a healthy Anki stays alive past this.
    try:
        rc = proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        rc = None
    if rc is not None and rc != 0:
        return _fail(f"Anki exited immediately with code {rc}",
                      "any Anki output is above; re-run from a clean shell, "
                      "or reinstall the .app bundle if it persists")
    print(f"dev: Anki pid={proc.pid}")
    return 0


def _ensure_prod_artifact():
    """Make sure dist/ holds the current PROD package; return its manifest.

    Reuses the packaged artifact when shipped bytes are unchanged
    (`build_addon.py --check` is a pure comparison) and rebuilds
    deterministically when they moved. Returns the manifest record, or None
    after printing a failure.
    """
    script = os.path.join(ROOT, "scripts", "build_addon.py")
    check = subprocess.run([sys.executable, script, "--check"],
                           capture_output=True, text=True, timeout=120)
    output = (check.stdout or check.stderr or "").strip()
    if check.returncode == 1:
        print(f"dev: {output}")
        print("dev: rebuilding the prod package from current source...")
        build = subprocess.run([sys.executable, script], capture_output=True,
                               text=True, timeout=300)
        if build.returncode != 0:
            return _fail("prod package build failed",
                         (build.stderr or build.stdout or "")[:400])
        print(f"dev: {(build.stdout or '').strip()}")
    elif check.returncode != 0:
        return _fail(f"prod package check failed: {output}")
    else:
        print(f"dev: {output}")
    try:
        with open(os.path.join(ROOT, "dist", "manifest.json"), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError) as exc:
        return _fail(f"no dist/manifest.json: {exc!r}",
                     "run scripts/build_addon.py")
    if not record.get("prod_endpoint_baked"):
        return _fail("current dist/ artifact is a DEV build (no prod endpoint)",
                     "run scripts/make_prod_config.py --url ... --anon-key ...")
    if not record.get("artifact_sha256"):
        return _fail("dist/manifest.json has no artifact hash; rebuild")
    return record


def _user_seed(scenario: str, profile: str = USER_PROFILE) -> dict:
    """Fixture payload for the production rehearsals.

    Collection content only: a starter deck, plus the 2.0.2-shape Classic
    data for the upgrade journey. No Evolved journal and no local overlay,
    so the packaged add-on behaves exactly as users receive it.
    """
    base = {"scenario": scenario, "profile": profile, "deck": "Sample Deck",
            "seeded_at": time.time(), "summary": []}
    if scenario == "user-new":
        base["cards"] = 20
        base["summary"] = ["20-card Sample Deck so the review loop is testable",
                           "no game state: this is the first-launch experience"]
        return base
    fixtures = _load_fixtures()
    base["cards"] = 100
    base["classic"] = {"player_data": fixtures.classic_player_data(),
                       "current_skill": "Mining"}
    base["summary"] = ["Classic 2.0.2-shape data: Mining 23, Woodcutting 17, "
                       "320 Rune essence banked",
                       "expect the Try Evolved / Continue Classic upgrade path"]
    return base


def _print_user_steps(scenario: str) -> None:
    print("dev: isolated test profile on the production backend; personal Anki "
          "untouched. OTP emails go to whatever address you register.")
    if scenario == "user-new":
        print("dev:   1. Chooser: pick Evolved (or Classic to exercise that path).")
        print("dev:   2. Guided setup -> finish it -> study the Sample Deck.")
        print("dev:   3. Evolved menu -> Account -> register with your real email ->")
        print("dev:      OTP from ankiscape@ankiscape.xyz -> enter the code.")
        print("dev:   4. Review -> XP/HUD -> Sync -> Hiscores. Close and reopen, then")
        print("dev:      pick 'Resume last session' to check the resume path.")
        print("dev: note: each wiped profile needs its own account (an email/username")
        print("dev: registers once); reuse an alias when starting over.")
    elif scenario == "user-upgrade":
        print("dev:   1. Expect the 2.0.2 -> 3.0 upgrade prompt (Try Evolved /")
        print("dev:      Continue Classic).")
        print("dev:   2. Continue Classic keeps Classic progress; Try Evolved starts")
        print("dev:      the Evolved setup fresh.")
        print("dev:   3. Settings -> Advanced switches modes both ways; both games persist.")
        print("dev: note: the Classic state is the 2.0.2-shape fixture, not a real save.")
    else:
        print("dev:   1. State is preserved from the last session; log in again if")
        print("dev:      asked (tokens are memory-only by design).")
        print("dev:   2. 'New user' / 'Upgrade' wipe this profile; Resume never does.")


def cmd_user_journey(args, anki_bin: str, installed: str) -> int:
    """Real-user journeys on the packaged prod artifact + production backend.

    Shared marker-guarded base .dev/anki/user, profile dev-user. user-new and
    user-upgrade wipe it and seed collection fixtures; user-resume relaunches
    it untouched. user-test uses a dedicated base/profile and a permanent
    hosted fixture account (no credentials embedded anywhere). ANKISCAPE_DEV
    is never set, so the add-on resolves its baked production endpoint
    exactly like an installed release.
    """
    scenario = args.scenario
    if getattr(args, "symlink", False):
        return _fail("--symlink is for the synthetic dev playground only",
                     "production rehearsals always install the packaged artifact")
    if scenario == "user-resume" and getattr(args, "fresh", False):
        return _fail("--fresh conflicts with user-resume",
                     "use --scenario user-new to start over")
    if not os.path.isfile(os.path.join(ROOT, "evolved", "prod_config.py")):
        return _fail("evolved/prod_config.py is absent; the launcher needs a "
                     "prod build",
                     "bake it first: python3 scripts/make_prod_config.py --url "
                     "https://<project>.supabase.co --anon-key <public anon key>")
    record = _ensure_prod_artifact()
    if record is None:
        return 1
    wipe = scenario != "user-resume"
    if scenario == "user-test":
        sdir = os.path.join(ANKI_BASE, "user-test")
        profile = "dev-user-test"
    else:
        sdir = USER_BASE
        profile = USER_PROFILE
    if _reject_personal_paths(sdir):
        return _fail(f"refusing to use path outside .dev: {sdir}")
    if wipe:
        if os.path.exists(sdir):
            _reap_mpv(sdir)
            shutil.rmtree(sdir, ignore_errors=True)
        if os.path.exists(sdir):
            return _fail(f"could not wipe {sdir}; close Anki first")
    elif not (os.path.isdir(sdir) and _check_marker(sdir)
              and os.path.isfile(os.path.join(sdir, profile,
                                              "collection.anki2"))):
        return _fail("no production test session to resume",
                     "pick 'New user' first (resume keeps the dev-user profile)")
    os.makedirs(sdir, exist_ok=True)
    _write_marker(sdir, scenario)
    _ensure_e2e_profile(sdir, profile)
    addons = os.path.join(sdir, "addons21")
    os.makedirs(addons, exist_ok=True)
    try:
        installed_kind = _install_addon(addons, symlink=False, prebuilt=True)
    except RuntimeError as exc:
        return _fail(str(exc))
    if wipe:
        # Dev-only seeder (never shipped): starter deck, plus the 2.0.2-shape
        # Classic fixture for the upgrade journey. The packaged add-on itself
        # stays exact release bits.
        seed_dest = os.path.join(addons, "seed_addon")
        if os.path.exists(seed_dest):
            shutil.rmtree(seed_dest)
        shutil.copytree(os.path.join(ROOT, "dev", "seed_addon"), seed_dest)
        seed_payload = _user_seed("user-new" if scenario == "user-test"
                                  else scenario, profile)
        with open(os.path.join(sdir, "seed.json"), "w", encoding="utf-8") as fh:
            json.dump(seed_payload, fh)
        if os.path.exists(os.path.join(sdir, "seed-done.json")):
            os.unlink(os.path.join(sdir, "seed-done.json"))
    else:
        seed_payload = {"summary": []}
    env = _prod_env()
    print(f"dev: [PROD {scenario}] Anki {installed} ({installed_kind}) "
          f"-b {sdir} -p {profile}")
    print(f"dev: artifact {record['artifact_sha256'][:12]}... "
          f"({record['members']} members, prod endpoint baked)")
    _print_scenario_summary(seed_payload)
    _print_user_steps(scenario)
    if scenario == "user-test":
        _print_fixture_account_steps(getattr(args, "fixture", ""))
    if getattr(args, "no_anki", False):
        print(f"dev: scenario '{scenario}' prepared at {sdir} (Anki launch skipped)")
        return 0
    try:
        proc = subprocess.Popen([anki_bin, "-b", sdir, "-p", profile], env=env)
    except Exception as exc:
        return _fail(f"could not launch Anki: {exc!r}")
    try:
        rc = proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        rc = None
    if rc is not None and rc != 0:
        return _fail(f"Anki exited immediately with code {rc}")
    print(f"dev: Anki pid={proc.pid}")
    return 0


def _print_fixture_account_steps(fixture: str) -> None:
    """Show which public demo account to sign in as (never a credential):
    the password derives from ANKISCAPE_FIXTURE_SECRET."""
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "ankiscape_demo_traces",
            os.path.join(ROOT, "dev", "demo_traces.py"))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = list(module.DISPLAY_NAMES)
        name = fixture or names[0]
        print(f"dev:   0. Sign in with demo account {name} "
              f"({module.email_for(name)});")
        print("dev:      derive its password from ANKISCAPE_FIXTURE_SECRET "
              "with dev/demo_players.py's password_for(secret, name).")
        print("dev:      Demo players appear on the public Hiscores labeled "
              "Demo.")
    except Exception as exc:
        print(f"dev:   fixture instructions unavailable: {exc!r}")


def _print_scenario_summary(seed_payload: dict) -> None:
    for line in seed_payload.get("summary", []):
        print(f"dev:   {line}")


def _load_fixtures():
    """Load dev/fixtures.py by path (dev.py shadows the dev/ directory)."""
    import importlib.util

    path = os.path.join(ROOT, "dev", "fixtures.py")
    spec = importlib.util.spec_from_file_location("ankiscape_dev_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenario_seed(scenario: str, profile: str) -> dict:
    """Build the seed.json payload (and pre-built journal files) per scenario."""
    import uuid as _uuid

    base = {"scenario": scenario, "profile": profile, "synthetic": True,
            "seeded_at": time.time(), "summary": []}
    if scenario == "fresh":
        base["cards"] = 20
        base["summary"] = ["empty profile; first load shows the Classic/Evolved chooser",
                           "20 Dev Deck cards ready to review"]
        return base
    if scenario == "classic-upgrade":
        fixtures = _load_fixtures()
        base["cards"] = 100
        base["classic"] = {"player_data": fixtures.classic_player_data(),
                           "current_skill": "Mining"}
        base["summary"] = ["Classic 2.0.2-shape data: Mining 23, Woodcutting 17, "
                           "320 Rune essence banked; chooser on load; Evolved starts fresh"]
        return base
    # Evolved scenarios: deterministic game identity (identical after reset).
    game_uuid = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"ankiscape-dev-{scenario}"))
    activated_at = 1785542400  # 2026-08-01T00:00:00Z fixed baseline
    fixtures = _load_fixtures()
    if scenario == "midgame":
        ops = fixtures.midgame_ops(game_uuid)
        cards, preset = 150, "mining"
        blurb = "midgame"
    else:
        ops = fixtures.endgame_ops(game_uuid)
        cards, preset = 650, "mining"
        blurb = "endgame"
    base["cards"] = cards
    base["evolved"] = {
        "game_uuid": game_uuid, "activated_at": activated_at,
        "device_id": "dev-seed", "preset": preset,
        "selections": {"mining": "Iron ore", "woodcutting": "Oak",
                       "smithing": "Steel bar", "crafting": "Gold ring",
                       "fishing": "Trout", "cooking": "Trout"},
    }
    # Pre-build the journal (pure Python; keeps profile load fast).
    sys.path.insert(0, ROOT)
    try:
        from evolved.journal import Journal, journal_path_for_profile
        from evolved.reducer import replay
        from evolved.data import load_rules
        journal = Journal(journal_path_for_profile(
            os.path.join(_scenario_dir(scenario), profile), game_uuid))
        try:
            observations = [
                {"review_key": op["payload"]["review_key"],
                 "revlog_id": 1000000 + i, "card_id": 2000000 + i,
                 "fingerprint": op["payload"]["review_key"]}
                for i, op in enumerate(ops)]
            counts = journal.import_game(
                game_uuid, {"operations": ops, "observations": observations})
        finally:
            journal.close()
        rules = load_rules()
        state = replay(ops, rules, game_uuid)
        levels = " ".join(f"{skill[:4]}:{level}"
                          for skill, level in state["levels"].items())
        base["summary"] = [
            f"{blurb}: journal {len(ops)} ops ({counts['operations']} new this run), "
            f"total level {state['total_level']} ({levels})",
            f"{len(state['achievements'])} achievements, "
            f"{sum(1 for _v in state['inventory'].values() if _v)} banked items, "
            f"{cards} Dev Deck cards",
        ]
    finally:
        try:
            sys.path.remove(ROOT)
        except ValueError:
            pass
    return base


def cmd_reset(args) -> int:
    scenario = args.scenario
    if scenario == "user-test":
        sdir = os.path.join(ANKI_BASE, "user-test")
    elif scenario in USER_SCENARIOS:
        sdir = USER_BASE
    else:
        sdir = _scenario_dir(scenario)
    real = os.path.realpath(sdir)
    if os.path.islink(sdir) or (os.path.exists(sdir) and os.path.realpath(sdir) != os.path.abspath(sdir)
                                and False):
        return _fail("symlink escape rejected")
    if _reject_personal_paths(sdir):
        return _fail(f"refusing to reset outside .dev: {sdir}")
    if not _check_marker(sdir):
        return _fail(f"no dev marker in {sdir}; refusing to delete",
                     "only reset scenarios created by dev.py launch")
    lock = os.path.join(sdir, "collection.anki2-journal")
    if os.path.exists(lock):
        return _fail("collection looks open/locked; close Anki first")
    # Refuse if Anki seems to run with this base (owned-process check only).
    try:
        ps = subprocess.run(["pgrep", "-af", "Anki"], capture_output=True, text=True, timeout=10)
        if real in (ps.stdout or ""):
            return _fail("an Anki process references this base; close it first")
    except FileNotFoundError:
        pass
    shutil.rmtree(sdir, ignore_errors=False)
    if scenario == "user-test":
        print(f"dev: test-fixture rehearsal profile removed ({sdir})")
        return 0
    if scenario in USER_SCENARIOS:
        print(f"dev: production rehearsal profile removed ({sdir})")
        return 0
    os.makedirs(sdir, exist_ok=True)
    _write_marker(sdir, scenario)
    print(f"dev: scenario '{scenario}' reset to identical seed state")
    return 0


def cmd_test(args) -> int:
    suite = args.suite
    if suite == "python":
        proc = _run([sys.executable, os.path.join(ROOT, "run_tests.py")])
        return proc.returncode
    if suite == "backend":
        return _backend_suite()
    if suite == "e2e":
        return _e2e_suite(getattr(args, "anki", ""), getattr(args, "qt", ""),
                          getattr(args, "journey", "fresh"),
                          getattr(args, "anki_bin", ""),
                          getattr(args, "anki_actual", ""))
    return _fail(f"unknown suite {suite!r}")


def _backend_suite() -> int:
    # Real local Supabase stack required; missing Docker is failure, not skip.
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    if info.returncode != 0:
        return _fail("Docker daemon unavailable; backend suite cannot run",
                     "start Docker Desktop safely (no factory reset) and retry; "
                     f"docker info: {(info.stderr or info.stdout)[:300]}")
    server_dir = os.path.join(ROOT, "server")
    # Clean migration reset twice (idempotency), then pgTAP, then live smoke.
    for i in (1, 2):
        proc = subprocess.run(["supabase", "db", "reset"], cwd=server_dir, timeout=300)
        if proc.returncode != 0:
            print(f"dev: (backend) db reset #{i} FAILED", file=sys.stderr)
            return 1
    proc = subprocess.run(["supabase", "test", "db"], cwd=server_dir,
                          timeout=300, capture_output=True, text=True)
    tap_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    print(tap_text.rstrip())
    if proc.returncode != 0:
        print("dev: (backend) pgTAP FAILED", file=sys.stderr)
        return 1
    tap_results = re.findall(r"^(ok|not ok)\b", tap_text, re.MULTILINE)
    pgtap_passed = tap_results.count("ok")
    pgtap_failed = tap_results.count("not ok")
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "dev", "auth_smoke.py")],
                          cwd=ROOT, timeout=600, capture_output=True, text=True)
    smoke_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    print(smoke_text.rstrip())
    if proc.returncode != 0:
        print("dev: (backend) auth smoke FAILED", file=sys.stderr)
        return 1
    # Machine-readable totals for the reliability record (nonzero real test
    # counts; pgTAP emits TAP, the smoke prints one PASS line per check).
    smoke_passed = len(re.findall(r"^PASS\b", smoke_text, re.MULTILINE))
    smoke_failed = len(re.findall(r"^FAIL\b", smoke_text, re.MULTILINE))
    print(f"dev: (backend) counts: passed={pgtap_passed + smoke_passed} "
          f"failed={pgtap_failed + smoke_failed} skipped=0 "
          f"(pgtap={pgtap_passed} smoke={smoke_passed})")
    print("dev: (backend) reset x2 + pgTAP + live Auth/RPC smoke PASS")
    return 0


def _ensure_e2e_profile(base: str, profile: str) -> None:
    """Pre-create an Anki profile so -p loads it directly.

    Replicates ProfileManager.create() (Anki 26.08.1, qt/aqt/profiles.py):
    insert pickle-protocol-4 of the default profileConf dict into the
    prefs21.db profiles table. All values are plain types, so Anki's own
    unpickler reads it without Qt involvement. Anki will NOT auto-create a
    -p profile (it shows the profile manager instead), which would strand
    the driver before profileLoaded ever fires.
    """
    import pickle
    import random
    import sqlite3

    prof = {"mainWindowGeom": None, "mainWindowState": None, "numBackups": 50,
            "lastOptimize": int(time.time()), "searchHistory": [],
            "syncKey": None, "syncMedia": True, "autoSync": True,
            "allowHTML": False, "importMode": 1, "lastColour": "#00f",
            "stripHTML": True, "deleteMedia": False}
    # A pre-existing prefs21.db WITHOUT a _global row makes _loadMeta take
    # the corrupt-recovery path (deleting our profile row), so seed _global
    # with the default metaConf shape too.
    meta = {"ver": 0, "updates": False, "created": int(time.time()),
            "id": random.randrange(0, 2 ** 63), "lastMsg": 0,
            "suppressUpdate": False, "firstRun": False, "defaultLang": "en_US"}
    db_path = os.path.join(base, "prefs21.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("create table if not exists profiles"
                     " (name text primary key collate nocase, data blob not null)")
        conn.execute("insert or ignore into profiles values (?, ?)",
                     (profile, pickle.dumps(prof, protocol=4)))
        conn.execute("insert or ignore into profiles values ('_global', ?)",
                     (pickle.dumps(meta, protocol=4),))
        conn.commit()
    finally:
        conn.close()


def _reap_mpv(base: str) -> None:
    """Reap orphaned Anki audio (mpv) children for one dev base.

    mpv helpers outlive Anki and hold base files; on macOS they can also spin
    and drive system load until every later Anki launch fails. Normal Anki
    exits (SIGTERM is ignored; assertions then quit) leave them behind, so
    every journey must reap its own before and after running.
    """
    try:
        ps = subprocess.run(["pgrep", "-af", "anki_audio/mpv"], capture_output=True,
                            text=True, timeout=10)
        for line in (ps.stdout or "").splitlines():
            if base in line:
                try:
                    pid = int(line.split()[0])
                    os.kill(pid, 9)
                except (ValueError, OSError):
                    pass
    except (FileNotFoundError, OSError):
        pass


def _kill_owned_child(child, base: str) -> None:
    """Stop the Anki process THIS invocation launched. Anki ignores SIGTERM,
    so escalate to SIGKILL and verify death. Also reaps its orphaned mpv
    audio children (they outlive Anki and hold base files). Never touches
    other processes: mpv orphans match only via our base in --config-dir."""
    try:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"dev: WARNING: owned Anki pid={child.pid} survived SIGKILL; "
                      f"base {base} may be locked", file=sys.stderr)
    except OSError:
        pass
    _reap_mpv(base)


def _e2e_suite(anki: str, qt: str, journey: str = "fresh",
               anki_bin_override: str = "",
               anki_actual: str = "",
               install_addon: bool = True,
               extra_config: dict = None,
               phase_timeout_s: int = 240) -> int:
    import zipfile

    journeys = ("fresh", "upgrade", "undo", "catchup", "sync", "dialogs",
                "ui-onboarding", "ui-training", "ui-settings", "ui-review",
                "ui-lifecycle", "ui-art", "ui-visual-polish",
                "ui-deferred-rewards", "ui-rebuild-review",
                "ui-test-leaderboard", "ui-account-lifecycle",
                "ui-credential-fallback",
                "ui-recovery", "ui-profile-races", "ui-report-bug",
                "native-performance", "native-performance-control")
    if journey not in journeys:
        return _fail(f"unknown journey {journey!r}", f"choose from {journeys}")
    print(f"dev: (e2e) journey={journey} target Anki={anki or '?'} Qt={qt or '?'}")
    if journey == "sync":
        # Live local-stack journey (needs Docker + Supabase): serviced sync
        # against the real local Auth/RPCs through the new service layer.
        return _e2e_sync_suite(anki)
    _app, anki_bin, installed = _resolve_runtime(anki, anki_bin_override,
                                                 anki_actual)
    if not anki_bin or str(installed).startswith("unverified:"):
        if anki:
            return _fail(f"no verified Anki runtime matches {anki}",
                         "install the target (e.g. Anki 23.10 alongside) or "
                         "pass --anki-bin")
        return _fail("Anki runtime not found")
    # Single-instance safety: a running Anki would swallow our launch args
    # and open the PERSONAL profile instead. Refuse outright.
    if _refuse_running_anki("e2e"):
        return 1
    # Build the exact artifact under test (installed from zip, not symlink).
    # Reuse a candidate whose bytes already match this source (CI consumers
    # must never rebuild the artifact they were handed); rebuild only when
    # --check reports stale.
    check = subprocess.run([sys.executable, os.path.join(ROOT, "scripts",
                                                         "build_addon.py"),
                            "--check"], capture_output=True, text=True)
    if check.returncode == 1:
        # Consumer lanes of a distributed candidate must never rebuild: the
        # evidence contract binds results to the handed-over bytes. Local
        # development without that lock still self-builds.
        if os.environ.get("ANKISCAPE_CANDIDATE_LOCKED"):
            return _fail("dist candidate does not match this checkout",
                         (check.stderr or "").strip()[:300]
                         + " (ANKISCAPE_CANDIDATE_LOCKED: consumers never "
                           "rebuild the artifact)")
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_addon.py")])
        if proc.returncode != 0:
            return 1
    elif check.returncode != 0:
        print(f"dev: build --check failed: {check.stderr[:300]}", file=sys.stderr)
        return 1
    import json as _json
    with open(os.path.join(ROOT, "dist", "manifest.json"), encoding="utf-8") as fh:
        build_record = _json.load(fh)
    artifact = os.path.join(ROOT, "dist", build_record["archive"])
    artifact_sha = build_record["artifact_sha256"]

    base = os.path.join(DEV_DIR, "e2e", journey)
    if _reject_personal_paths(base):
        return _fail(f"refusing e2e base outside .dev: {base}")
    # Anki ignores SIGTERM: a leftover from a killed run would survive and
    # lock the base, silently breaking rmtree (ignore_errors) and causing a
    # second instance to open a half-wiped tree. Refuse instead of guessing.
    try:
        ps = subprocess.run(["pgrep", "-x", "Anki"], capture_output=True,
                            text=True, timeout=10)
        if (ps.stdout or "").strip():
            return _fail("an Anki process is already running (leftover or personal)",
                         "close personal Anki; kill only E2E leftovers you own, "
                         "then retry - e2e never drives a foreign process")
    except FileNotFoundError:
        pass
    run_id = f"{journey}-{int(time.time())}"
    _reap_mpv(base)  # stale audio helpers from a previous run hold the base
    shutil.rmtree(base, ignore_errors=True)
    if os.path.exists(base):
        return _fail(f"could not wipe {base}; a process still holds it")
    os.makedirs(base, exist_ok=True)
    _write_marker(base, f"e2e-{journey}")
    with open(os.path.join(base, "run-id.txt"), "w", encoding="utf-8") as fh:
        fh.write(run_id)
    addons = os.path.join(base, "addons21")
    os.makedirs(addons, exist_ok=True)
    if install_addon:
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(os.path.join(addons, "ankiscape"))
    # Dev-only driver (never shipped: outside the package allowlist).
    shutil.copytree(os.path.join(ROOT, "dev", "e2e", "driver_addon"),
                    os.path.join(addons, "e2e_driver"))
    with open(os.path.join(base, "artifact_sha256.txt"), "w", encoding="utf-8") as fh:
        fh.write(artifact_sha)
    if extra_config:
        with open(os.path.join(base, "perf-config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(extra_config, fh)

    profile = f"e2e-{journey}"
    # Anki will not auto-create a -p profile (profile manager instead), so
    # pre-create it exactly like ProfileManager.create() does.
    _ensure_e2e_profile(base, profile)
    with open(os.path.join(base, "journey.json"), "w", encoding="utf-8") as fh:
        json.dump({"journey": journey, "phase": 1, "run_id": run_id}, fh)
    if journey == "upgrade":
        # Classic 2.0.2-shape fixture for the driver's phase-1 seeding.
        fixtures = _load_fixtures()
        with open(os.path.join(base, "classic-fixture.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"player_data": fixtures.classic_player_data(),
                       "current_skill": "Mining"}, fh)
    if journey in ("ui-art", "ui-visual-polish"):
        # High-level / full-bank world for offline art rendering (same
        # generator as the synthetic endgame scenario). The driver swaps the
        # placeholder uuid for the game it created during onboarding.
        fixtures = _load_fixtures()
        with open(os.path.join(base, "art-fixture.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"operations": fixtures.endgame_ops("ART_PLACEHOLDER"),
                       "observations": []}, fh)
    log_path = os.path.join(base, "anki-stdout.log")
    env = _anki_env({"ANKISCAPE_DEBUG": "1"})

    phase = 1
    last_result = None
    while phase <= 4:
        print(f"dev: (e2e) launching {installed} phase={phase} -b {base} -p {profile}")
        with open(log_path, "a", encoding="utf-8") as log:
            try:
                child = subprocess.Popen([anki_bin, "-b", base, "-p", profile],
                                         stdout=log, stderr=subprocess.STDOUT, env=env)
            except OSError as exc:
                return _fail(f"could not launch Anki: {exc!r}")
        outcome = _wait_e2e_phase(child, base, run_id, log_path,
                                  timeout_s=phase_timeout_s)
        if outcome == "timeout":
            return 1
        if outcome == "relaunch":
            try:
                with open(os.path.join(base, "relaunch.json"), encoding="utf-8") as fh:
                    request = _json.load(fh)
            except (OSError, ValueError):
                return _fail("relaunch requested but relaunch.json unreadable")
            if request.get("run_id") != run_id:
                return _fail("stale relaunch.json (wrong run_id)")
            if request.get("ok") is False:
                failed = request.get("failed_steps") or ["(unrecorded)"]
                print("dev: (e2e) intermediate phase failed steps: "
                      f"{failed}", file=sys.stderr)
                return 1
            try:
                os.unlink(os.path.join(base, "relaunch.json"))
            except OSError:
                pass
            phase = int(request.get("next_phase", phase + 1))
            with open(os.path.join(base, "journey.json"), "w", encoding="utf-8") as fh:
                _json.dump({"journey": journey, "phase": phase, "run_id": run_id}, fh)
            continue
        # Final assertions.
        try:
            with open(os.path.join(base, "e2e-assertions.json"),
                      encoding="utf-8") as fh:
                last_result = _json.load(fh)
        except (OSError, ValueError):
            # Early Anki exit (e.g. an orphaned audio helper blocking launch):
            # still reap this base's mpv children so they cannot accumulate
            # and break every later journey.
            _reap_mpv(base)
            return _fail("e2e exited without final assertions")
        break
    if last_result is None:
        _reap_mpv(base)
        return _fail("e2e ended with no final assertions")
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _kill_owned_child(child, base)
        return _fail("e2e wrote assertions but Anki did not exit")
    _reap_mpv(base)
    steps = last_result.get("steps", [])
    failed = [s for s in steps if not s.get("ok")]
    print(f"dev: (e2e) journey={last_result.get('journey')} steps={len(steps)} "
          f"failed={len(failed)} screenshots={len(last_result.get('screenshots', []))}")
    for step in failed:
        print(f"dev: (e2e) FAILED step: {step}", file=sys.stderr)
    if child.returncode != 0:
        print(f"dev: (e2e) Anki exit code {child.returncode}", file=sys.stderr)
    perf_journey = journey in ("native-performance",
                               "native-performance-control")
    if (failed or child.returncode != 0
            or (not perf_journey and not last_result.get("screenshots"))
            or last_result.get("journey") != journey):
        return 1
    print(f"dev: (e2e) {journey} journey PASS on Anki {installed} "
          f"(artifact {artifact_sha[:16]}...)")
    return 0


def _wait_e2e_phase(child, base: str, run_id: str, log_path: str,
                    timeout_s: int = 240) -> str:
    """Wait for final assertions ('done'), a relaunch request ('relaunch'),
    or failure ('timeout')."""
    import json as _json

    assertions = os.path.join(base, "e2e-assertions.json")
    relaunch = os.path.join(base, "relaunch.json")
    deadline = time.time() + max(30, int(timeout_s))
    while time.time() < deadline:
        if os.path.exists(assertions):
            try:
                with open(assertions, encoding="utf-8") as _fh:
                    if _json.load(_fh).get("run_id") == run_id:
                        return "done"
            except (OSError, ValueError):
                pass
        if os.path.exists(relaunch):
            try:
                with open(relaunch, encoding="utf-8") as _fh:
                    if _json.load(_fh).get("run_id") == run_id:
                        _kill_owned_child(child, base)
                        return "relaunch"
            except (OSError, ValueError):
                pass
        if child.poll() is not None:
            break  # exited without assertions -> diagnose from log
        time.sleep(2)
    _kill_owned_child(child, base)
    print(f"dev: (e2e) no phase output; Anki log tail:", file=sys.stderr)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            print("".join(fh.readlines()[-30:]), file=sys.stderr)
    except OSError:
        pass
    print("dev: ERROR: e2e produced no phase output (crash, forwarding, or hang)",
          file=sys.stderr)
    return "timeout"


def _e2e_sync_suite(anki: str) -> int:
    """Online/recovery journey against the REAL local stack (no mocks).

    Exercises the new service layer end to end through SyncJob.run_once:
    signup -> verify -> link -> offline credit -> upload -> second-device
    download/merge -> conflict quarantine -> lost-ack retry. Uses the real
    local Supabase Auth/RPCs on the alternate-port stack; never touches
    production, real email, or personal Anki data.
    """
    import importlib.util as _ilu
    import uuid as _uuid

    info = subprocess.run(["docker", "info"], capture_output=True,
                          text=True, timeout=30)
    if info.returncode != 0:
        return _fail("Docker daemon unavailable; sync journey cannot run",
                      "start Docker Desktop safely (no factory reset) and retry")
    path = os.path.join(ROOT, "dev", "sync_e2e.py")
    spec = _ilu.spec_from_file_location("ankiscape_sync_e2e", path)
    module = _ilu.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        return _fail(f"could not load dev/sync_e2e.py: {exc!r}")
    try:
        rc = module.main()
    except Exception as exc:
        print(f"dev: (e2e) sync journey raised {exc!r}", file=sys.stderr)
        return 1
    if rc != 0:
        return 1
    _app, _bin, installed = _pick_app(anki)
    print(f"dev: (e2e) sync journey PASS (local stack, Anki {installed} present)")
    return 0


def cmd_verify(args) -> int:
    release = getattr(args, "release", False)
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = os.path.join(ARTIFACTS, run_id)
    os.makedirs(outdir, exist_ok=True)
    results: dict = {"run_id": run_id, "release": release, "steps": {}}

    def _step(name, fn):
        try:
            rc = fn()
        except Exception as exc:
            rc = 1
            print(f"dev: step {name} raised {exc!r}")
        results["steps"][name] = {"returncode": rc}
        return rc

    overall = 0
    overall |= _step("python", lambda: subprocess.run(
        [sys.executable, os.path.join(ROOT, "run_tests.py")]).returncode)
    overall |= _step("package", lambda: subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "build_addon.py",
                                      )]).returncode if os.path.exists(
            os.path.join(ROOT, "scripts", "build_addon.py")) else 1)
    if release:
        overall |= _step("backend", _backend_suite)
        overall |= _step("e2e", lambda: _e2e_suite("", ""))
        # Release readiness delegates to the reliability orchestrator: the
        # seven-target evidence must validate before this returns success.
        overall |= _step("reliability", lambda: subprocess.run(
            [sys.executable, os.path.join(ROOT, "dev", "reliability.py"),
             "verify", "--stage", "release", "--evidence",
             os.path.join(ROOT, "artifacts", "reliability")]).returncode)
    results["overall"] = overall
    results["note"] = ("Quick subsets are not release readiness. "
                       "verify --release requires all steps incl. backend/e2e/"
                       "evidence aggregation via dev/reliability.py.")
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"dev: verification {'PASS' if overall == 0 else 'FAIL'}; summary at {outdir}/summary.json")
    return overall


def cmd_verify_evidence(args) -> int:
    """Validate release evidence; delegates to the reliability orchestrator.

    The orchestrator checks: non-empty matrix, one resolvable artifact from
    dist/manifest.json (never a first glob), record schema/completeness,
    interrupted runs, version/hash mismatches, stale or copied results,
    fabricated evidence paths, file hashes and run windows, omitted or
    skipped required scenarios, zero-test reports and budget violations.
    """
    evidence = getattr(args, "evidence", None) or os.path.join(ROOT, "artifacts", "reliability")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "dev", "reliability.py"),
         "verify-evidence", "--matrix", args.matrix, "--evidence", evidence,
         "--stage", getattr(args, "stage", "release")],
        cwd=ROOT)
    return proc.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dev.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    p_launch = sub.add_parser("launch")
    p_launch.add_argument("--scenario", required=True,
                          choices=SCENARIOS + USER_SCENARIOS,
                          help="user-new/user-upgrade/user-resume: packaged prod "
                               "artifact on the production backend (isolated "
                               "profile; auto rebuild if source changed)")
    p_launch.add_argument("--anki", default="")
    p_launch.add_argument("--anki-bin", default="",
                          help="explicit runtime executable (native matrix)")
    p_launch.add_argument("--symlink", action="store_true",
                          help="link the repo instead of installing the zip (dev loop only)")
    p_launch.add_argument("--fresh", action="store_true",
                          help="wipe the scenario base first (verifies the wipe)")
    p_launch.add_argument("--fixture", default="",
                          help="user-test demo display name "
                               "(default: first public-demo account)")
    p_launch.add_argument("--no-anki", action="store_true")
    p_reset = sub.add_parser("reset")
    p_reset.add_argument("--scenario", required=True,
                         choices=SCENARIOS + USER_SCENARIOS)
    p_test = sub.add_parser("test")
    p_test.add_argument("--suite", required=True, choices=("python", "backend", "e2e"))
    p_test.add_argument("--anki", default="")
    p_test.add_argument("--anki-bin", default="",
                        help="explicit runtime executable (native matrix)")
    p_test.add_argument("--anki-actual", default="",
                        help="declared version for hash-verified downloads "
                             "that cannot be probed")
    p_test.add_argument("--qt", default="")
    p_test.add_argument("--journey", default="fresh",
                        choices=("fresh", "upgrade", "undo", "catchup", "sync",
                                 "dialogs", "ui-onboarding", "ui-training",
                                 "ui-settings", "ui-review", "ui-lifecycle",
                                 "ui-art", "ui-visual-polish",
                                 "ui-deferred-rewards", "ui-rebuild-review",
                                 "ui-test-leaderboard", "ui-account-lifecycle",
                                 "ui-credential-fallback", "ui-recovery",
                                 "ui-profile-races", "ui-report-bug"))
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--release", action="store_true")
    p_ev = sub.add_parser("verify-evidence")
    p_ev.add_argument("--matrix", default=os.path.join(ROOT, "dev", "matrix.json"))
    p_ev.add_argument("--evidence",
                      default=os.path.join(ROOT, "artifacts", "reliability"))
    p_ev.add_argument("--stage", default="release",
                      choices=("pr", "nightly", "release"))
    args = parser.parse_args(argv)
    if args.cmd == "setup":
        return cmd_setup(args)
    if args.cmd == "launch":
        return cmd_launch(args)
    if args.cmd == "reset":
        return cmd_reset(args)
    if args.cmd == "test":
        return cmd_test(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "verify-evidence":
        return cmd_verify_evidence(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
