# AnkiScape 3.0.1 — Hiscores redesign

A small update on the 3.0.0 base. The only shipped change is the Hiscores
screen; gameplay, sync, accounts and scoring are untouched. Installing over
3.0.0 keeps all progress. There is no data migration on the client.

## Changed — Hiscores

- **Overall board** in addition to one board per skill, in an icon tab strip.
  Overall ranks players by total XP across the six skills.
- **Sort Overall by total XP or by total level.** Level mode breaks ties by XP;
  players share a rank only when both are equal. The choice is not remembered
  between sessions.
- **One list for everyone.** Ranks 1-3 carry a Gold, Silver or Bronze bar after
  the name and a matching outline around the row. The row text also names the
  medal, so it is not colour-only.
- **Your standing** (signed in): a pinned bar with your rank and what the next
  rank costs, in XP, or in total levels when sorting by level.
- **Rank movement** since your last visit (up/down arrows). It is remembered in
  a small file in your Anki profile folder, `ankiscape_hiscores_seen.json`,
  which holds only public board data (usernames and ranks), is never uploaded
  and is safe to delete.
- **Player cards:** click anyone to see all six skills with levels, XP bars and
  ranks, compared with you.
- Players with 0 XP in a board are counted ("5 players haven't trained Mining
  yet") instead of listed as a long run of tied rows.
- Everything is sized from the text, so larger UI scales no longer clip.

## Server

- Migration `0013` (already live) lets the public `hiscores()` accept `overall`.
  It is read-only and additive; 3.0.0 clients are unaffected because every
  existing result is unchanged.

## Operations

- The public demo players were removed from production on 2026-09-29 and the
  hosted CI lane no longer re-creates them; it now fails if any reappear.

## Notes

- Opening Hiscores now reads seven boards (Overall plus six skills) anonymously
  instead of one, so ranks, levels and player cards are complete. Measured
  about 0.7 s in total against production.
- Anki compatibility is unchanged: 23.10+ on Qt5/Qt6 (macOS, Linux, Windows).
