# AnkiScape account/sync repair — findings

Evidence labels: **confirmed** (source or runtime proves it), **reproduced**
(observed in the packaged/fixture run), **hypothesis** (plausible, not
established). Reviewed HEAD at plan start: `796fd41`. Baseline record:
`artifacts/account-repair/baseline.json`.

## Runtime baseline (confirmed, local read-only)

- Installed add-on in `.dev/anki/user/addons21/ankiscape` matches repo source
  (`__init__.py` sha256 `f768819b…`, manifest package `1808450369`, version
  `3.0.0`). So the reported playground runs the reviewed source.
- The reported profile `dev-user` holds one game
  `68bcf32c-aef0-4c29-b059-7bf9da5be144` with **71 pending `review_award`
  operations, zero acked, empty server cursor**. No `account_binding`
  metadata exists, so the product never linked this game.
- A remembered credential item exists in the OS keychain (service
  `AnkiScape Evolved`); its value was never read.
- Journal backed up with the SQLite backup API to
  `artifacts/account-repair/private/` (private, not for upload).

## Finding status

| # | Finding | Status | Evidence |
|---|---|---|---|
| 1 | Signup always opens the verification page and discards `AccountResult.needs_code` | confirmed | `__init__.py:1803` returns only `{"ok": True}`; `show_code_dialog` unconditional at `:1833` |
| 2 | Existing-email signup can look successful; no duplicate outcome exists | confirmed | `evolved/accounts.py:33` treats any non-error response as success; `AccountResult` has no `email_exists` |
| 3 | The product never links a game; server requires linkage before submission | confirmed | only dev tools call `link_game`; `0003_registration_gate.sql:81` gates `submit_operations` on a player-owned game |
| 4 | The 71 pending reviews are local-only and were never syncable | reproduced | baseline record: unacked outbox, empty cursor, no binding; `submit_operations` would fail `game_mismatch` |
| 5 | Account HTTP runs synchronously on the Qt thread | confirmed | `evolved/ui/dialogs.py:36` `complete(submit(payload))`; login also calls `svc.maybe_sync(on_login=True)` inline at `__init__.py:2322` |
| 6 | Reset success can be reported as failure when post-PUT session persistence raises | hypothesis | `__init__.py:2290` `set_new_password` succeeds, then `sess.session.set(...)` invokes the vault/persistence callback at `:2293`; a local exception after a successful PUT surfaces as a failure. Not reproduced against hosted Auth. |
| 7 | Recovery never links or syncs | confirmed | `__init__.py:2270` updates the session and views only |
| 8 | A first sync can wait ~20 minutes or 200 reviews | confirmed | `evolved/sync.py:44` `min_reviews=200`, `min_interval_s=20*60`; no prior success yields `since=0` on ordinary events |
| 9 | Cursor advances before durable ingestion; remote rows enter the upload outbox | confirmed | `evolved/sync.py:78` saves the cursor while downloading; `_ingest_remote` → `journal.append_operation` inserts into `outbox` (`evolved/journal.py:287`) |
| 10 | Upload assumes a missing ack list means success | confirmed | `evolved/sync.py:116` `result.get("acked", batch_ids)` |
| 11 | Pending counts are capped and inconsistent | confirmed | `_pending_ids(limit=1)` vs `status_line(limit=1000)` (`evolved/service.py:416,432`); header counts operations, not reviews |
| 12 | Public leaderboard is hidden when logged out even though the server grants it to `anon` | confirmed | `evolved/ui/hiscores.py:167` `panel.setVisible(logged_in)`; `_evolved_query_hiscores` raises when logged out (`__init__.py:2134`) |
| 13 | Public responses expose more state than needed | confirmed | `_profile_cohort` returns `to_jsonb(s)`; rank rows carry `user_id` (`0006_test_cohorts.sql:142,167`) |
| 14 | Username login turns upstream timeout/429/service failures into "wrong password" | confirmed | `username-login/index.ts` maps every failure to `generic(401)` |
| 15 | The old test-leaderboard journey asserts a 24-account fixture population | confirmed | driver `FIXTURE_NAMES` (24 names) and `test_leaderboard_test_rows`; superseded by the five-demo decision |

## Runtime reproduction performed this session

- Read-only inspection of the preserved profile (no launch, no wipe):
  71 pending confirmed, no binding, no cursor.
- Unit and local-layer reproduction follows in the task evidence below as the
  repair is implemented (duplicate signup, unlinked upload, crash-safe
  ingestion, ten-second scheduling, public browsing).

## Hosted / inbox items that remain owner-run

- Real inbox signup/recovery/resend (Wilson only; no agent sends email).
- Hosted migrations 0008/0009, `account-status` deploy and the demo apply
  require explicit authorization and are reported as hosted blockers, not
  silently skipped.
