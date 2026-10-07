# GitHub release body draft — v3.0.2

AnkiScape 3.0.2 fixes two slowdowns for signed-in players. Nothing else
changes, and installing over 3.0.1 keeps your progress.

- No more "Processing..." box during reviews: background sync no longer holds
  up Anki when it saves your answer (#60)
- Opening Hiscores or pressing Sync no longer freezes the window

Verified artifact: `ankiscape-3.0.2.ankiaddon`, SHA-256 `PENDING`, from release
verification run `PENDING`. See `READINESS.md`.

Install: download `ankiscape-3.0.2.ankiaddon` and use
Tools → Add-ons → Install from file. The AnkiWeb listing (1808450369) gets the
same version.

Thanks to @sunrucong for the report and an A/B test that pinned down the cause.

Not affiliated with or endorsed by Jagex Ltd. RuneScape/Old School RuneScape
and the relevant artwork belong to Jagex Ltd; image sourcing is attributed to
the Old School RuneScape Wiki.

**Full changelog:** see `CHANGELOG.md` in `docs/release/3.0.2/`.
