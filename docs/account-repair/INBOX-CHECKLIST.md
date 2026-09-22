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
9. **Duplicate resend guard** — The first code is sent automatically when
   you reach the verification page: that is expected, signup sends it.
   Resend must then be disabled for 60 seconds (counting down), and
   nothing on the page may claim a code was sent when it wasn’t actually
   requested.
10. **Resend: same address, then edited address** — (a) On the
    verification page, click Resend WITHOUT editing: the status must say
    a new code was requested without promising delivery, and a code
    arrives at the address the signup was created with (subject to the
    email rate limit — if refused, the message must say when to retry).
    (b) Then edit the email field to another address you control and
    click Resend. If that address has NO pending signup (the
    typo-recovery case), the page must say so honestly — “No account is
    waiting for verification at that address” — never a phantom
    “requested” — and Back must return to registration keeping the
    fields. (The server answers 200 to any resend address, so this
    honesty comes from the add-on’s preflight check, not the server.)
11. **Fresh-start acknowledgement** — On a fresh profile the add-on opens
    straight into Evolved setup, so there is no Classic menu yet: first
    click **Continue Classic** (bottom-left of the Welcome screen), then
    from the Classic menu or Settings → Advanced click **Try Evolved**
    (this applies whenever no Evolved game has been activated yet).
    Expected: prominent "Evolved starts fresh." text with
    the acknowledgement checkbox; Try Evolved stays disabled until it is
    checked; canceling changes nothing. After Evolved is activated, later
    switches show no notice and never restart either game.
    (Navigation clarified 2026-09-21: "fresh profile → Classic menu" alone
    was unreachable — the fresh profile lands in Evolved setup.)
12. **Account deletion (deliberate throwaway)** — Use ONLY the throwaway
    account from step 1 (never a real-progress account). Settings → Account →
    Delete account… Expected: the window explains what is removed, requires
    your exact username and password, and defaults to keeping local progress.
    Check the local-removal box once to see the second warning, then uncheck
    it and confirm. Expected: "Account deleted.", login no longer works, the
    game is gone from the leaderboard, and local progress remains. If the
    reply is lost, the app says it could not confirm deletion and offers a
    read-only check — do not repeat the deletion manually.
13. **Deletion cleanup scope** — After deletion, confirm the old account
    cannot log in, that the local game still opens for offline play
    (keep-local choice), and that re-registering with the same email
    succeeds and yields a GENUINELY FRESH account — no old sessions,
    links, or rows attached. That freshness is the proof the cleanup was
    complete; a blocked re-registration is NOT expected (corrected
    2026-09-21: no spec ever required an email hold, and a hold would
    retain the very address deletion promises to erase).

If a step fails, note the screen, the exact message, and whether the header
said pending/offline; that is enough to reproduce without logs.
