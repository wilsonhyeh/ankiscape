# Release readiness — AnkiScape 3.0.2

Status: **NOT READY — release verification has not run.** This file records
evidence as it is produced; it is not a go decision. Publication requires
Wilson's explicit authorization, and the AnkiWeb upload is manual.

Scope: 3.0.2 is 3.0.1 plus the two threading fixes for #60
(`docs/release/3.0.2/CHANGELOG.md`). The only shipped-code change since
`v3.0.1` is `__init__.py` (own background executor for network work; removal
of the shadowing blocking `_evolved_manual_sync`), plus the version bump.
No server change.

| Row | State | Evidence |
|---|---|---|
| Frozen source SHA on `main` | pending | set when the release PR merges |
| release-verify run (build, key probe, shared, backend, hosted, 7 native targets) | pending | not yet dispatched; now includes `ui-slow-sync` and `ui-sync-stall` on every native target, the first Windows evidence for this fix |
| Artifact `ankiscape-3.0.2.ankiaddon` sha256 | pending | from the evidence run's `candidate-artifact` |
| `verify-evidence --stage release` | pending | aggregate job of the evidence run |
| Unit suite | done on branch | 835 passed |
| `ui-slow-sync` (answers while a 4 s network job is in flight) | done, macOS | shipped 3.0.1 bits (sha256 `31eb1b58…`): answers 3887/3849/3850 ms, progress window seen → FAIL. Fix: 103/105/104 ms on Anki 26.8.1, 106/105/107 ms on 23.10.1, no window → PASS |
| `ui-sync-stall` (real `on_sync` with a 4 s sync) | done, macOS | shipped 3.0.1: 4001 ms on `MainThread` → FAIL. Fix: 1 ms, sync ran once on `ankiscape-net_0`, on 26.8.1 and 23.10.1 → PASS |
| Other journeys on the fix (macOS 26.8.1) | done on branch | `ui-account-lifecycle`, `ui-test-leaderboard`, `ui-review`, `ui-settings`, `dialogs` green; must be re-established by release-verify on the release artifact |
| Production sync end-to-end (`dev/prod_e2e.py`) | done 2026-10-07 | ALL PASS (31 checks incl. cleanup) against the unchanged server |
| Reporter's Windows confirmation | partial | #60 reporter A/B-tested an equivalent `uses_collection=False` patch on Windows 11 / Anki 26.09.3 and saw the popup go away; the shipped fix differs (single own worker) and is not yet confirmed by them |
| Human rows from 3.0.0 | carried | unchanged by 3.0.2; see `docs/release/3.0.0/READINESS.md` |
| AnkiWeb upload | owner-manual | Wilson; no API |

## Known gaps

See `KNOWN-LIMITATIONS.md`.
