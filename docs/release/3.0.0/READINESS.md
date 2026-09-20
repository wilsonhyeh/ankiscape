# Release readiness — AnkiScape 3.0.0 (Evolved)

Status: **BLOCKED — release rehearsal not complete.**

Frozen source SHA: _not yet frozen_
Artifact: _not yet frozen_ (name `ankiscape-3.0.0.ankiaddon`)
Artifact SHA-256: _pending_ — no frozen candidate exists. The artifact of
record for this tree is
`a7731a9cdcdfcef5660d151ce636074881e8d5e590ffc2e7d5f3efb1fba32ef8`
(`dist/ankiscape-3.0.0.ankiaddon`, 172 members, `prod_endpoint_baked=true`,
`source_hash fd0baa39…`, `source_hash_no_generated b8592bfa…`); it is a build of
record for `7471f71`, **not** a release candidate and not evidence for any
automated row below. A frozen candidate needs a `release-verify` run bound to
one immutable SHA; nothing here substitutes for that.
Baked project: expected `vjqzamuogcughdvzskmf` (Supabase `ankiscape`)
AnkiWeb ID: `1808450369` (same listing; verify current published version
immediately before publication)
Evidence run: _pending — no `release-verify` run has ever been dispatched, so
no release-verify run id exists to cite. No row below claims one.

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
| Seven native targets (macOS/Windows/Linux × oldest/current, Qt5+Qt6) | pending — journeys now pass on all seven; the lanes still fail on `native-performance` (below) | release-verify `lanes` |
| Native performance per target | **OPEN — the nearer release blocker.** `native-performance: exit 1` on all seven lanes: the journey completes (`answers=120`, `native_performance_completed` ok) and the app then does not exit — a 45 s faulthandler timeout with the main thread inside `aqt._run`. Pre-existing and previously masked by the credential red; the same evidence is in `35488210125`, which predates today's changes | nightly `35531744272`; `native-performance-runs/*/faulthandler.log` and `heartbeat.json` |
| Two-hour endurance per target | pending — shape corrected 2026-09-19 (see above); no lane has run it yet. **Still blocked by the 26.08.1 retention** (below) | `endurance-2h` metrics |
| Endurance gate: current-RSS sampling everywhere, and no green on an unmeasured lifecycle | **fixed 2026-09-20** (#30) — `dev/rss.py` is the single sampler; `no_fixed_phase` fails a run with no fixed-phase samples | `dev/rss.py`, `dev/endurance_metrics.py`, `tests/test_endurance_metrics.py`, `tests/test_dev_rss.py` |
| Endurance retention on the Anki 26.08.1 / Qt 6.11 / CPython 3.13 lanes | **OPEN — the second blocker, behind `native-performance` above.** Real memory, not an add-on leak: identical add-on code is neutral under both Qt versions with Anki absent, and the three pinned components are one indistinguishable variable in this matrix (`dev/runtime_manifest.json`). Requires a decision, not a patch — prove the runtime mechanism, ship on the 23.10 lanes, or both. No `evolved/ui` change can turn it green | nightly `35531744272` re-measured `slope:4.042>1.0` on `linux-26.8.1-qt6`; earlier `slope:4.001` in `35503351999` |
| Shared checks + engine benchmarks | pending | `shared` record |
| Linux backend: suite, parity, account contracts, sync, full mutation | pending | `backend` record |
| Public demo board: five labeled demos, retried 24-account suite retired | pending | `hosted` record + `dev/demo_players.py --verify` |
| Account identity: two-game model (local vs account), server-owned game uuid, login adoption, retired local outbox never uploaded, durable download-first sync, ten-second scheduling | pending | ui-account-lifecycle lane records + dev/account_journey_e2e.py + tests/test_bricked_recovery.py |
| Hosted migrations 0008/0009 and `account-status`/`username-login` deploy | deployed 2026-09-13 | `RELEASE-SMOKE.md:242` |
| Hosted migration 0010 and `account-delete` deploy (JWT verification ON) | deployed 2026-09-14 | `server/OPERATIONS.md:111` |
| Hosted migration 0011 `account_identity_rework` | **deployed 2026-09-19** | `server/supabase/migrations/0011_account_identity_rework.sql`; nightly `35468225639` hosted role `success` |
| Hosted migration 0012 `service_role_table_grants` | **deployed 2026-09-19**; grants `service_role` the table/sequence DML the postgres-owned default ACL withholds, so the required `account-contracts` scenario can write fixtures. `anon`/`authenticated` unchanged | `server/supabase/migrations/0012_service_role_table_grants.sql` |
| Hosted native public browsing/labels per current OS | pending | `native-journeys` target requirements |
| Real-inbox delivery (human) | blocked | Wilson |
| AnkiMobile/AnkiDroid checklist (human) | blocked | Wilson |
| Artwork publication decision (owner) | **closed 2026-09-19 — not a release blocker** (Wilson) | released by owner decision; no longer gating |
| Rollback note: settled rule recorded | prepared | `ROLLBACK.md` |
| Backup restore rehearsal (isolated profile, `ROLLBACK.md` steps) | **engine round-trip performed 2026-09-19; UI menu path still unexercised** — the documented criterion ("the restored state must equal the reference reducer over the same operations", `engine.projection()` boundary: xp_micro, inventory, levels, revision) is now asserted by `tests/test_accounts_backup.py::test_rollback_rehearsal_restored_projection_equals_reference`. That gap was real: the sibling test asserted only operation COUNT, so a restore could have restored the right number of operations while producing a different world. Still not covered: driving Export/Restore through `Settings -> Advanced` in a real profile, which no journey touches | `ROLLBACK.md`; `tests/test_accounts_backup.py` |

Evidence pointers above are `file:line` references in this repository, or a
recorded run id / artifact sha256. No `release-verify` run has ever been
dispatched, so no row cites a release-verify run id; automated rows stay
`pending` until one exists. A row that cannot be grounded in something that
exists is recorded pending/unconfirmed rather than reported green.

Publication requires: every automated row green on one immutable candidate,
human rows recorded, and Wilson's explicit authorization. Do not treat this
file as a go decision.
