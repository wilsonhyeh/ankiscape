# Release readiness — AnkiScape 3.0.1

Status: **NOT READY — release verification has not run.** This file records
evidence as it is produced; it is not a go decision. Publication requires
Wilson's explicit authorization, and the AnkiWeb upload is manual.

Scope: 3.0.1 is 3.0.0 plus the Hiscores redesign (`docs/release/3.0.1/CHANGELOG.md`).
The only shipped-code changes since `v3.0.0` are the Hiscores screen and its
support files (`evolved/ui/hiscores*.py`, `evolved/hiscores_seen.py`,
`evolved/service.py`, `evolved/ui/theme.py`, `__init__.py`), plus the version bump.

| Row | State | Evidence |
|---|---|---|
| Frozen source SHA on `main` | pending | set when the release PR merges |
| release-verify run (build, key probe, shared, backend, hosted, 7 native targets) | pending | not yet dispatched |
| Artifact `ankiscape-3.0.1.ankiaddon` sha256 | pending | from the evidence run's `candidate-artifact` |
| `verify-evidence --stage release` | pending | aggregate job of the evidence run |
| Server migration `0013` (Overall board) | done | applied to production 2026-09-29 as version `0013`; dry-run in a rolled-back transaction proved all six skill boards unchanged and Overall equal to an independent sum |
| Migration `0013` pgTAP test (`server/supabase/tests/0008_overall_board.test.sql`) | **not run** | no local Supabase stack; CI runs a Linux backend lane |
| Demo accounts stay removed | done | deleted 2026-09-29; PR #50 makes the hosted lane fail if they reappear |
| Unit suite | done on branch | 791 passed at PR #51 (744 baseline + 47 new) |
| Real-Anki journeys on the redesign (macOS 26.8.1) | done at PR #51 | `ui-test-leaderboard`, `ui-account-lifecycle`, `ui-visual-polish`, `ui-profile-races` passed on a pre-merge artifact; **must be re-established by release-verify on the release artifact** |
| Redesign reviewed by eye | done, macOS only | 16 rendered states at 100% and 200% UI scale, minimum window, 100 players |
| Human rows from 3.0.0 (real-inbox, mobile catch-up, backup-restore rehearsal) | carried | unchanged by 3.0.1; see `docs/release/3.0.0/READINESS.md` |
| AnkiWeb upload | owner-manual | Wilson; no API |

## Known gaps

See `KNOWN-LIMITATIONS.md`. The two that matter for a go decision: the pgTAP
file has never run, and nobody has viewed the new screen on Windows or Linux.
