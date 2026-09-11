# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem and never include secrets,
tokens, account identifiers or collection data anywhere public.

Use GitHub's private vulnerability reporting on this repository:

1. Open the **Security** tab of
   [wilsonhyeh/ankiscape](https://github.com/wilsonhyeh/ankiscape).
2. Choose **Report a vulnerability** and describe the issue, impact and a
   minimal reproduction.

If that button is not available, private vulnerability reporting has not been
enabled yet; open a minimal public issue that only asks the maintainer to
enable a private channel, without any sensitive detail.

## Scope

In scope: the add-on package in this repository, its local journal and backup
files, the documented Supabase schema/RPCs under `server/`, and credential
handling.

Out of scope: third-party Anki add-ons, Anki itself, Jagex services, and
issues that require modifying the user's machine or account.

## What to expect

- Acknowledgement as soon as practical; this is a volunteer project.
- A fix or a documented mitigation before public disclosure.
- Credit in release notes if you want it.

## Guidelines for users

- Keep Anki and AnkiScape updated.
- Never share `collection.anki2`, `game.sqlite3`, exported backups or
  diagnostic dumps publicly without reviewing them.
- The add-on never asks for your password outside Anki's login flow and never
  stores it in plain text.
