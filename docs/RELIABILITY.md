# Reliability operations

One-command recipes, prerequisites, triage and ownership for the reliability
gates. See `docs/TEST-MATRIX.md` for the requirement-to-test mapping.

## Stages and commands

| Stage | Command | What runs | Expected duration |
|---|---|---|---|
| pr | `python3 dev/reliability.py verify --stage pr` | Pure suite, generated PR traces, asset/package audit; Linux backend + one native smoke in CI | ~1 min local / ~15 min CI (excluding first runtime download) |
| nightly | `python3 dev/reliability.py verify --stage nightly` | Adds seven-target native matrix, expanded generated traces, 30-min endurance, performance metrics, trusted hosted public-demo verify | ~4-6 h CI |
| release | `python3 dev/reliability.py verify --stage release --evidence artifacts/reliability` | Consumes validated lane evidence + mutation gate; all seven targets required | ~6-8 h CI |
| evidence only | `python3 dev/reliability.py verify-evidence --stage pr --matrix dev/reliability-matrix.json --evidence artifacts/reliability` | Validates provenance/hashes/scenarios/budgets without executing. `--stage pr` is required: the default is `release`, and both `nightly` and `release` additionally demand `--expected-commit <sha> --expected-artifact-sha256 <sha256> --expected-run-id <run>` (candidate binding) | seconds |
| one lane | `python3 dev/reliability.py run-lane --stage nightly --os linux --anki 26.8.1 --qt 6 --anki-bin <path> --out artifacts/reliability/<run>/<lane>` | Executes this lane's scenarios and writes `record.json` | 10-90 min |
| local | `python3 dev/reliability.py verify --stage pr` on macOS | Runs what this machine can and fails naming `.github/workflows/pr.yml` for the rest — it never pretends to run Windows on a Mac | ~1 min |

Baseline capture (already recorded at `artifacts/reliability/baseline/`, do not
overwrite its date): `python3 dev/reliability.py baseline`.

The `evidence only` command above parses and runs as written, but currently
exits **1**: no admissible release record exists on this tree, so it reports
`record_version` plus `missing_role:shared|backend|native`. That is the state
of the evidence tree, not a CLI error — do not "fix" it by relaxing the flags.

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
- Linux native lanes need Xvfb, a real desktop session (`dev/runtime_adapter.requires_desktop` refuses offscreen) and a UTF-8 locale (`LANG=C.UTF-8`; the 23.10 launcher exits without one). The 23.10 Qt5 bundle needs its own system libs beyond the Qt6 set: `libglib2.0-0`, `libxcb-xinerama0`, `libxcb-randr0`, `libxcb-sync1`, `libxcb-xinput0`, `libxi6`, `libxtst6`, `libxdamage1`, `libxcomposite1`, `libxrandr2`, `libxcursor1`, `libsm6`, `libice6`, `libnss3`, `libpulse-mainloop-glib0`, `libwayland-cursor0`, `libwayland-egl1`, `libgstreamer1.0-0`, `libgstreamer-plugins-base1.0-0` (workflows install the full list).
- Hosted lanes need `ANKISCAPE_PROD_URL`, `ANKISCAPE_PROD_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY` (provisioning only) and
  `ANKISCAPE_FIXTURE_SECRET` from approved secret storage. Per-lane native journeys run the deterministic local account fixture and need no fixture credentials.

## Scenario ids

`dev/reliability-matrix.json` is the inventory. Stage membership is in the
same file. Native journeys: `fresh`, `upgrade`, `undo`, `catchup`, `sync`,
`dialogs`, `ui-onboarding`, `ui-training`, `ui-settings`, `ui-review`,
`ui-lifecycle`, `ui-art`, `ui-visual-polish`, `ui-deferred-rewards`,
`ui-rebuild-review`, `ui-test-leaderboard`, `ui-account-lifecycle`,
`ui-credential-fallback`, `ui-recovery`, `ui-profile-races`, `ui-report-bug`.

## Rerunning one failed case

- Native journey: `python3 dev.py test --suite e2e --anki 26.8.1 --qt 6
  --journey ui-rebuild-review --anki-bin <path>`; screenshots land in
  `.dev/e2e/<journey>/`.
- Bricked-profile recovery: `python3 -m unittest tests.test_bricked_recovery -v`
  rebuilds the synthetic unbound profile from scratch and replays the
  post-auth coordinator against the loopback fixture server; no artifact is
  needed.
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

- Permanent public demo players: `dev/fixtures/public-demo-v1.json` (five
  labeled demos, no credentials). Passwords derive from
  `ANKISCAPE_FIXTURE_SECRET`.
