# Wilson's inbox journey — AnkiScape account repair

Artifact: `dist/ankiscape-3.0.0.ankiaddon`, sha256 `b7b1f2bd…`. Hosted project
`vjqzamuogcughdvzskmf` has migrations 0008/0009, `account-status` and
`username-login` deployed. Steps 10–13 require the follow-up deploy: migration
0010 (deletion cleanup) plus the `account-delete` function with JWT
verification ON. Do not run step 12 against a server without it.

Only Wilson performs these steps; no agent sends email. Record the result for
each step (pass/fail + note). Use a real inbox you control. A fresh email
address is required for step 1.

1. **New email signup** — Settings → Account → Create account. Register with a
   new email + username. Expected: the verification page appears and a code
   arrives in the inbox (check spam too).
2. **Existing-email retry** — Register again with the SAME email. Expected:
   “An account already exists for this email. Log in or reset your password.”
   with Log in / Reset available. No code page, no second account.
3. **Delivered code verification** — Enter the code from step 1. Expected: the
   window closes, you are signed in, and progress links and starts syncing.
4. **Login** — Log out (Settings → Account), log back in with the same
   password. Expected: signed in; “Keep me signed in” behavior matches the
   checkbox.
5. **One review → online score** — Do one review. Expected: within ~10–15
   seconds the header changes from pending to synced, and your row appears on
   the Hiscores (mining or your training skill).
6. **Reset** — Forgot password → request a code → enter it with a new
   password. Expected: the reset window closes with “Password updated,” you
   are signed in automatically, and sync continues in the background.
7. **Old password fails / new works** — Log out; the old password is
   rejected, the new one signs in.
8. **Restart remembers session** — Restart Anki (Resume profile). Expected:
   still signed in without re-entering the password; pending drains.
9. **Duplicate resend guard** — On any verification page, Resend should be
   disabled for 60 seconds after a request and never claims a code was sent
   when it wasn’t requested.
10. **Resend route** — On the verification page, edit the email field to
    another address you control, click Resend, and check that the message
    arrives at the EDITED address (not the original). The status line should
    say a new code was requested without promising delivery. If the server
    refuses because of an email limit, the message must say when to retry.
11. **Fresh-start acknowledgement** — With a fresh profile (or before any
    Evolved game is activated), click Try Evolved from the Classic menu or
    Settings → Advanced. Expected: prominent "Evolved starts fresh." text with
    the acknowledgement checkbox; Try Evolved stays disabled until it is
    checked; canceling changes nothing. After Evolved is activated, later
    switches show no notice and never restart either game.
12. **Account deletion (deliberate throwaway)** — Use ONLY the throwaway
    account from step 1 (never a real-progress account). Settings → Account →
    Delete account… Expected: the window explains what is removed, requires
    your exact username and password, and defaults to keeping local progress.
    Check the local-removal box once to see the second warning, then uncheck
    it and confirm. Expected: "Account deleted.", login no longer works, the
    game is gone from the leaderboard, and local progress remains. If the
    reply is lost, the app says it could not confirm deletion and offers a
    read-only check — do not repeat the deletion manually.
13. **Deletion cleanup scope** — After deletion, confirm the account cannot
    log in or re-register with the same email as a fresh account owner, and
    that the local game still opens for offline play (keep-local choice).

If a step fails, note the screen, the exact message, and whether the header
said pending/offline; that is enough to reproduce without logs.
