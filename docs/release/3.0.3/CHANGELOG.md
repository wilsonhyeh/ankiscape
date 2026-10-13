# AnkiScape 3.0.3 — switching skills works again

A bug-fix update on 3.0.2. Scoring, accounts, Hiscores and the sync protocol
are unchanged. Installing over 3.0.2 keeps all progress. There is no data
migration and no server change.

## Fixed

- **Could not switch back to a skill (AnkiWeb report).** AnkiScape remembers
  your last resource for each skill. When you opened a skill you had trained
  before, its remembered resource showed "Currently training" and the Train
  button was disabled, even though reviews were still paying into a
  different skill. A player with thousands of Shrimp could not start Cooking.
  Train is now enabled unless that skill *and* that resource are the ones
  actually being trained.
- **"Training" badge on the wrong tile.** The resource you were previewing
  showed a "Training" badge on any skill. It now appears only on the resource
  being trained.
- **Cooking named the wrong reward.** The Skills panel and the Training screen
  said a Cooking review earns "one Shrimp". It earns one Cooked Shrimp (or
  whichever fish), which is what the game has always given.

## Improved

- **The Skills screen shows which skill you are training.** The active skill's
  tile now reads "Level N (training)".

## Notes

- Who saw it: Evolved players who had trained a skill, moved to another, and
  tried to go back to the same resource. It hit early Cooking, Woodcutting,
  Smithing and Fishing hardest, where only one resource is unlocked.
- Anki compatibility is unchanged: 23.10+ on Qt5/Qt6 (macOS, Linux, Windows).
