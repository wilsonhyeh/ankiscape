# AnkiScape 3.0.2 — smoother reviews while syncing

A bug-fix update on 3.0.1. Gameplay, scoring, accounts, the Hiscores screen
and the sync protocol are unchanged. Installing over 3.0.1 keeps all progress.
There is no data migration and no server change.

## Fixed

- **"Processing..." during reviews (#60).** While signed in, AnkiScape syncs
  in the background, and that network call was sharing the one thread Anki
  uses to save card answers. The next answer waited for the sync and Anki
  showed its "Processing..." box, every few cards for a few seconds. Sync,
  Hiscores and player lookups now run on AnkiScape's own background thread,
  so answering never waits on the network.
- **The window froze when opening Hiscores or pressing Sync.** Both ran a
  full sync on Anki's main thread instead of in the background (an earlier
  fix had been left inactive by an older duplicate of the same function).
  Sync now always runs in the background; the window stays responsive even
  when the server is slow or unreachable.

## Notes

- Who saw it: signed-in Evolved players. Offline play never syncs and was not
  affected.
- Anki compatibility is unchanged: 23.10+ on Qt5/Qt6 (macOS, Linux, Windows).
