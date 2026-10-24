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
- Teardown relaunch (since 2026-10-21): when every step of a journey passed
  but Anki then crashed or hung on quit, `dev._e2e_suite` returns
  `E2E_TEARDOWN_FAILED` (3) and the journey, matrix and performance runners
  launch it once more. A failed step is never relaunched, and a second
  teardown failure fails the journey (players quit Anki too). The first
  attempt's logs are kept (`-attempt1`), and each relaunch is listed as a
  `teardown_retried:<scenario>:<journey>:x<n>:<record>` evidence warning.
- `fatal` relaunch (since 2026-10-23): when a matrix journey's only failed
  step is the harness's own `fatal` verdict, `dev/ui_ux_verify.py` relaunches
  it once, keeps the first attempt's logs under `-attempt1`, and lists a
  `fatal_retried:<scenario>:<journey>:x<n>:<record>` evidence warning.
  `fatal` is written only by `_fail()` in `dev/e2e/driver_addon/__init__.py`:
  the watchdog timed out, a driver call raised, a journey phase was unknown,
  or a modal dialog blocked the main thread (nightly 39984018007,
  macos-26.8.1-qt6 `ui-onboarding`: a `TypeError: Failed to fetch` alert
  became a Qt `QMessageBox` and held the main thread). It is an environment
  verdict, not a product result. Failed product steps are still never
  relaunched, `fatal` beside any other failed step is a real failure, and a
  second `fatal` fails the journey.
- Performance reps also relaunch once when Anki dies before this launch wrote
  any result (`e2e-assertions.json` whose `run_id` matches `run-id.txt`;
  the previous rep's result files are removed before each launch), as the
  journey and matrix runners already did. This is the macOS-26 exit -13 at
  the `bulk` stage. A rep that wrote a result and failed is never relaunched.
  Each relaunch shows in the metrics file as `launch_retries` on the run and
  a `launch_retried:<journey>:rep<n>` warning, with the earlier attempt's
  evidence under `rep<n>-<mode>-attempt<k>`.
- Event-loop stall (since 2026-10-21): every perf rep's `perf-native.json`
  carries `stall_capture` (driver `stall_capture.py`). It holds all thread
  stacks whenever the 100 ms lag tick ran >150 ms late; `watchdog_gaps`, the
  times the capture thread itself could not run (the GIL was held or the whole
  process was paused; macOS runners pause even the control run ~150 ms at a
  time), each with a stack snapshot taken right after; gc runs
  (every gen 2, gen 0/1 at or above 5 ms); and `Journal.save_checkpoint` /
  `ProjectionWorker._build` timings, all on the `lag_at` clock.
  `native-performance.log` prints one `native_performance: stall:` line per
  sample ≥150 ms, naming the main-thread frame or the busy thread and any
  overlapping gc run or checkpoint call; the same lines are in the metrics
  file's `stall_notes`. Capture stops before the idle window.

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

### 2026-10-21 lag-max amendment (approved by Wilson)

The event-loop-lag max is judged on the typical rep, not the single worst
sample. `event_loop_lag.max_verdict` is `median_rep_max`: the gate takes each
rep's worst ordinary sample (`rep_max_ms`, `control_rep_max_ms`) and compares
their medians (`rep_max_median_ms`, `control_rep_max_median_ms`) against the
unchanged 200 ms ceiling and 50 ms paired-control delta, failing with
`budget:event_loop_lag:max:median_rep:<ms>`. Any pooled sample above
`max_backstop_ms` (1000 ms) still fails with
`budget:event_loop_lag:max:backstop:<ms>`, so a one-second freeze in a single
rep is never averaged away. A payload without per-rep maxima falls back to
the pooled rule. The pooled `max_ms` / `control_max_ms` stay in the payload.

Rationale: on nightlies 2026-10-12..10-21 (unchanged code), 5 lane failures
were a single runner tick in one rep while the other reps sat in budget, e.g.
linux-23.10-qt6 addon reps 146.8 / 437.5 / 187.8 ms against control
184.5 / 131.8 / 273.7 ms. Replayed on those 5 lanes plus all 7 lanes of a
green nightly, the median rule passes all 12. The tradeoff: a stall that
shows up in only 1 of 3 reps and stays under the backstop no longer fails the
gate on its own. The amendment is recorded in the budget file's
`lag_max_amendment` object.

### 2026-10-23 macOS lag-max scoping amendment (approved by Wilson)

Like the 2026-09-27 reward scoping, a macOS-runner event-loop-lag max miss is
a documented warning, not a gate, while it stays bounded and attributable to
the runner. `scope_macos_lag_max()` (in `dev/reliability.py`, applied in
`dev/native_performance.py` and in the aggregate budget re-check) downgrades
`budget:event_loop_lag:max:median_rep:<ms>` (and the pooled fallback
`budget:event_loop_lag:max:<ms>`) to a warning, for nightly and release
profiles on a macOS lane only, and only while both hold: the observed value
the verdict used is at most `macos_max_scope_limit_ms` (300 ms, 1.5x the
ceiling), and a paired control is present with observed minus control at most
`macos_max_scope_control_delta_ms` (100 ms). A missing control, an unparseable
value or a Linux/Windows lane keeps failing; `max:backstop:` and p95 lag
failures are never scoped. The 200 ms ceiling and 50 ms delta are unchanged,
the numbers stay in the payload, and the scoping is recorded as
`scoped:event_loop_lag:max:macos_runner_observation(budgets scoping amendment
2026-10-23, Wilson)`.

Rationale: nightly 39936385911 (macos-23.10-qt6) failed only this gate: addon
rep maxima 177.9 / 200.5 / 200.5 ms (median 200.478) against control
149.1 / 145.8 / 173.1 ms (median 149.072), delta 51.4 vs 50. The macOS runner
alone costs about 145-175 ms of max lag with no add-on (Linux/Windows
controls 8-25 ms), so its lanes have almost no headroom under 200 ms. The
amendment is recorded in the budget file's `lag_max_scoping_amendment` object.
