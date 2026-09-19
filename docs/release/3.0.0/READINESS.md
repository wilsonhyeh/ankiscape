# Release readiness — AnkiScape 3.0.0 (Evolved)

Status: **BLOCKED — release rehearsal not complete.**

Frozen source SHA: _not yet frozen_
Artifact: _not yet frozen_ (name `ankiscape-3.0.0.ankiaddon`)
Artifact SHA-256: _pending_ — no frozen candidate exists. The artifact of
record for this tree is
`d06293e1fad09a23de36fd5c63e04747232cec7e5c547d7623648324d9f795b9`
(`dist/ankiscape-3.0.0.ankiaddon`); it is a build of record, **not** a release
candidate and not evidence for any row below. That build predates the edits now
in the working tree, so it no longer matches the tree as it stands — a frozen
candidate needs a clean tree and a fresh build.
Baked project: expected `vjqzamuogcughdvzskmf` (Supabase `ankiscape`)
AnkiWeb ID: `1808450369` (same listing; verify current published version
immediately before publication)
Evidence run: _pending — no `release-verify` run has ever been dispatched, so
no release-verify run id exists to cite. No row below claims one.

## Requirements matrix

| Requirement | Status | Evidence |
|---|---|---|
| Seven native targets (macOS/Windows/Linux × oldest/current, Qt5+Qt6) | pending | release-verify `lanes` |
| Native performance + two-hour endurance per target | pending | `native-performance` / `endurance-2h` metrics |
| Shared checks + engine benchmarks | pending | `shared` record |
| Linux backend: suite, parity, account contracts, sync, full mutation | pending | `backend` record |
| Public demo board: five labeled demos, retried 24-account suite retired | pending | `hosted` record + `dev/demo_players.py --verify` |
| Account identity: two-game model (local vs account), server-owned game uuid, login adoption, retired local outbox never uploaded, durable download-first sync, ten-second scheduling | pending | ui-account-lifecycle lane records + dev/account_journey_e2e.py + tests/test_bricked_recovery.py |
| Hosted migrations 0008/0009 and `account-status`/`username-login` deploy | deployed 2026-09-13 | `RELEASE-SMOKE.md:242` |
| Hosted migration 0010 and `account-delete` deploy (JWT verification ON) | deployed 2026-09-14 | `server/OPERATIONS.md:111` |
| Hosted migration 0011 `account_identity_rework` | pending — committed at `d6819f8`, not yet applied hosted | `server/supabase/migrations/0011_account_identity_rework.sql` |
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
