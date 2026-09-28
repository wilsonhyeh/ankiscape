# Release readiness — AnkiScape 3.0.0 (Evolved)

Status: **READY FOR PUBLICATION — every automated row green on one immutable
candidate; human rows recorded (one deferred by Wilson). Publication still
requires Wilson's explicit authorization; this file is not a go decision.**

Frozen source SHA: `0684d956693b959468fedf0295b8222233ca5a6f` (main; the
product tree is unchanged since `b6bc8b8` — #42, #45 and #46 touch only `dev/`,
CI, tests and non-shipping docs)
Artifact: `ankiscape-3.0.0.ankiaddon` (172 members, `prod_endpoint_baked=true`,
`source_hash 7186fabf…`, `source_hash_no_generated 41747319…`)
Artifact SHA-256:
`c22876fe8f909f831b4dcf0f65affc894c799100966677711a8d7fc806af0e16` — the
CI-built `candidate-artifact` of the evidence run below, byte-identical to the
`b6bc8b8` build Wilson used for the live backup-restore retest.
Baked project: `vjqzamuogcughdvzskmf` (Supabase `ankiscape`), key form
`sb_publishable_` (Wilson, 2026-09-27), verified by the build job's key probe
AnkiWeb ID: `1808450369` (same listing; verify current published version
immediately before publication)
Evidence run: **release-verify
[`36365255588`](https://github.com/wilsonhyeh/ankiscape/actions/runs/36365255588)**
(attempt 1, 2026-09-28 UTC) — build + key probe, shared, backend, hosted and all
seven native lanes green; aggregate job `108786383509` passed
`verify-evidence --stage release --expected-artifact-sha256 c22876fe…`
(`evidence ok: 10 records, 7 targets, run 36365255588-1`) and
`account_journey_e2e.py --collect --require-all-targets`
(`account_journey: PASS (7 targets)`).

Warnings carried by the evidence run, each from a documented Wilson scoping
amendment in `dev/reliability-budgets.json` (numbers unchanged in the payload):

- `macos-23.10-qt6` `reward_completion` p95 252.88 ms vs 250 —
  `reward_scoping_amendment` (2026-09-27). Limit unchanged on every lane.
- Qt 6.11 endurance retention — `scoping_amendment` (2026-09-21):
  `macos-26.8.1-qt6` slope 11.1 MiB/min, settled +291.5 MiB (the runner family:
  15.7 / 9.1 / 11.1 across the three release-verify runs);
  `windows-26.8.1-qt6` slope 12.1, settled +460.3 MiB from an abnormally low
  324.9 MiB baseline (its prior two runs: -0.4 and -0.8; the lane ended near its
  usual ~760-790 MiB). `linux-26.8.1-qt6`, the same Qt 6.11, is flat (-0.16).

Prior release-verify runs on the same product bytes, kept for provenance:
`36330863967` (two macOS items: the reward p95 miss, now scoped, and an m26
account-journey harness end-of-run failure, fixed in #45 along with four
release-validator defects) and `36352832465` (one `linux-23.10-qt5`
`ui-rebuild-review` exit-time SIGSEGV after all ten steps passed; the same
journey passed in that lane's matrix; the same rare exit-time `-11` appeared on
nightlies `35779841717` and `35692375494`; root cause not established).

## History (pre-2026-09-27 blocker narrative)

The notes below describe how the blockers were found and closed. Every row they
call open is resolved in the matrix that follows.

Nightly `35464480415` (dispatched 2026-09-19 on `7471f71`) is the most recent
nightly and the first with an admissible record: `source.dirty: false`, schema
v2, `exit_status: 0`. Its `build`, `shared` and `backend` roles passed —
including `account-contracts`, which had never passed in this repository. Its
`hosted` role **failed**, and that failure was the un-applied `0011` — now
resolved (see below):

```
demo_players: BLOCKED: link_game reply violates the created/resumed pin for
DemoWillow: {'resumed': True, 'game_uuid': '4b3fd130-…'}
```

**Resolved 2026-09-19.** `0011_account_identity_rework.sql` and
`0012_service_role_table_grants.sql` are applied hosted to
`vjqzamuogcughdvzskmf` (`ankiscape`), and the hosted role now passes: nightly
`35468225639` on `44e933c` reports
`Trusted hosted fixture role (real backend): success` — the first time that
role has ever gone green in this repository. Capability advertisement was
verified independently (`evolved_capabilities` returns
`board_visibility: true`, `protocol_version: 2`), and the public demo board
still serves through `hiscores()` with all five demos ranked. The critical
path is therefore no longer the migration; it is `release-verify` on a frozen
candidate.

**The two-hour endurance shape was also defective and is fixed.** The
endurance journey seeded a flat 8 cards while the scenario meant to answer
~14,400 (2/s for 120 minutes), so the queue emptied, Anki showed "finished
this deck", and the driver idled outside the reviewer for the rest of the run
— while the memory trend, which asserted nothing about reviews, still
reported `pass: true`. `dev/e2e/driver_addon/__init__.py` now seeds the full
demand up front and lifts the per-day cap to cover it, and
`dev/endurance_metrics.py` fails such a run as `no_answers_measured`. No
release-verify lane has yet run the corrected shape.

**The memory metric itself was also wrong, and it was the cause of the reported
endurance failure.** `_rss_mib()` sampled `getrusage().ru_maxrss`, which is the
**maximum** resident set size -- a high-water mark that can only ever increase.
Its "slope" was therefore monotonic by construction, any transient peak anywhere
in the run was locked into the evaluated window permanently, and a negative
slope was impossible to observe. That is exactly what the samples showed: RSS
held at *precisely* 528.0 MiB across 15 minutes of reviews (1,263 answers), then
"grew" only once the lifecycle churn pushed the peak higher. Measured against
real current RSS the same shape reports `slope -8.811 MiB/min` and
`growing_rss_change_mib -282.4` -- memory going **down**. The sampler now reads
current resident size: `task_info(MACH_TASK_BASIC_INFO)` on macOS and
`/proc/self/statm` on Linux (`ru_maxrss` is a peak on Linux too, so three of the
seven targets shared the defect; Windows already used `WorkingSetSize`, which is
current). The 1.0 MiB/min and 50 MiB limits are unchanged and still bite -- they
now judge a signal that can move in both directions.

**Nightly `35531744272` (dispatched 2026-09-20 on `ca2764f`) verified the
credential fix and exposed the next blocker in line.** `build`, `shared`,
`backend` and `hosted` all passed. On every one of the seven native lanes the
journey that had reddened the whole matrix now passes:

```
dev: (e2e) journey=ui-credential-fallback steps=5 failed=0 screenshots=2
dev: (e2e) ui-credential-fallback journey PASS on Anki 23.10
```

All seven lanes still fail, and the reason is now `native-performance: exit 1`
— **on every lane, including the four at Qt ≤ 6.5.3.** That is a pre-existing
hang, not a regression from today's work, and it was previously masked: these
lanes used to die at `ui-credential-fallback` and never reached the performance
stage at all. Run `35488210125`, taken before any of today's changes, carries
the same evidence — a 45-second faulthandler timeout with the main thread inside
`aqt._run`, and a heartbeat stopped at `stage: "finish"` with
`native_performance_completed: answers=120 lag=419`. The work finishes and then
the app does not exit.

`linux-26.8.1-qt6` additionally fails `endurance-30m` on the retention above,
re-measured at `endurance:slope:4.042>1.0` against the 1.0 MiB/min limit — the
same defect at the same magnitude, unaffected by anything in this note. The
26.08.1 lanes now reach that stage on their own merits.

So the release now stands on **two** blockers, in this order: the
`native-performance` hang, which reds all seven lanes and is the nearer one, and
the 26.08.1 endurance retention, which reds three and is a scoping decision
rather than a patch.

**Four further defects were found and fixed on 2026-09-20**, three of them
latent rather than blocking, and one of them the nearest thing to a blocker this
tree had. Nothing below changes the paragraph above: `release-verify` on a frozen
candidate is still the critical path, and no lane has run the corrected shape.

*The nightly itself was red on all seven lanes for a reason that was not memory.*
Nightly `35503351999` failed every target on
`ui-credential-fallback:1 — credential_vault_unavailable_refuses`. The
account-delete fix had landed in the wrong layer: `CredentialVault.delete()`
returned `True` when the vault was unavailable, while the journey pins the
primitive contract at `False`. Both halves are now correct — the vault refuses
again, and `ProfileSession._persist` treats an unavailable vault as "nothing to
persist" rather than "failed to persist" (#27). This mattered more than its size
suggests: it reddened the four Qt ≤ 6.5.3 lanes that are the fallback if the
26.08.1 retention cannot be resolved.

*The endurance gate could pass a run whose lifecycle was never measured.*
`evaluate_endurance` runs its churn guard, its leak evaluation and its warm-up
trim all inside `if len(fixed) >= 2`, so a run that produced no fixed-phase
samples skipped all three and reported `evaluated_phase: "all"` with
`pass: true`. It now fails as `no_fixed_phase` — the same class of false green
`no_answers_measured` (above) and `no_lifecycle_cycles` exist to kill, one level
up. A phase-less series is unaffected (#30).

*Two of the three `ru_maxrss` sinks above were never fixed.* The 2026-09-19
sampler correction reached `dev/e2e/driver_addon/__init__.py` only;
`dev/endurance.py:59` and `dev/perf_runtime.py:335` still read the high-water
mark. Neither gates the release — `dev/reliability.py` invokes
`dev/perf_runtime.py` without `--endurance-minutes`, so `measure_endurance`
never runs there, and `dev/endurance.py` is not invoked at all — but both were
reachable by a human running the tools directly, which is how the original false
verdict was produced. The corrected sampler now lives once in `dev/rss.py`, and
a missing measurement is `None` rather than a `0.0` that would read as a flat,
healthy slope (#30).

*A genuine unbounded leak in the Skills screen.* `build_skills_screen` rebuilt
its whole resource grid on every refresh and relied on `deleteLater()`, so a
refresh from a context that cannot drain `DeferredDelete` left the previous slot
set alive as hidden children — 22 → **1672** live `ItemSlot`s over 150 rail
sweeps, under real Qt 6.11.0. Slots are now reused, which is `bank_view.py`'s
existing pattern. **This did not cause the endurance red and does not fix it**:
the leak is identical under Qt 6.11 and Qt 6.5 (44.00 widgets/cycle in both) and
~8× smaller than the macOS lane's retention, so it cannot produce a split with
zero exceptions by Qt family (#29).

`docs/` is not part of the shipped artifact, so none of this moves the artifact
hash.

Deploy note: the Edge Functions were not modified by this release
(`git diff c29e2f0..7471f71 -- server/supabase/functions/` is empty), so the
hosted step is `supabase db push` only — no function deploy is required.

## Requirements matrix

| Requirement | Status | Evidence |
|---|---|---|
| Seven native targets (macOS/Windows/Linux × oldest/current, Qt5+Qt6) | **green** — all seven lanes | release-verify `36365255588` `lanes` |
| Native performance per target | **green** on all seven (m23 reward p95 scoped to a warning, 2026-09-27 amendment; lag max judged by `max_control_delta_ms`, #42) | `36365255588` lane records `native-performance` |
| Two-hour endurance per target | **green** on all seven (`endurance-2h`; Qt 6.11 retention scoped per the 2026-09-21 amendment — see warnings above) | `36365255588` lane records `endurance-2h` |
| Endurance gate: current-RSS sampling everywhere, and no green on an unmeasured lifecycle | **fixed 2026-09-20** (#30) — `dev/rss.py` is the single sampler; `no_fixed_phase` fails a run with no fixed-phase samples | `dev/rss.py`, `dev/endurance_metrics.py`, `tests/test_endurance_metrics.py`, `tests/test_dev_rss.py` |
| Endurance retention on the Anki 26.08.1 / Qt 6.11 / CPython 3.13 lanes | **scoped by Wilson 2026-09-21** — tracked observation, not a release gate; Qt ≤ 6.5 lanes still gate and pass | `dev/reliability-budgets.json` `scoping_amendment`; `36365255588` aggregate warnings |
| Shared checks + engine benchmarks | **green** | `36365255588` `shared` record |
| Linux backend: suite, parity, account contracts, sync, full mutation | **green** | `36365255588` `backend` record |
| Public demo board: five labeled demos, retried 24-account suite retired | **green** | `36365255588` `hosted` record |
| Account identity: two-game model (local vs account), server-owned game uuid, login adoption, retired local outbox never uploaded, durable download-first sync, ten-second scheduling | **green** — `ui-account-lifecycle` on all seven targets | `36365255588` account-journey records; `account_journey: PASS (7 targets)` |
| Hosted migrations 0008/0009 and `account-status`/`username-login` deploy | deployed 2026-09-13 | `RELEASE-SMOKE.md:242` |
| Hosted migration 0010 and `account-delete` deploy (JWT verification ON) | deployed 2026-09-14 | `server/OPERATIONS.md:111` |
| Hosted migration 0011 `account_identity_rework` | **deployed 2026-09-19** | `server/supabase/migrations/0011_account_identity_rework.sql`; nightly `35468225639` hosted role `success` |
| Hosted migration 0012 `service_role_table_grants` | **deployed 2026-09-19**; grants `service_role` the table/sequence DML the postgres-owned default ACL withholds, so the required `account-contracts` scenario can write fixtures. `anon`/`authenticated` unchanged | `server/supabase/migrations/0012_service_role_table_grants.sql` |
| Hosted native public browsing/labels per current OS | **green** | `36365255588` `native-journeys` (`ui-test-leaderboard` on all seven) |
| Real-inbox delivery (human) | **PASSED 2026-09-21** (Wilson, 13/13; defects fixed and merged in #33) | `RELEASE-SMOKE.md` inbox steps |
| AnkiMobile/AnkiDroid checklist (human) | **deferred past 3.0 (Wilson, 2026-09-27)** — no phone/desktop synced setup available; release copy no longer claims phone interop | `KNOWN-LIMITATIONS.md` § Mobile; `RELEASE-SMOKE.md` mobile checklist |
| Artwork publication decision (owner) | **closed 2026-09-19 — not a release blocker** (Wilson) | released by owner decision; no longer gating |
| Rollback note: settled rule recorded | prepared | `ROLLBACK.md` |
| Backup restore rehearsal (isolated profile, `ROLLBACK.md` steps) | **UI menu path PASSED 2026-09-27 (Wilson, live profile)** on the `b6bc8b8` build (sha256 `c22876fe…0e16`, fixes #43 + #44), after failing live on 2026-09-25. Earlier: engine round-trip performed 2026-09-19 — the documented criterion ("the restored state must equal the reference reducer over the same operations", `engine.projection()` boundary: xp_micro, inventory, levels, revision) is now asserted by `tests/test_accounts_backup.py::test_rollback_rehearsal_restored_projection_equals_reference`. That gap was real: the sibling test asserted only operation COUNT, so a restore could have restored the right number of operations while producing a different world. The `Settings -> Advanced` Export/Restore path is still touched by no automated journey; Wilson's 2026-09-27 live retest is the evidence for it | `ROLLBACK.md`; `tests/test_accounts_backup.py` |

Evidence pointers above are `file:line` references in this repository, or a
recorded run id / artifact sha256. Automated rows cite release-verify
`36365255588`, the one run whose evidence all binds to artifact
`c22876fe…0e16`. A row that cannot be grounded in something that exists is
recorded pending/unconfirmed rather than reported green.

Publication requires: every automated row green on one immutable candidate,
human rows recorded, and Wilson's explicit authorization. Do not treat this
file as a go decision.
