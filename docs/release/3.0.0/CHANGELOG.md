# AnkiScape 3.0.0 — Evolved

Same AnkiWeb listing (1808450369) as the 2.x Classic releases. One-time
choice on first launch: **Try Evolved** or **Continue Classic**. Classic
stays frozen; Evolved starts fresh at level 1 / 0 XP (no stat transfer, for
hiscore fairness). Paths can be switched later in Settings → Advanced.

The first switch to Evolved now requires an explicit acknowledgement:
"Evolved starts fresh." — Classic levels, XP and items are **not**
transferred, each mode keeps its own progress, switching never restarts
either game, and there is no Classic-to-Evolved transfer. Returning players
with an activated Evolved game see no notice; canceling the notice changes
nothing.

## Added

- **Evolved game**: six skills (Mining, Woodcutting, Smithing, Crafting,
  Fishing, Cooking), materials, a read-only Bank, achievements, level-ups and
  an anchored XP HUD, all driven by real card answers.
- **Guided setup** that resumes after restarts until it is committed.
- **Nonmodal game shell** with icon rail: Training, Skills, Bank,
  Achievements, Hiscores, Settings, plus an offline Guide covering every
  resource, recipe, XP formula and level.
- **Server-authoritative hiscores** (optional account): one account links one
  Evolved game; desktop progress merges through the journal/outbox; scores
  are recomputed from stored operations.
- **Mobile review catch-up**: reviews synced from AnkiMobile/AnkiDroid earn
  catch-up rewards on desktop under the gathering preset.
- **Offline art**: all item/skill/nav imagery ships inside the add-on; no
  image requests while you play.
- **Backups & recovery**: Export/Restore Game Backup, persistent write-failure
  warning, and Undo/Redo retraction of rewards.
- **Support**: Report a bug from the Guide with an allowlisted diagnostic
  preview; GitHub issue templates.

## Changed

- Rewards are credited on accepted answers only; production pauses (zero
  reward, review still observed) when materials run out or a level is lost.
- XP retune: every eligible review earns XP; resource tiers scale it.
- Remembered sign-in uses the OS credential vault when one is available,
  otherwise session-only. Passwords are never stored.
- Anki compatibility remains 23.10+ on Qt5/Qt6 (macOS/Linux/Windows).
- A level-up that crosses several levels at once shows **one** dialog naming the
  level reached, instead of one dialog per level. Profiles upgraded from 2.x
  store levels and XP that can be out of step with each other, so a single
  review could otherwise open nineteen dialogs back to back.

## Account & identity (3.0.0 rework)

- **Two games coexist per profile.** The offline game born on this computer
  and the account game are separate: separate uuids, journals, XP and stats.
  One active-game pointer decides which is live. The offline game never
  uploads, even while signed in; the account game never imports the offline
  game's history.
- **Login activates the account game; logout activates the offline game.**
  Creating an account imports nothing and deletes nothing — the account game
  starts fresh at level 1 / 0 XP, stated before the user commits.
- **Server-owned game uuid.** `link_game` returns the account's game,
  creating one when absent; it never raises "game claimed"/"mismatch". The
  client offers a uuid only as an argument and adopts the returned one.
- **First run is a choice:** Create account (primary) or Play offline. No
  game is minted before the user picks a path.
- **Durable download-first sync.** A fresh desktop materializes the account
  game by downloading its history into a new journal before activating it.
- **Un-brick path.** An install with stray pending local operations drains
  its outbox (rows removed, operations kept, never marked acked, never
  uploaded) the next time an account is linked.
- **Public-board visibility.** A Settings control (shown only when the server
  advertises the capability) persists via `set_board_visibility`; it filters
  the public Hiscores only. The toggle is disclosed on the register notice.
- **Account deletion** is discoverable from Settings → Account; the copy
  distinguishes the account game from the offline game.
- **Email-limit 429 copy** names the email limit and asks for a spam check at
  every Retry-After value.

## Fixed

- Sync/hiscores contract failures found after 2.x: array-mode Hiscores,
  wrapper access-token delegation, player lookup, and rank handling.
- Mined gems are written to the Bank (jewelry crafting reachable); cooking
  obeys its level gate in every replay path.
- Recovery warning clears after a successful pending write.
- A reward is saved before any celebration dialog opens. Previously a level-up
  or achievement popup that failed could discard XP the review had already
  earned and abort the answer; the reward is durable first now, and the popups
  are best-effort.
