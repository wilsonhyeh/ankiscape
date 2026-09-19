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

Deploy note: the Edge Functions were not modified by this release
(`git diff c29e2f0..7471f71 -- server/supabase/functions/` is empty), so the
hosted step is `supabase db push` only — no function deploy is required.

## Requirements matrix

| Requirement | Status | Evidence |
|---|---|---|
| Seven native targets (macOS/Windows/Linux × oldest/current, Qt5+Qt6) | pending | release-verify `lanes` |
| Native performance + two-hour endurance per target | pending — shape corrected 2026-09-19 (see above); no lane has run it yet | `native-performance` / `endurance-2h` metrics |
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
| Artwork publication decision (owner) | blocked | Wilson / ASSET-RIGHTS |
| Rollback note: settled rule recorded | prepared | `ROLLBACK.md` |
| Backup restore rehearsal (isolated profile, `ROLLBACK.md` steps) | pending — not performed; no rehearsal evidence exists | `ROLLBACK.md` |

Evidence pointers above are `file:line` references in this repository, or a
recorded run id / artifact sha256. No `release-verify` run has ever been
dispatched, so no row cites a release-verify run id; automated rows stay
`pending` until one exists. A row that cannot be grounded in something that
exists is recorded pending/unconfirmed rather than reported green.

Publication requires: every automated row green on one immutable candidate,
human rows recorded, and Wilson's explicit authorization. Do not treat this
file as a go decision.
