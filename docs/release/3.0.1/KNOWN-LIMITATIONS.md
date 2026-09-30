# AnkiScape 3.0.1 — known limitations

Everything in `docs/release/3.0.0/KNOWN-LIMITATIONS.md` still applies
unchanged. 3.0.1 adds or changes only the following.

## Hiscores

- **Beyond the top 100 of a board,** the "You" bar can only say you are not in
  the loaded rankings. A server-side "my standing" lookup would fix that and is
  not built (the board is far below 100 players today). The server caps a board
  at 100 rows.
- **The sort choice is not remembered** between sessions; Overall opens sorted
  by total XP.
- **Overall by total level is computed on your computer** from the six skill
  boards, because the server ranks Overall by XP only. It needs all six to have
  loaded (about 0.7 s); until then the XP order is shown with "Total ..".
- **Rank arrows are relative to your last visit on this computer.** A new
  profile, a deleted `ankiscape_hiscores_seen.json`, or a first visit shows no
  arrows.
- Opening Hiscores makes seven anonymous reads instead of one. Still no bearer
  token and no account identity, for any profile including "Play offline".
- The test-cohort leaderboard (server-reported test accounts only) has no
  Overall tab.
- Looked at by eye on macOS only. Windows, Linux (Qt5 and Qt6) and Anki 23.10
  are covered by the automated release-verify journeys (see `READINESS.md` for
  the run), not by anyone viewing the screen.
