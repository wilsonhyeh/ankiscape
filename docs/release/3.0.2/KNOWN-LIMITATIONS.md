# AnkiScape 3.0.2 — known limitations

Everything in `docs/release/3.0.1/KNOWN-LIMITATIONS.md` (and through it
3.0.0's) still applies unchanged. 3.0.2 adds only the following.

- **A failing sync still fails.** 3.0.2 stops a slow or failing sync from
  freezing Anki; it does not change why a sync fails. The #60 reporter's sync
  shows "Sync unavailable — progress saved" on both 3.0.1 and the patch, while
  production sync passes end to end (`dev/prod_e2e.py`, 2026-10-07), so the
  cause is on their side and unknown until they send an in-app report.
- **Tested on macOS by hand.** Windows and Linux evidence comes from the
  release-verify native lanes: `ui-slow-sync` and `ui-sync-stall` passed on
  all seven targets in run 37661546595, including Windows 26.8.1 and 23.10.
  Nobody has used 3.0.2 on Windows yet, and no lane runs Anki 26.09.x (the
  #60 reporter's version).
- Not yet profiled: anything else that may run on Anki's main thread when the
  AnkiScape window opens, beyond the Sync call fixed here.
