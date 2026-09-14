# dev playground

One-command Anki playground + production rehearsal. All state under
gitignored `.dev/`; reports under `artifacts/`. Synthetic scenarios never
touch personal collections or production services; the `user-*` journeys
deliberately run the packaged prod artifact against the production backend.

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

The synthetic scenarios above are CLI-only. Double-click
`Launch AnkiScape.command` for the production rehearsal picker, or run the
`user-*` commands in the section below.

## Production rehearsal (what users get)

`Launch AnkiScape.command` — or `dev.py launch --scenario user-...` —
installs the packaged prod artifact into one shared isolated base
(`.dev/anki/user`, profile `dev-user`) pointed at the production backend:

```bash
python3 dev.py launch --scenario user-new       # wipe + first-launch journey
python3 dev.py launch --scenario user-upgrade   # wipe + 2.0.2 -> 3.0 upgrade
python3 dev.py launch --scenario user-resume    # relaunch, keep all state
```

- The artifact is reused when `dist/` matches the source (deterministic
  build) and rebuilt automatically when shipped bytes changed; the launcher
  prints the artifact hash it installed.
- `evolved/prod_config.py` must exist (public anon key baked) or the
  launcher refuses. Nothing sets `ANKISCAPE_DEV`: no DEV label, no local
  stack — the baked production endpoint is used exactly like a release.
- A dev-only seeder supplies a starter deck (plus the 2.0.2-shape Classic
  fixture for the upgrade journey). The packaged add-on itself is exact
  release bits; OTP emails you trigger are real sends to your own address,
  and each wiped profile registers its own account.

```bash
python3 dev.py setup
python3 dev.py test --suite python
python3 dev.py test --suite backend
python3 dev.py test --suite e2e --anki 26.8.1 --qt 6
python3 dev.py verify --release
python3 dev.py verify-evidence --matrix dev/matrix.json
```

Journeys: `--journey fresh|upgrade|undo|catchup` (real-Anki Qt),
`--journey sync` (live local-stack service sync, no mocks),
`--journey dialogs` (live Qt menu + account screens),
`--journey ui-onboarding|ui-training|ui-settings|ui-review|ui-lifecycle`
(3.0 shell journeys), and `--journey ui-art` (offline art/UI verification:
installs the package, denies and records image network access, asserts every
manifest asset decodes with visible alpha at real slot sizes, visits every
tab, and re-checks after a restart with an unchanged installed tree).
`--journey ui-account-lifecycle` drives the real account window (register,
verify, link, review, drain, recovery, logged-out browsing) against a
deterministic loopback fixture by default, or against the real local Auth
stack + captured loopback OTP when `ANKISCAPE_ACCOUNT_JOURNEY_MODE=auth`.
`python3 dev/account_contracts_e2e.py --local` runs the real account/service
journey (ProfileSession + vault + real transport) against the local stack.

Prod release-candidate path (publication gate; Wilson authorizes):
```bash
python3 scripts/make_prod_config.py --url https://<ref>.supabase.co \
  --anon-key <sb_publishable_...>   # public key only; refuses privileged
python3 scripts/build_addon.py      # stamps prod_endpoint_baked: true
ANKISCAPE_PROD_URL=... ANKISCAPE_PROD_ANON_KEY=... \
  SUPABASE_SERVICE_ROLE_KEY=... python3 dev/prod_e2e.py  # hosted stack, no real email
```

Synthetic `launch` seeds a base at `.dev/anki/<scenario>` (marker-checked)
and opens managed Anki with `-b <base> -p dev-<scenario>`; a visible DEV label
shows the scenario and backend, with no hosted links, real SMTP, or AnkiWeb
login. The `user-*` journeys use `.dev/anki/user` / `dev-user` instead.

`reset` only deletes marker-bearing scenario dirs, refuses symlinks escapes,
default personal paths, and running/locked collections. It stops only
processes owned by the invocation and detects single-instance forwarding.

macOS double-click: `Launch AnkiScape.command` — production rehearsal picker
(opens Terminal into the repo and runs the documented entry point).

## Reliability tooling (dev only, never shipped)

| Tool | Purpose |
|---|---|
| `reliability.py` | Orchestrates stages, writes/validates evidence records, budget checks |
| `perf_runtime.py` | Engine/worker latency, rebuild and memory measurements |
| `endurance.py` | Paced active run with RSS/object/thread bounds |
| `generated_traces.py` | Deterministic operation-sequence property checks |
| `mutation_gate.py` | Injects defects into temp copies; owning tests must fail |
| `runtime_adapter.py` | Platform runtime resolution, owned-process policy, path normalization |
| `fetch_runtimes.py` | Verified official runtime downloads (fails closed on unpinned targets) |
| `demo_traces.py` | Deterministic traces for the five public demo players |
| `demo_players.py` | Plan/apply/verify the five public demo players + retire surplus fixtures |
| `account_journey_e2e.py` | Per-target account-journey records and `--require-all-targets` aggregation |
| `ui_ux_verify.py` | Runs every UI scenario on a managed runtime |

See `docs/RELIABILITY.md` for commands, prerequisites and triage.
