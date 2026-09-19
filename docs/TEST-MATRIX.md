# AnkiScape test matrix

Requirements-to-test mapping with stable ids. Layers: `python` (unittest,
no Anki), `backend` (local Supabase/pgTAP), `native` (packaged add-on in a
real Anki), `ci` (workflow orchestration). Gate: `pr`, `nightly`, `release`.
Evidence paths are relative to the repository root.

| Id | Severity | Requirement | Layer | Scenario / test | Gate | Evidence |
|---|---|---|---|---|---|---|
| REL-01 | high | Evidence contract: matrix/targets/artifact/schema/freshness/files/scenarios/skips/budgets all validated | python | `tests/test_reliability_evidence.py` (33 cases) | pr | `artifacts/reliability/<run>/local/` |
| REL-02 | high | Nearest-rank percentiles never exceed the observed max | python | `tests/test_reliability_evidence.py::PercentileTests` | pr | same |
| REL-03 | high | Accepted-answer hook p95/p99 budgets at 0/1k/10k/100k history, >=500 samples (release) | python | `python3 dev/perf_runtime.py --profile release` | release | lane `metrics.json` |
| REL-04 | high | Warm 100k-vs-1k p95 ratio <=2x, absolute budget still met | python | `dev/perf_runtime.py` (`warm_review_scaling`) | release | same |
| REL-05 | high | Reward completion p95 <=250 ms from durable append to displayed state | python+native | `dev/perf_runtime.py`; `ui-deferred-rewards` journey | nightly | lane record |
| REL-06 | high | 100k late-retraction rebuild <=8 s target / <=10 s gate; review stays responsive | python+native | `dev/perf_runtime.py`; `ui-rebuild-review` journey | release | lane record |
| REL-07 | medium | Event-loop lag p95 <=50 ms, no stall >200 ms | native | platform lane journeys (deferred rewards + rebuild) | nightly | lane record |
| REL-08 | medium | Cold shell <=500 ms, no main-thread replay; warm shell/section p95 <=300 ms | python+native | `dev/perf_runtime.py`; `ui-visual-polish` journey | nightly | lane record |
| REL-09 | medium | Idle/closed add-on: no new timers/polling; CPU delta <=1 pt | native | platform lane endurance (`ui-lifecycle`, `dev/endurance.py` record) | release | lane record |
| REL-10 | medium | Endurance memory: slope <=1 MiB/min, settled <=50 MiB | python+native | `python3 dev/endurance.py --minutes 30/120` | nightly/release | `endurance-*.json` |
| ENG-01 | high | Atomic accepted-answer persistence (seq+op+outbox+observation) | python | `tests/test_journal_atomic_review.py` (8 cases) | pr | CI log |
| ENG-02 | high | Incremental state equals full replay at every prefix | python | `tests/test_projection_incremental.py` (13 cases) | pr | CI log |
| ENG-03 | high | Worker publishes monotonically; corrupt checkpoint disposable; bounded stop | python | `tests/test_projection_worker.py` (10 cases) | pr | CI log |
| ENG-04 | high | Failed write: no award, no claim, recovery warning; conservative reconcile | native+python | `ui-recovery` journey; `tests/test_coverage_guards.py` | nightly | lane record |
| ENG-05 | high | Undo/redo/retract/restore precedence; duplicate/conflicting ids | python | `tests/test_reducer.py`, `tests/test_catchup_undo.py`, `tests/test_projection_incremental.py` | pr | CI log |
| ENG-06 | medium | Deterministic RNG/gems/cooking gate/policies 1-2 identical across paths | python | `tests/test_projection_incremental.py`, `tests/test_reducer.py`, `dev/scoring_parity.py --local` | pr/backend | CI log |
| ENG-07 | high | State equals the unique canonical operation set (arrival order) | python | generated traces + `tests/test_reducer.py::test_permutation_invariant` | pr | CI log |
| GEN-01 | high | >=100 generated sequences of <=100 ops at PR; >=1000 nightly; >=5000 release | python | `tests/test_generated_sequences.py`; `dev/generated_traces.py --stage` | pr/nightly/release | CI log |
| GEN-02 | medium | Failures retain seed + minimized reproducer | python | `dev/generated_traces.py` (minimize on failure) | nightly | CI log |
| MUT-01 | high | Reward-policy guard, gem grant, stale-generation guard mutants killed | python | `python3 dev/mutation_gate.py --environment pure` | release | `mutation.json` |
| MUT-02 | high | Cohort-filter mutant killed | backend | `dev/mutation_gate.py --environment full` (pgTAP lane) | release | `mutation.json` |
| COV-01 | high | All 69 resource rows / six skills / thresholds / overflow / policies / RNG / gems / cooking gate | python | `tests/test_rules_economy.py`, `tests/test_logic*.py`, `tests/test_reducer.py` | pr | CI log |
| COV-02 | high | Python/SQL scoring parity | backend | `python3 dev/scoring_parity.py --local` | pr | CI log |
| COV-03 | high | Account/network contracts: 401 refresh once, rotated token, 429 bounded, 5xx, banned/flagged, old-client rejection | python | `tests/test_account_contracts.py`, `tests/test_net_auth.py` | pr | CI log |
| COV-04 | high | Storage/recovery: damaged cache vs journal, future schema, incomplete backup, Unicode paths | python | `tests/test_accounts_backup.py`, `tests/test_journal.py`, `tests/test_storage*.py` | pr | CI log |
| COV-05 | medium | Lifecycle: profile switch/logout/close during credit, rebuild, sync | native | `ui-lifecycle`, `ui-profile-races` journeys | nightly | lane record |
| COV-06 | medium | Fault paths: offline, malformed/truncated JSON, timeouts, disconnect | native+python | `ui-*` fault journeys; `tests/test_net_auth.py` | nightly | lane record |
| COV-07 | medium | Credentials: vault available/locked/unavailable, no plaintext fallback | native | `ui-credential-fallback` journey | nightly | lane record |
| COV-08 | high | Security: direct table writes denied, cross-account denied, forged cohort, unsafe content, no secrets in artifacts | backend+python | pgTAP suites; `tests/test_reliability_evidence.py`; package audit | release | CI logs |
| COV-09 | medium | Upgrade: packaged fresh install, 2.0.2 upgrade path, mode preservation, uninstall/reinstall | native | `fresh`, `upgrade` journeys | release | lane record |
| COV-10 | high | Classic award gate: exp is credited, persisted and the answer submitted only when the gate holds; answer-hook registration order is load-bearing; a missed roll is not a lockout; a gain crossing several levels shows one dialog; Evolved passes through | python | `tests/test_classic_awards.py`; `tests/test_award_durability.py` | pr | CI log |
| COV-11 | high | Native endurance validity: a trend run must have actually reviewed cards, so a deck that empties mid-run cannot report a green memory trend while measuring an idle application | python | `tests/test_endurance_metrics.py` | pr/release | CI log |
| FIX-01 | high | Test cohort isolation: classification birth, forgery denial, public exclusion, test authorization, ties/limits/banned | backend | `server/supabase/tests/0004_test_cohorts.test.sql` (31 plan) | pr/release | pgTAP output |
| FIX-02 | high | Public demo suite: five labeled demos, every-skill coverage, reproducible tie, deterministic hashes, bounds | python | `tests/test_demo_traces.py`; `tests/test_fixture_plans.py` | pr | CI log |
| FIX-03 | high | Public demo apply twice is idempotent; verify scores/labels/isolation; retired hosted-v1 tools refuse | backend | `dev/demo_players.py --local/--hosted --plan/--apply/--verify` | nightly/release | `public-demo-verify.json` |
| FIX-04 | medium | Registry-owned identities are never deleted by disposable cleanup | python+backend | `dev/prod_e2e.py` guard; pgTAP `on delete restrict` | release | CI log |
| ACC-01 | high | Typed account outcomes (duplicate email, unconfirmed resume, invalid code, rate limit, offline) with bounded error codes | backend+python | `server/supabase/tests/0005_public_board.test.sql`; `tests/test_account_lifecycle.py` | pr | pgTAP + CI log |
| ACC-02 | high | One account window: real buttons, async off-thread requests, stale-callback discard, reset success independent of vault/sync | python+native | `tests/test_account_flow.py`; `ui-account-lifecycle` journey | pr/nightly | CI log + lane record |
| ACC-03 | high | Durable page ingest (op+cursor one transaction, no outbox echo), explicit acks, no-progress backoff | python | `tests/test_sync_durable.py`; `tests/test_sync.py` | pr | CI log |
| ACC-04 | high | First pending change schedules sync within ten seconds; single-flight coalescing; permanent states pause | python+native | `tests/test_scheduler.py`; `ui-account-lifecycle` journey | pr/nightly | CI log + lane record |
| ACC-05 | high | Public rankings browse logged out; five demos labeled; test-cohort and legacy fixtures never leak; allowlisted response fields | backend+native | `0005_public_board.test.sql`; `ui-test-leaderboard` journey | nightly | pgTAP + lane record |
| ACC-06 | high | Per-target account-journey records aggregate with `--require-all-targets`; missing targets fail | ci | `dev/account_journey_e2e.py --local --matrix dev/reliability-matrix.json --require-all-targets` | nightly/release | `artifacts/account-journey/<target>/record.json` |
| ACC-07 | high | `link_game` creates on first call and resumes thereafter; the returned `game_uuid` is server-owned and adopted by every caller (R18) | backend | `server/supabase/tests/0007_account_identity.test.sql`; `dev/auth_smoke.py`; `dev/scoring_parity.py --local`; `dev/account_contracts_e2e.py --local` | pr | pgTAP output + backend CI log |
| ACC-08 | high | Two-game model: login adopts the account game, local game retained, no cross-game import or upload | python+native | `tests/test_two_game_model.py`; `ui-account-lifecycle` | pr/nightly | CI log + lane record |
| ACC-09 | high | Bricked profile recovery on a synthetic unbound journal: outbox emptied, operations retained, `acked` never set, zero uploads | python | `tests/test_bricked_recovery.py` | pr | CI log |
| ACC-10 | high | Board visibility filters exactly `hiscores`/`public_profile` before rank/sort/limit; test-cohort RPCs and recorded stats unchanged | backend | `server/supabase/tests/0007_account_identity.test.sql` | pr/release | pgTAP output |
| ACC-11 | high | Board visibility toggle persists via RPC; control hidden when the capability is absent or `self_context` lacks the field | python+native | `tests/test_board_visibility.py`; `ui-test-leaderboard` | pr/nightly | CI log + lane record |
| ACC-12 | high | Email-send 429 renders email-specific copy with the email-limit distinction on the coded path, at any Retry-After; covered by a direct `rate_limit_copy(email_limit=True)` unit test | python | `tests/test_rate_limit_copy.py` | pr | CI log |
| ACC-13 | medium | Account deletion discoverable from Settings → Account; delete copy distinguishes account vs offline game | native | `ui-account-lifecycle` | nightly | lane record |
| ACC-14 | medium | The register notice discloses the board-visibility toggle on both variants (first-run and upgrade), before the user commits | python | `tests/test_two_game_model.py` (notice copy assertions for both variants) | pr | CI log |
| UI-01 | high | Icon cache bounded (256/16 MiB), misses cached, DPI/revision keyed, no re-decode | python+native | `tests/test_icon_cache.py`; `ui-visual-polish` journey | pr/nightly | CI log + lane record |
| UI-02 | high | Rows update by identity; bounded count after 100 section changes | native | `ui-visual-polish` journey | nightly | lane record |
| UI-03 | high | Reviewed icon-slot map; no misleading achievement fallback; manifest coverage | python | `tests/test_icon_cache.py::SlotMapTests`; `scripts/audit_assets.py --check` | pr | CI log |
| UI-04 | medium | Screens at 100/150/200%, min shell size, accessible names, keyboard nav, offline zero requests, install tree unchanged | native | `ui-visual-polish` journey + `ui-art` journey | nightly | screenshots + lane record |
| UI-05 | medium | Stale Hiscores results discarded (request/account generation) | python+native | `tests/test_coverage_guards.py::StaleGuardTests`; `ui-profile-races` | pr/nightly | CI log |
| SUP-01 | high | Diagnostics allowlist excludes secrets/personal data; preview equals copied/opened payload | python | `tests/test_diagnostics.py` | pr | CI log |
| SUP-02 | high | Report dialog: cancel sends zero requests; oversized/Unicode input; clipboard/browser failure | native | `ui-report-bug` journey | nightly | lane record |
| SUP-03 | medium | GitHub config/labels/issue forms verified against the repo | ci | `python3 scripts/configure_github.py --verify` | release | script output |
| CRED-01 | medium | Credits page concise with required notices; details offline; package audit covers both views | python+native | `tests/test_guide_credits.py`; `ui-art` journey | pr/nightly | CI log |
| PLT-01 | high | Seven native targets (macOS/Win/Linux x Qt5/6) packaged journeys | native | `dev/ui_ux_verify.py` per target lane | nightly/release | lane records |
| PLT-02 | high | One Linux native smoke + local backend on PR | native+backend | pr workflow lanes | pr | CI log |
| PLT-03 | high | Evidence aggregation validates all seven targets before release success | ci | `python3 dev/reliability.py verify --stage release --evidence artifacts/reliability` | release | `artifacts/reliability/` |

## Notes

- Coverage percentages (>=90% branches in changed engine/journal/service code)
  are reported by the nightly workflow; they never substitute for the
  enumerated cases above.
- Native UI cycles for endurance run in the platform lane `ui-lifecycle`
  journey; `dev/endurance.py` records the journal/engine/worker layers it
  actually ran and never claims UI cycles.
- Expected skips are zero in mandatory lanes; optional Hypothesis examples
  degrade to deterministic sequences without reporting a skip.