- Plan/apply/verify: `python3 dev/demo_players.py --local|--hosted
  --plan|--apply --plan-file PATH|--verify`. The old hosted-v1 24-account
  suite is retired; `dev/seed_hosted_fixtures.py` refuses to recreate it.
- Account journeys: `python3 dev/account_journey_e2e.py --local` runs the
  packaged `ui-account-lifecycle` journey over the real local Auth chain and
  writes this target's record under `artifacts/account-journey/`;
  `--collect --require-all-targets` validates records from every target.
- Bricked-profile recovery: `tests/test_bricked_recovery.py` generates its
  synthetic profile through the Journal API. No scenario or fixture reads
  `artifacts/account-repair/private/` — that tree is a real user's operation
  payloads and is never a test input.
- One provisioning/smoke run at a time; <=2 req/s; abort on repeated 429/5xx.
- GitHub configuration: `python3 scripts/configure_github.py
  --plan|--apply|--verify` (labels, Issues, private vulnerability reporting,
  template/workflow files). No sample issues, no messages, no visibility
  changes.

## Nightly ownership

- Owner: repository maintainer. The nightly workflow is the only place the
  seven-target matrix, 30-minute endurance and hosted public-demo verify run.
- Trusted-main only: hosted credentials are never exposed to forks.
- Evidence retention: failure artifacts 30 days, release evidence 90 days.
  Old evidence directories are historical records; never rewrite their dates.

## Budgets

`dev/reliability-budgets.json` holds the fixed acceptance targets (hook
p95/p99, event-loop lag, scaling ratio, shell timing, reward completion,
100k rebuild, idle CPU, endurance memory). `dev/perf_runtime.py` measures the
pure/live pipeline; native lanes supply event-loop, CPU and UI-cycle numbers.

### 2026-09-14 amendment (approved by Wilson)

The paired native gate measures ordinary responsiveness and rebuild-window
behavior separately. Ordinary budgets are unchanged: event-loop-lag p95
50 ms / max 200 ms, reward completion p95 250 ms.

Rationale: after a late retraction with a very large review history, the
projection worker replays that history on the main thread and stalls it for
seconds while publishing rewards; the worst observed stall (~3.7 s, see
`docs/release/3.0.0/KNOWN-LIMITATIONS.md`) sits far above the ordinary
event-loop-lag budget but is inherent to a main-thread projection, and the
rewards it publishes only appear when the projection completes. Bounding
rebuild-window behavior with its own limits keeps the ordinary budgets
honest without failing every release on a known, bounded stall. The
amendment is recorded in the budget file's `amendment` object; changing any
of these limits again requires a documented plan amendment.

Amended semantics, exactly as implemented in `dev/native_performance.py`
and `dev/reliability.py`:

- **Separate rebuild-window buckets.** The driver classifies each event-loop
  lag sample and each reward sample as ordinary or rebuild-window (a sample
  whose measurement overlapped a late-retraction rebuild) and records them
  in `rebuild_lag_ms` and `rebuild_reward_ms`, never in `lag_ms` /
  `reward_ms`. Ordinary and rebuild-window summaries are computed
  independently; rebuild samples never leak into the ordinary summaries.
- **Rebuild-window bounds.** `event_loop_lag.rebuild_window.max_ms` is
  bounded at 5000 ms (~37% above the worst observed 3660 ms) and
  `reward_completion.rebuild_window.p95_ms` at 10000 ms (equal to the
  rebuild hard gate). Exceeding either fails with
  `budget:event_loop_lag:rebuild_window:max:<ms>` /
  `budget:reward_completion:rebuild_window:p95:<ms>`.
- **Paired control delta on macOS.** The runner baseline alone can exceed
  the 50 ms absolute p95 (observed on macOS), so when `event_loop_lag.p95_ms`
  exceeds the absolute limit the gate compares it against the paired
  control's p95: it passes only if `control_p95_ms` is known and
  `p95 - control_p95 <= p95_control_delta_ms` (50 ms). With no control
  baseline or no configured delta limit, the absolute limit alone decides.
  The absolute limit still applies even over a fast control.
- **Reconciliation counters.** The driver reports `lag_probes` and
  `rewards_published` totals; the orchestrator reconciles them against the
  classified sample counts. Mismatches fail with `lag_samples:unreconciled`
  / `reward_samples:unreconciled`. Reconciling across runs sums the
  counters; a payload without the counters (old shape) makes
  reconciliation not applicable — never a fabricated failure. Missing
  driver counters are never treated as zero.
- **Rebuild count guards.** Rebuilds recorded with no rebuild-window stall
  samples fail `rebuild_window_lag:not_measured`; stall or reward samples
  present with zero recorded rebuilds fail `rebuild_window_lag:unreconciled`
  / `rebuild_reward_samples:unreconciled` (the empty-bucket guard applies
  to payloads of either shape).
