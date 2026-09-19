# Tests for AnkiScape

- All tests live under `tests/`.
- They use Python's built-in `unittest` framework (no pytest dependency).

## How to run

Use the helper at repo root:

```
python3 run_tests.py
```

It discovers and runs all `tests/test_*.py` files. The script adds the repo root to `PYTHONPATH` so tests can import modules like `logic_pure` directly.

## Debug logging during development

This add-on now uses a centralized, rotating debug log stored next to the package as `ankiscape_debug.log`.
Logging is disabled by default. To enable it temporarily while debugging, set an environment variable before launching Anki or running tests:

- macOS/Linux (zsh): `export ANKISCAPE_DEBUG=1`
- Windows (PowerShell): `$env:ANKISCAPE_DEBUG = '1'`

The logger rotates at ~1 MB with up to 3 backups (`ankiscape_debug.log.1`, etc.).
The log files are git-ignored and should not be shipped in releases.

## What is covered

- Core game logic, level/exp math, and probability helpers
- Storage migration and default data shape guarantees
- Settings helpers and toggles (Experience HUD, floating XP, popups)
- UI progress calculations and boundary conditions
- Hook registration planning (dry-run)
- A no-Qt integration smoke test that dynamically loads the add-on as a package and validates:
	- HUD ensure/update/hide are gated by the Experience HUD toggle
	- Floating XP toasts respect the Floating XP toggle
	- Overview refresh hides the HUD

These tests aim to catch regressions in gating, migrations, and core behavior without requiring a full Anki GUI runtime.

## Reliability gates (3.0)

Beyond the unit suite, the reliability workstream adds:

```bash
python3 dev/reliability.py verify --stage pr
python3 dev/reliability.py verify --stage nightly
python3 dev/reliability.py verify --stage release --evidence artifacts/reliability
python3 dev/reliability.py verify-evidence --stage pr --matrix dev/reliability-matrix.json --evidence artifacts/reliability
python3 dev/perf_runtime.py --profile release
python3 dev/endurance.py --minutes 30
python3 dev/generated_traces.py --stage pr
python3 dev/mutation_gate.py --environment pure
```

- Evidence contract and budgets: `dev/reliability.py`, `dev/reliability-budgets.json`,
  `dev/reliability-matrix.json`; see `docs/RELIABILITY.md`.
- Requirement-to-test mapping: `docs/TEST-MATRIX.md`.
- Native journeys (all run on the packaged artifact): `ui-visual-polish`,
  `ui-deferred-rewards`, `ui-rebuild-review`, `ui-test-leaderboard`,
  `ui-account-lifecycle`, `ui-credential-fallback`, `ui-recovery`,
  `ui-profile-races`, `ui-report-bug`.
- Public demos: `dev/demo_players.py` (plan/apply/verify). The old
  hosted-v1 seeding tools refuse to run.
- Account journeys: `dev/account_journey_e2e.py --local` writes a
  per-target record; `--collect --require-all-targets` aggregates.
