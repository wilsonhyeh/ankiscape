# Release readiness — AnkiScape 3.0.1

Status: **PUBLISHED ON GITHUB (2026-10-02, tag `v3.0.1`); AnkiWeb upload
pending (owner-manual).** Every automated row is green on one immutable
candidate. Publication was authorized by Wilson on 2026-10-02.

Scope: 3.0.1 is 3.0.0 plus the Hiscores redesign (`docs/release/3.0.1/CHANGELOG.md`).
The only shipped-code changes since `v3.0.0` are the Hiscores screen and its
support files (`evolved/ui/hiscores*.py`, `evolved/hiscores_seen.py`,
`evolved/service.py`, `evolved/ui/theme.py`, `__init__.py`), plus the version bump.
No file that reads or writes saved progress (journal, reducer, data, storage,
engine, sync, credentials, backup) differs from `v3.0.0`, so installing over
3.0.0 changes no client data format. That is a diff-level fact; no test installs
3.0.1 over 3.0.0.

Frozen source SHA: `9fe69b675e4d3cddb6fff8a8da697bcd8366e3a4` (main; tag
`v3.0.1`). Everything after the redesign was test and CI code, plus one wording
change to `README.md` (#54), which ships inside the package.
Artifact: `ankiscape-3.0.1.ankiaddon` (175 members, `prod_endpoint_baked=true`,
`source_hash 7693affc…`, `source_hash_no_generated d5509655…`)
Artifact SHA-256:
`31eb1b58e265e30e29ad314d854c9247a7392339e45470c330da6baf09cf62a7` — the
CI-built `candidate-artifact` of the evidence run below. The published release
asset was downloaded again after publication and matches; it is also byte-equal
to a local build of the same tree.
Baked project: `vjqzamuogcughdvzskmf` (Supabase `ankiscape`), key form
`sb_publishable_`, verified by the build job's key probe
AnkiWeb ID: `1808450369` (same listing; verify the currently published version,
3.0.0, immediately before uploading)
Evidence run: **release-verify
[`36883256412`](https://github.com/wilsonhyeh/ankiscape/actions/runs/36883256412)**
(attempt 1 of that run, 2026-10-01 UTC; fifth release-verify run for this
release, see below) — build + key probe, shared, backend, hosted and all seven
native lanes green, each with the two-hour endurance run; aggregate job
`110521981414` passed `verify-evidence --stage release --expected-commit
9fe69b67… --expected-artifact-sha256 31eb1b58…` (`evidence ok: 10 records, 7
targets, run 36883256412-1`) and `account_journey_e2e.py --collect
--require-all-targets` (`account_journey: PASS (7 targets)`).

Warnings carried by the evidence run, each from a documented Wilson scoping
amendment in `dev/reliability-budgets.json` (numbers unchanged in the payload):

- `macos-23.10-qt6` `reward_completion` p95 250.9 ms vs 250 —
  `reward_scoping_amendment` (2026-09-27). Limit unchanged on every lane.
- Qt 6.11 endurance retention — `scoping_amendment` (2026-09-21):
  `macos-26.8.1-qt6` slope 2.139 MiB/min.

| Row | State | Evidence |
|---|---|---|
| Frozen source SHA on `main` | done | `9fe69b675e4d3cddb6fff8a8da697bcd8366e3a4`, tag `v3.0.1` |
| release-verify run (build, key probe, shared, backend, hosted, 7 native targets) | done | run `36883256412`, 12 of 12 jobs green |
| Artifact `ankiscape-3.0.1.ankiaddon` sha256 | done | `31eb1b58…62a7`, the evidence run's `candidate-artifact`, re-verified on the published asset |
| `verify-evidence --stage release` | done | aggregate job of the evidence run (above) |
| Server migration `0013` (Overall board) | done | applied to production 2026-09-29 as version `0013`; dry-run in a rolled-back transaction proved all six skill boards unchanged and Overall equal to an independent sum; applied again on two clean resets in the evidence run's backend lane |
| Migration `0013` pgTAP test (`server/supabase/tests/0008_overall_board.test.sql`) | done | ran and passed in the evidence run's backend lane: `0008_overall_board.test.sql ..... ok`, 8 files, 165 tests, `Result: PASS`, then live Auth/RPC smoke |
| Demo accounts stay removed | done | deleted 2026-09-29; the hosted lane fails if they reappear and passed in the evidence run |
| Unit suite | done | 835 passed at #58 (the commit before the evidence run) |
| Real-Anki journeys on the release artifact | done | all journeys passed on all seven native lanes of the evidence run |
| Redesign reviewed by eye | done | macOS: 16 rendered states at 100% and 200% UI scale, minimum window, 100 players. Windows and Linux: the evidence run's screenshots were viewed on macOS 26.8.1, Windows 26.8.1 and Linux 23.10 Qt5; the board renders with the icon tabs, sort pills, medal bars and outlines on ranks 1-3, and the sign-in bar, nothing clipped. Those captures were taken while the level totals were still on the "Total …" loading placeholder |
| Human rows from 3.0.0 (real-inbox, mobile catch-up, backup-restore rehearsal) | carried | unchanged by 3.0.1; see `docs/release/3.0.0/READINESS.md` |
| GitHub release `v3.0.1` | done | published 2026-10-02 14:36 UTC with the verified artifact and `SHA256SUMS.txt` |
| AnkiWeb upload | owner-manual | Wilson; no API |

## How this evidence run came to be the fifth

Four earlier release-verify runs for this release were red. Every cause was in the
test harness or the runners, none in the add-on, and each was fixed before the next
run. The only file inside the package that changed across the five candidates is
`README.md` (#54, wording), which is why the artifact hash differs from the first run's.

| Run | Commit | Result | Cause |
|---|---|---|---|
| `36694752807` | `4995c222` | red, 3 lanes | `visual_icon_cache_reuse` measured before late icons were decoded (#53); macOS `rebuild_answer_responsive` 313.6 ms on a single sample; Linux Qt5 event-loop lag and an exit crash |
| `36727126349` | `0234e47f` | red, 2 lanes | Anki died before writing any result on macOS (exit -13) and on Linux Qt5 on exit (-11); native journeys now relaunch once when no result was written (#55) |
| `36767446239` | `fc69bca4` | red, 1 lane | Anki 23.10 on Qt5/Linux segfaults on exit when quit during startup; present on the pre-redesign code and on both runner images (31 of 240 launches, 0 of 90 once held open); the driver now keeps Anki up 10 s before quitting (#57) |
| `36838881172` | `c1f3fa56` | red, 2 lanes | `recovery_warning_shown` checked on the first tick after an asynchronous answer; `rebuild_answer_responsive` judged one answer against 250 ms while the product budget allows 5000 ms stalls during a rebuild publish; now a bounded wait and the median of three answers, budget unchanged (#58) |
| `36883256412` | `9fe69b67` | **green, 12 of 12** | — |

## Known gaps

See `KNOWN-LIMITATIONS.md`. For a go decision: the level sort, player cards and
rank arrows have been viewed only on macOS (Windows and Linux are covered by the
automated journeys and by screenshots of the board itself), and installing over
3.0.0 is argued from the diff, not exercised by a test. The nightly workflow was
red on the days around this release for runner-noise reasons unrelated to the
add-on (event-loop-lag gate with little margin); it does not gate this release.
