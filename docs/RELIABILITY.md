# Reliability operations

One-command recipes, prerequisites, triage and ownership for the reliability
gates. See `docs/TEST-MATRIX.md` for the requirement-to-test mapping.

## Stages and commands

| Stage | Command | What runs | Expected duration |
|---|---|---|---|
| pr | `python3 dev/reliability.py verify --stage pr` | Pure suite, generated PR traces, asset/package audit; Linux backend + one native smoke in CI | ~1 min local / ~15 min CI (excluding first runtime download) |
| nightly | `python3 dev/reliability.py verify --stage nightly` | Adds seven-target native matrix, expanded generated traces, 30-min endurance, performance metrics, trusted hosted fixture smoke | ~4-6 h CI |
| release | `python3 dev/reliability.py verify --stage release --evidence artifacts/reliability` | Consumes validated lane evidence + mutation gate; all seven targets required | ~6-8 h CI |
| evidence only | `python3 dev/reliability.py verify-evidence --matrix dev/reliability-matrix.json --evidence artifacts/reliability` | Validates provenance/hashes/scenarios/budgets without executing | seconds |
| one lane | `python3 dev/reliability.py run-lane --stage nightly --os linux --anki 26.8.1 --qt 6 --anki-bin <path> --out artifacts/reliability/<run>/<lane>` | Executes this lane's scenarios and writes `record.json` | 10-90 min |
| local | `python3 dev/reliability.py verify --stage pr` on macOS | Runs what this machine can and fails naming `.github/workflows/pr.yml` for the rest — it never pretends to run Windows on a Mac | ~1 min |

Baseline capture (already recorded at `artifacts/reliability/baseline/`, do not
overwrite its date): `python3 dev/reliability.py baseline`.

## Prerequisites

- Python 3.9+ for local gates; CI uses 3.11. Dev-only pins: `dev/requirements.lock`
  (supabase CLI, hypothesis).
- Docker Desktop + Supabase CLI 2.115.0 for the local backend/pgTAP lane.
- Pinned Anki runtimes: `python3 dev/fetch_runtimes.py --list`; all seven
  targets are pinned (26.08.1 asset digests, 23.10 signed checksums) and
  `--install` mounts/installs the runtime and prints its binary. Never install
  a different version silently. Headless probes read the release layout
  (Info.plist, `anki-<v>.dist-info`); hash-verified downloads may declare
  `--anki-actual`, recorded as `declared` in evidence.
- Linux native lanes need Xvfb and a real desktop session; an offscreen Qt
  platform is refused (`dev/runtime_adapter.requires_desktop`).
- Hosted fixture lanes need `ANKISCAPE_PROD_URL`, `ANKISCAPE_PROD_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY` (provisioning only) and
  `ANKISCAPE_FIXTURE_SECRET` from approved secret storage. Per-lane native
  journeys also read `ANKISCAPE_FIXTURE_EMAIL` / `ANKISCAPE_FIXTURE_PASSWORD`.

## Scenario ids

`dev/reliability-matrix.json` is the inventory. Stage membership is in the
same file. Native journeys: `fresh`, `upgrade`, `undo`, `catchup`, `sync`,
`dialogs`, `ui-onboarding`, `ui-training`, `ui-settings`, `ui-review`,
`ui-lifecycle`, `ui-art`, `ui-visual-polish`, `ui-deferred-rewards`,
`ui-rebuild-review`, `ui-test-leaderboard`, `ui-credential-fallback`,
`ui-recovery`, `ui-profile-races`, `ui-report-bug`.

## Rerunning one failed case

- Native journey: `python3 dev.py test --suite e2e --anki 26.8.1 --qt 6
  --journey ui-rebuild-review --anki-bin <path>`; screenshots land in
  `.dev/e2e/<journey>/`.
- One evidence record: delete that lane directory from the evidence tree (the
  tree is derived, never authority) and rerun `run-lane` for that lane.
- Generated trace failure: the runner prints the seed and a minimized
  operation list; reproduce with `dev/generated_traces.py --stage pr` or by
  importing `generate_sequence(seed, length)`.
- Mutation gate: `python3 dev/mutation_gate.py --environment pure --only
  <id>`; full release lane needs Docker.

## Failure triage

- `missing_target` / `missing_scenario`: rerun the named workflow; never edit
  a record by hand.
- `artifact_hash_mismatch`: the lane tested different bytes than the
  candidate; rebuild once and redistribute — do not rebuild after
  verification (release workflow builds exactly once).
- `budget:*`: compare against the paired control and the baseline in
  `artifacts/reliability/baseline/`; changing a budget requires a documented
  plan amendment, not automatic recalibration.
- `skipped_tests` / `zero_tests`: treat as failure; find the skipped test or
  the empty report rather than accepting a green-looking run.
- Flaky-looking native failure: reruns are allowed for diagnosis only, and
  both attempts are retained (`actions/upload-artifact` keeps first failure).

## Fixtures and secrets

- Permanent hosted cohort: `dev/fixtures/hosted-v1.json` (24 names, no
  credentials). Passwords derive from `ANKISCAPE_FIXTURE_SECRET`.
- Seed/verify: `python3 dev/seed_hosted_fixtures.py --local|--hosted
  --plan|--apply|--verify`, then `python3 dev/hosted_fixture_e2e.py`.
- One provisioning/smoke run at a time; <=2 req/s; abort on repeated 429/5xx.
- GitHub configuration: `python3 scripts/configure_github.py
  --plan|--apply|--verify` (labels, Issues, private vulnerability reporting,
  template/workflow files). No sample issues, no messages, no visibility
  changes.

## Nightly ownership

- Owner: repository maintainer. The nightly workflow is the only place the
  seven-target matrix, 30-minute endurance and hosted fixture smoke run.
- Trusted-main only: hosted credentials are never exposed to forks.
- Evidence retention: failure artifacts 30 days, release evidence 90 days.
  Old evidence directories are historical records; never rewrite their dates.

## Budgets

`dev/reliability-budgets.json` holds the fixed acceptance targets (hook
p95/p99, event-loop lag, scaling ratio, shell timing, reward completion,
100k rebuild, idle CPU, endurance memory). `dev/perf_runtime.py` measures the
pure/live pipeline; native lanes supply event-loop, CPU and UI-cycle numbers.
