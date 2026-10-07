# Release readiness — AnkiScape 3.0.2

Status: **PUBLISHED (2026-10-07): GitHub release `v3.0.2` and AnkiWeb
listing `1808450369`.** Every automated row is green on one immutable
candidate. Publication was authorized by Wilson on 2026-10-07.

Scope: 3.0.2 is 3.0.1 plus the two threading fixes for #60
(`docs/release/3.0.2/CHANGELOG.md`). The only shipped-code change since
`v3.0.1` is `__init__.py` (own background executor for network work; removal
of the shadowing blocking `_evolved_manual_sync`), plus the version bump.
No server change. No file that reads or writes saved progress differs from
`v3.0.1`, so installing over 3.0.1 changes no client data format. That is a
diff-level fact; no test installs 3.0.2 over 3.0.1.

Frozen source SHA: `132a2bf48810fc7a935a667eb77ca3038c990e79` (main, merge of
#61; tag `v3.0.2`).
Artifact: `ankiscape-3.0.2.ankiaddon` (175 members)
Artifact SHA-256:
`b5ea6f7d588dcb12ee9d96f08baa8f4ebd6210070760ee937ee5b30d372db8e5` — the
CI-built `candidate-artifact` of the evidence run below. It is byte-equal to a
local build of the same tree, and the published release asset was downloaded
again after publication and matches.
Baked project: `vjqzamuogcughdvzskmf` (Supabase `ankiscape`), verified by the
build job's key probe.
AnkiWeb ID: `1808450369` (same listing; verify the currently published version
immediately before uploading).
Evidence run: **release-verify
[`37661546595`](https://github.com/wilsonhyeh/ankiscape/actions/runs/37661546595)**
(attempt 1, 2026-10-07 UTC; the first release-verify run for this release) —
build + key probe, shared, backend, hosted and all seven native lanes green,
each with the two-hour endurance run; the aggregate job passed
`verify-evidence --stage release --expected-commit 132a2bf4…
--expected-artifact-sha256 b5ea6f7d…` (`evidence ok: 10 records, 7 targets,
run 37661546595-1`) and `account_journey_e2e.py --collect
--require-all-targets` (`account_journey: PASS (7 targets)`).

Warnings carried by the evidence run, each from a documented Wilson scoping
amendment in `dev/reliability-budgets.json` (the same two as 3.0.1):

- `macos-23.10-qt6` `reward_completion` p95 251.41 ms vs 250 —
  `reward_scoping_amendment` (2026-09-27). Limit unchanged on every lane.
- Qt 6.11 endurance retention — `scoping_amendment` (2026-09-21):
  `macos-26.8.1-qt6` slope 2.432 MiB/min, settled 52.4.

## The #60 fix on every target

Both new journeys ran on the release artifact on all seven native lanes.
`ui-slow-sync` answers three cards while a 4 s job holds AnkiScape's network
runner (budget < 600 ms, the point where Anki shows "Processing...").
`ui-sync-stall` calls the real `on_sync` with a 4 s sync (budget < 250 ms).

| Target | Answer times (ms) | Progress window | `on_sync` | Sync ran on |
|---|---|---|---|---|
| windows-26.8.1-qt6 | 104, 103, 103 | never | 2 ms | `ankiscape-net_0` |
| windows-23.10-qt6 | 103, 104, 103 | never | 2 ms | `ankiscape-net_0` |
| macos-26.8.1-qt6 | 129, 173, 134 | never | 1 ms | `ankiscape-net_0` |
| macos-23.10-qt6 | 224, 153, 197 | never | 2 ms | `ankiscape-net_0` |
| linux-26.8.1-qt6 | 104, 104, 103 | never | 1 ms | `ankiscape-net_0` |
| linux-23.10-qt6 | 103, 103, 103 | never | 1 ms | `ankiscape-net_0` |
| linux-23.10-qt5 | 104, 104, 103 | never | 1 ms | `ankiscape-net_0` |

On the shipped 3.0.1 bits (sha256 `31eb1b58…`, macOS 26.8.1, run locally) the
same journeys failed: answers 3887/3849/3850 ms with the progress window shown,
and `on_sync` 4001 ms on `MainThread`.

| Row | State | Evidence |
|---|---|---|
| Frozen source SHA on `main` | done | `132a2bf48810fc7a935a667eb77ca3038c990e79`, tag `v3.0.2` |
| release-verify run (build, key probe, shared, backend, hosted, 7 native targets) | done | run `37661546595`, 12 of 12 jobs green on attempt 1 |
| Artifact `ankiscape-3.0.2.ankiaddon` sha256 | done | `b5ea6f7d…b8e5`, the evidence run's `candidate-artifact`, re-verified on the published asset |
| `verify-evidence --stage release` | done | aggregate job of the evidence run (above) |
| `ui-slow-sync` and `ui-sync-stall` on the release artifact | done | all seven native lanes (table above), including both Windows targets |
| Unit suite | done | 835 passed on the branch before merge |
| Real-Anki journeys on the release artifact | done | all journeys passed on all seven native lanes of the evidence run |
| Production sync end-to-end (`dev/prod_e2e.py`) | done 2026-10-07 | ALL PASS (31 checks incl. cleanup) against the unchanged server |
| Reporter's own confirmation | open | the #60 reporter A/B-tested an equivalent `uses_collection=False` patch on Windows 11 / Anki 26.09.3 and saw the popup go away; they have not run 3.0.2 |
| Human rows from 3.0.0 | carried | unchanged by 3.0.2; see `docs/release/3.0.0/READINESS.md` |
| GitHub release `v3.0.2` | done | published 2026-10-07 21:15 UTC with the verified artifact and `SHA256SUMS.txt`; marked Latest |
| AnkiWeb upload | done | uploaded by Wilson 2026-10-07; the public listing shows "0.38MB. Updated 2026-10-07" (the artifact is 403,571 bytes) |

## Known gaps

See `KNOWN-LIMITATIONS.md`. For a go decision: no CI lane runs Anki 26.09.x,
the reporter's version (the lanes are 26.8.1 and 23.10.1; the fix is the same
Python on every version), and the reporter's separate "Sync unavailable"
failure is not addressed by this release.
