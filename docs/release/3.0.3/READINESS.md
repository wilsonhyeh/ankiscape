# Release readiness — AnkiScape 3.0.3

Status: **PUBLISHED (2026-10-14 UTC): GitHub release `v3.0.3` and AnkiWeb
listing `1808450369`, with one owner-accepted exception.** Every row
below is green on one immutable candidate except a single macOS performance
sample; Wilson accepted it on 2026-10-13 instead of a full re-run:
*"looks like it's unlikely this test run result will affect the end user …
I'd like to not waste several hours doing the entire test again if
possible."* Wilson published the GitHub release and uploaded to AnkiWeb.

Scope: 3.0.3 is 3.0.2 plus the Skills-screen fix for the AnkiWeb "cannot
switch to Cooking" report (`docs/release/3.0.3/CHANGELOG.md`). The only
shipped-code changes since `v3.0.2` are three UI files
(`evolved/ui/skills.py`, `widgets.py`, `training.py`) plus the version bump.
Commits on `main` between `v3.0.2` and this release touch only the server,
dev tooling, tests and docs; no add-on file. No server change in this release.

Frozen source SHA: `18c69880a95be0429ab08f84a3fd56b14d3ea5b7` (main, merge of
#69; tag `v3.0.3`).
Artifact: `ankiscape-3.0.3.ankiaddon` (175 members)
Artifact SHA-256:
`f6c287c9c3aba15f64b2342e5199083b3e18684fe1a6c61be5125892d9a0a84f` — the
CI-built `candidate-artifact` of the evidence run below. Byte-equal to a local
build of the same tree. `prod_endpoint_baked: true`; the archive's
`evolved/prod_config.py` names `vjqzamuogcughdvzskmf` (Supabase `ankiscape`),
and the build job's key probe answered HTTP 200.
AnkiWeb ID: `1808450369` (same listing).
Evidence run: **release-verify
[`38444772317`](https://github.com/wilsonhyeh/ankiscape/actions/runs/38444772317)**
(attempt 1, 2026-10-13 UTC; the only release-verify run for this release).

| Row | State | Evidence |
|---|---|---|
| Build + key probe | pass | `Validate and build the immutable candidate` |
| Shared, Linux backend, hosted roles | pass | run 38444772317 |
| Native lanes (journeys, performance, two-hour endurance) | **6 of 7 pass** | `macos-23.10-qt6`, `linux-23.10-qt5`, `linux-23.10-qt6`, `linux-26.8.1-qt6`, `windows-23.10-qt6`, `windows-26.8.1-qt6` green including endurance. `macos-26.8.1-qt6`: see exception |
| `ui-training` with the switch-back steps | **pass on all 7 targets** | 20/20 steps on every lane's `native-matrix-report.md` (Qt 5.15.2, 6.5.0, 6.5.3, 6.11.0) |
| All other native journeys on `macos-26.8.1-qt6` | pass | 11/11, including `ui-slow-sync` and `ui-sync-stall` |
| `verify-evidence --stage release` | **fail** | aggregate job; fails only because the `macos-26.8.1-qt6` lane record is FAIL |
| Unit suite | pass | 842; 4 new tests, each red on the 3.0.2 UI code |
| Local `ui-training` | pass | 3.0.2 UI code fails it (`button='Currently training'`); fix passes on Anki 26.8.1 and 23.10.1 |
| Reporter's confirmation | not possible | AnkiWeb review; no reply channel |
| Human rows from 3.0.0 | carried | unchanged by 3.0.3; see `docs/release/3.0.0/READINESS.md` |
| GitHub release `v3.0.3` | done | published by Wilson 2026-10-14 00:15 UTC with the verified artifact and `SHA256SUMS.txt`; marked Latest; the published asset re-downloaded as `f6c287c9…` |
| AnkiWeb upload | done | uploaded by Wilson 2026-10-14; the public listing shows "0.39MB. Updated 2026-10-14" (the artifact is 403,860 bytes) |

## The accepted exception

`macos-26.8.1-qt6` `native-performance` failed one budget:
`budget:event_loop_lag:max:473.812`. The limit is 200 ms, or within 50 ms of
the paired no-add-on control (control max 185.399 ms) under the 2026-09-21
Decision A amendment.

- It is one sample of 4,507: repetition 3 of 3, phase `answers:55`, 28 s
  into ordinary reviews (not the shell, not a rebuild window). Neighbouring
  samples were 78–120 ms. The other two add-on repetitions peaked at
  238.836 ms and 167.42 ms; p95 was 141.403 ms against a control p95 of
  142.099 ms.
- The review path is byte-identical to `v3.0.2`. Nightly runs on `ab507b3`
  (same add-on code as 3.0.2) passed this budget on this target at
  177.359 / 174.374 / 171.295 ms max (nightlies 38399323789, 38253814835,
  38135439571).
- Consequence: `endurance-2h` was **skipped** on `macos-26.8.1-qt6`
  (`earlier_scenario_failed:native-performance`). The six other targets ran
  it. Qt 6.11 endurance retention is warning-only under the 2026-09-21
  scoping amendment.
- No budget was changed. This is a one-release acceptance, not an amendment.

## Known gaps

See `KNOWN-LIMITATIONS.md`.
