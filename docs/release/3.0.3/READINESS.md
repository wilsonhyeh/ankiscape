# Release readiness — AnkiScape 3.0.3

Status: **NOT READY — release verification has not run.** This file records
evidence as it is produced; it is not a go decision. Publication requires
Wilson's explicit authorization, and the AnkiWeb upload is manual.

Scope: 3.0.3 is 3.0.2 plus the Skills-screen fix for the AnkiWeb "cannot
switch to Cooking" report (`docs/release/3.0.3/CHANGELOG.md`). The only
shipped-code changes since `v3.0.2` are three UI files
(`evolved/ui/skills.py`, `widgets.py`, `training.py`) plus the version bump.
Commits on `main` since `v3.0.2` other than this release touch only the
server, dev tooling, tests and docs; no add-on file. No server change in
this release.

| Row | State | Evidence |
|---|---|---|
| Frozen source SHA on `main` | pending | set when the release PR merges |
| release-verify run (build, key probe, shared, backend, hosted, 7 native targets) | pending | not yet dispatched; `ui-training` now switches skill and back on every native target |
| Artifact `ankiscape-3.0.3.ankiaddon` sha256 | pending | from the evidence run's `candidate-artifact` |
| `verify-evidence --stage release` | pending | aggregate job of the evidence run |
| Unit suite | done on branch | 842 passed; 4 new tests in `tests/test_skills_grid_reuse.py`, each red on the 3.0.2 UI code |
| `ui-training` (switch skill and back to a remembered resource) | done, macOS | 3.0.2 UI code: `ui_training_switch_back_enabled` FAIL (`button='Currently training'`), `ui_training_tile_marks_active` FAIL. Fix, packaged artifact (local sha256 `f6c287c9…`): 20/20 steps PASS on Anki 26.8.1 and 23.10.1 |
| Look at it | done, macOS | `e2e-ui-training-switch-back` screenshot: Woodcutting tile "(training)", Mining's remembered Clay selected, Train enabled. Dev profile: Cooking → Shrimp → Train switched the Training screen to "Cooking — Shrimp"; Training screen reads "one Cooked Trout" |
| Reporter's confirmation | not possible | AnkiWeb review; no reply channel |
| Human rows from 3.0.0 | carried | unchanged by 3.0.3; see `docs/release/3.0.0/READINESS.md` |
| AnkiWeb upload | owner-manual | Wilson; no API |

## Known gaps

See `KNOWN-LIMITATIONS.md`.
