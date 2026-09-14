# Wilson's inbox journey — AnkiScape account repair

Artifact: `dist/ankiscape-3.0.0.ankiaddon`, sha256 `da9600cf03…`, built from
commit `1b4ce7d` (+ the deployment fix commit). Hosted project
`vjqzamuogcughdvzskmf` has migrations 0008/0009, `account-status` and
`username-login` deployed.

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

If a step fails, note the screen, the exact message, and whether the header
said pending/offline; that is enough to reproduce without logs.
