# dev playground

One-command synthetic Anki playground. All state under gitignored `.dev/`;
reports under `artifacts/`. Never touches personal collections or production.

```bash
python3 dev.py launch --scenario fresh             # empty + mode chooser
python3 dev.py launch --scenario midgame           # Evolved Lv~40, 150 cards
python3 dev.py launch --scenario endgame           # Evolved Lv~225, 650 cards
python3 dev.py launch --scenario classic-upgrade   # Classic 2.0.2 data + chooser
python3 dev.py launch --scenario midgame --fresh   # wipe first (verified)
python3 dev.py launch --scenario midgame --symlink # live repo link (dev loop only)
python3 dev.py launch --scenario midgame --anki 23.10
python3 dev.py reset --scenario midgame
```

Double-click `Launch AnkiScape Dev.command` for a scenario picker, or run the
other commands below.

```bash
python3 dev.py setup
python3 dev.py launch --scenario fresh
python3 dev.py launch --scenario midgame
python3 dev.py launch --scenario endgame
python3 dev.py launch --scenario classic-upgrade
python3 dev.py reset --scenario midgame
python3 dev.py test --suite python
python3 dev.py test --suite backend
python3 dev.py test --suite e2e --anki 26.8.1 --qt 6
python3 dev.py verify --release
python3 dev.py verify-evidence --matrix dev/matrix.json
```

Journeys: `--journey fresh|upgrade|undo|catchup` (real-Anki Qt),
`--journey sync` (live local-stack service sync, no mocks),
`--journey dialogs` (live Qt menu + account screens).

Prod release-candidate path (publication gate; Wilson authorizes):
```bash
python3 scripts/make_prod_config.py --url https://<ref>.supabase.co \
  --anon-key <sb_publishable_...>   # public key only; refuses privileged
python3 scripts/build_addon.py      # stamps prod_endpoint_baked: true
ANKISCAPE_PROD_URL=... ANKISCAPE_PROD_ANON_KEY=... \
  SUPABASE_SERVICE_ROLE_KEY=... python3 dev/prod_e2e.py  # hosted stack, no real email
```

`launch` seeds a synthetic base at `.dev/anki/<scenario>` (marker-checked)
and opens managed Anki with `-b <base> -p dev-<scenario>`. A visible DEV label
shows the scenario and backend. No hosted links, real SMTP, or AnkiWeb login.

`reset` only deletes marker-bearing scenario dirs, refuses symlinks escapes,
default personal paths, and running/locked collections. It stops only
processes owned by the invocation and detects single-instance forwarding.

macOS double-click: `Launch AnkiScape Dev.command` (opens Terminal into the
repo and runs the documented entry point with proper quoting).
