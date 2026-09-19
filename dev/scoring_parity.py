#!/usr/bin/env python3
"""dev/scoring_parity.py - Python vs. PostgreSQL scoring parity (--local).

Executes the same operation vectors through the Python reducer and a real
local PostgreSQL instance (the running Supabase stack's database), then
compares the authoritative state exactly: XP, levels, inventory, counters,
achievements, material-conflict adjustments, and per-review outcomes.

An API integration case then signs in a synthetic confirmed user, links a
game through the real RPCs, submits real operations over REST, and verifies
hiscores/public_profile moved without any direct game_state fixture writes.

Exit codes: 0 = parity proven; nonzero = missing runtime, mismatch, or error.
Never touches production; uses the local stack only. All synthetic rows are
removed at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evolved.data import load_rules                      # noqa: E402
from evolved import reviews as reviews_mod               # noqa: E402
from evolved.reducer import replay as py_replay          # noqa: E402

MIGRATION = os.path.join(ROOT, "server", "supabase", "migrations",
                         "0004_authoritative_scoring.sql")
MIGRATIONS = (MIGRATION,
              os.path.join(ROOT, "server", "supabase", "migrations",
                           "0005_gem_inventory_grant.sql"))
API_URL = "http://127.0.0.1:55321"
NS = uuid.NAMESPACE_URL
PW = "parity-pass-1"
UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class ParityError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Local stack plumbing
# --------------------------------------------------------------------------

def db_container() -> str:
    try:
        proc = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParityError(f"docker unavailable: {exc!r}")
    names = [line.strip() for line in (proc.stdout or "").splitlines()
             if line.strip().startswith("supabase_db")]
    if not names:
        raise ParityError("no running supabase_db container; start the local "
                          "stack (dev.py spawns it) first")
    return names[0]


def psql(container: str, sql: str, *, check: bool = True) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "postgres",
         "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-tA", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=180)
    if check and proc.returncode != 0:
        raise ParityError(f"psql failed:\n{(proc.stderr or proc.stdout)[:2000]}")
    return (proc.stdout or "").strip()


def apply_migration(container: str) -> None:
    """Apply every replay-affecting migration in order (idempotent)."""
    for path in MIGRATIONS:
        with open(path, encoding="utf-8") as fh:
            try:
                psql(container, fh.read())
            except ParityError as exc:
                raise ParityError(f"{os.path.basename(path)}: {exc}") from exc


def api_anon_key() -> str:
    proc = subprocess.run(
        ["supabase", "status", "-o", "env"], capture_output=True, text=True,
        timeout=60, cwd=os.path.join(ROOT, "server"))
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ANON_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise ParityError("could not read local ANON_KEY via supabase status")


# --------------------------------------------------------------------------
# Synthetic identities / games
# --------------------------------------------------------------------------

def uid_for(name: str) -> str:
    return str(uuid.uuid5(NS, f"ankiscape-parity-{name}"))


def ensure_user_sql(user_id: str, username: str, *, confirmed: bool) -> str:
    """Idempotent synthetic user; the username trigger creates players."""
    email = f"{username}@parity.local"
    return f"""
do $$
begin
  if not exists (select 1 from auth.users where id = '{user_id}'::uuid) then
    insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
      email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at, updated_at,
      confirmation_token, recovery_token, email_change_token_new, email_change,
      email_change_token_current, phone_change, phone_change_token,
      reauthentication_token)
    values (
      '{user_id}'::uuid,
      '00000000-0000-0000-0000-000000000000'::uuid,
      'authenticated', 'authenticated', '{email}',
      extensions.crypt('{PW}', extensions.gen_salt('bf')),
      case when {str(confirmed).lower()} then now() else null end,
      '{{"provider":"email","providers":["email"]}}'::jsonb,
      jsonb_build_object('username_norm', '{username}',
                         'username_display', '{username}'),
      now(), now(),
      '', '', '', '', '', '', '', '');
  else
    update auth.users set
      encrypted_password = extensions.crypt('{PW}', extensions.gen_salt('bf')),
      email_confirmed_at = case when {str(confirmed).lower()} then now()
                                else email_confirmed_at end
    where id = '{user_id}'::uuid;
  end if;
end $$;
"""


def reset_user_sql(user_id: str, username: str) -> str:
    return f"""
delete from public.game_state where user_id = '{user_id}'::uuid;
delete from public.game_operations where user_id = '{user_id}'::uuid;
delete from public.players where user_id = '{user_id}'::uuid
   or username_norm = '{username}';
delete from auth.users where id = '{user_id}'::uuid;
"""


def bind_game_sql(user_id: str, game_uuid: str, username: str) -> str:
    """Direct fixture binding for SQL-vector games (not used by the API case)."""
    return f"""
insert into public.players(user_id, username_norm, username_display, game_uuid)
values ('{user_id}'::uuid, '{username}', '{username}', '{game_uuid}'::uuid)
on conflict (user_id) do update set game_uuid = excluded.game_uuid;
insert into public.game_state(game_uuid, user_id, revision, xp, inventory,
  achievements, counters)
values ('{game_uuid}'::uuid, '{user_id}'::uuid, 0,
  jsonb_build_object('mining','0','woodcutting','0','smithing','0',
                     'crafting','0','fishing','0','cooking','0'),
  '{{}}'::jsonb, '[]'::jsonb, '{{}}'::jsonb)
on conflict (game_uuid) do update set user_id = excluded.user_id;
delete from public.game_operations where game_uuid = '{game_uuid}'::uuid;
"""


def insert_ops_sql(game_uuid: str, user_id: str, ops: list) -> str:
    seen = set()
    rows = []
    for op in ops:
        if op["op_id"] in seen:
            continue  # duplicate delivery: same identity, inserted once
        seen.add(op["op_id"])
        payload = json.dumps(op["payload"], sort_keys=True, separators=(",", ":"))
        rows.append(
            "('{op}', '{game}'::uuid, '{user}'::uuid, '{dev}', {seq}, {lam}, "
            "'{kind}', '{payload}'::jsonb, '')".format(
                op=op["op_id"], game=game_uuid, user=user_id,
                dev=op["device_id"], seq=int(op["device_seq"]),
                lam=int(op["lamport"]), kind=op["kind"],
                payload=payload.replace("'", "''")))
    if not rows:
        return "select 1;"
    return ("insert into public.game_operations(op_id, game_uuid, user_id,"
            " device_id, device_seq, lamport, kind, payload, payload_hash)"
            " values " + ",".join(rows) + ";")


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------

def _op(game: str, device: str, seq: int, lamport: int, kind: str,
        payload: dict) -> dict:
    return {"op_id": str(uuid.uuid5(NS, f"{game}:{device}:{seq}:{kind}")),
            "game_uuid": game, "device_id": device, "device_seq": seq,
            "lamport": lamport, "kind": kind, "payload": payload}


def _direct(game: str, device: str, seq: int, lamport: int, rid: int,
            skill: str, resource: str, policy: int = 2, ease: int = 3) -> dict:
    key = reviews_mod.make_review_key(game, rid, rid + 1000)
    return _op(game, device, seq, lamport, "review_award", {
        "review_key": key, "review_ts": 1_800_000_000 + seq, "rating": ease,
        "review_kind": "review", "provenance": "direct",
        "reward_policy": policy, "skill": skill, "resource": resource})


def _catchup(game: str, device: str, seq: int, lamport: int, rid: int,
             policy: int = 2) -> dict:
    key = reviews_mod.make_review_key(game, rid, rid + 2000)
    return _op(game, device, seq, lamport, "review_award", {
        "review_key": key, "review_ts": 1_800_000_000 + seq, "rating": 3,
        "review_kind": "review", "provenance": "catchup",
        "reward_policy": policy})


def build_vectors() -> list:
    out = []

    # 1. Mixed policy across all six skills (historical + new).
    g = str(uuid.uuid5(NS, "parity-mixed"))
    ops = [
        _direct(g, "dev-a", 1, 1, 101, "mining", "Rune essence", policy=2),
        _direct(g, "dev-a", 2, 2, 102, "woodcutting", "Tree", policy=2),
        _direct(g, "dev-a", 3, 3, 103, "smithing", "Bronze bar", policy=2),
        _direct(g, "dev-a", 4, 4, 104, "crafting", "Soft clay", policy=2),
        _direct(g, "dev-a", 5, 5, 105, "fishing", "Shrimp", policy=2),
        _direct(g, "dev-a", 6, 6, 106, "cooking", "Shrimp", policy=1),
        _direct(g, "dev-a", 7, 7, 107, "smithing", "Bronze bar", policy=1),
    ]
    out.append({"name": "mixed_policy", "game": g, "ops": ops})

    # 2. 200-operation batch.
    g = str(uuid.uuid5(NS, "parity-batch"))
    ops = [_direct(g, "dev-b", i + 1, i + 1, 5000 + i, "mining",
                   "Rune essence", policy=2) for i in range(200)]
    out.append({"name": "batch_200", "game": g, "ops": ops})

    # 3. Late arrival: canonical order differs from arrival order.
    g = str(uuid.uuid5(NS, "parity-late"))
    ops = [
        _direct(g, "dev-c", 5, 50, 301, "mining", "Copper ore", policy=2),
        _direct(g, "dev-c", 1, 10, 302, "mining", "Rune essence", policy=2),
        _direct(g, "dev-c", 3, 30, 303, "mining", "Tin ore", policy=2),
        _direct(g, "dev-c", 2, 20, 304, "mining", "Clay", policy=2),
    ]
    out.append({"name": "late_arrival", "game": g, "ops": ops})

    # 4. Duplicate delivery (same op_id twice in the vector).
    g = str(uuid.uuid5(NS, "parity-dup"))
    d1 = _direct(g, "dev-d", 1, 1, 401, "mining", "Rune essence", policy=2)
    ops = [d1, dict(d1)]
    out.append({"name": "duplicate_delivery", "game": g, "ops": ops})

    # 5. Shared-material conflict: two Smithing claims, one Copper/Tin set.
    g = str(uuid.uuid5(NS, "parity-conflict"))
    ops = [
        _direct(g, "dev-e", 1, 1, 501, "mining", "Copper ore", policy=2),
        _direct(g, "dev-e", 2, 2, 502, "mining", "Tin ore", policy=2),
        _direct(g, "dev-e", 3, 3, 503, "smithing", "Bronze bar", policy=2),
        _direct(g, "dev-e", 4, 4, 504, "smithing", "Bronze bar", policy=2),
        _direct(g, "dev-e", 5, 5, 505, "smithing", "Bronze bar", policy=1),
    ]
    out.append({"name": "material_conflict", "game": g, "ops": ops})

    # 6. Undo/restore around a credited review.
    g = str(uuid.uuid5(NS, "parity-undo"))
    award = _direct(g, "dev-f", 1, 1, 601, "mining", "Rune essence", policy=2)
    key = award["payload"]["review_key"]
    ops = [
        award,
        _op(g, "dev-f", 2, 2, "review_retract",
            {"target_review_key": key, "reason": "anki_undo"}),
        _op(g, "dev-f", 3, 3, "review_restore",
            {"target_review_key": key, "reason": "anki_redo"}),
    ]
    out.append({"name": "undo_restore", "game": g, "ops": ops})

    # 7. Catch-up preset timeline + skip precedence over catch-up.
    g = str(uuid.uuid5(NS, "parity-catchup"))
    skipped_key = reviews_mod.make_review_key(g, 707, 1707)
    skipped_award = {"op_id": str(uuid.uuid5(NS, f"{g}:skipcatchup")),
                     "game_uuid": g, "device_id": "dev-g", "device_seq": 9,
                     "lamport": 9, "kind": "review_award",
                     "payload": {"review_key": skipped_key,
                                 "review_ts": 1_800_000_100, "rating": 3,
                                 "review_kind": "review", "provenance": "catchup",
                                 "reward_policy": 2}}
    ops = [
        _op(g, "dev-g", 1, 1, "catchup_preset",
            {"skill": "fishing", "effective_ts": 1_800_000_000}),
        _catchup(g, "dev-g", 2, 2, 701),
        _catchup(g, "dev-g", 3, 3, 702),
        skipped_award,
        _op(g, "dev-g", 4, 10, "review_skip",
            {"review_key": skipped_key, "reason": "classic_mode",
             "provenance": "skip"}),
        _direct(g, "dev-g", 5, 11, 703, "fishing", "Shrimp", policy=2),
    ]
    out.append({"name": "catchup_skip_precedence", "game": g, "ops": ops})

    # 8. Guaranteed gem drop on a tier-2 ore (rid 3807 of parity-gem draws a
    # sapphire). Proves the gem bonus XP uses the selected ore's multiplier
    # on both sides: Clay 5 * 1.05 = 5.25 plus Uncut sapphire 50 * 1.05 = 52.5.
    g = str(uuid.uuid5(NS, "parity-gem"))
    ops = [
        _direct(g, "dev-h", 1, 1, 3807, "mining", "Clay", policy=2),
        _direct(g, "dev-h", 2, 2, 4701, "mining", "Clay", policy=2),
    ]
    out.append({"name": "gem_bonus_multiplier", "game": g, "ops": ops})

    # 9. Cooking level gate: a locked fish pauses under policy 2 even with
    # no materials, and legacy policy 1 earns practice XP consuming nothing.
    g = str(uuid.uuid5(NS, "parity-cookgate"))
    ops = [
        _direct(g, "dev-i", 1, 1, 9001, "cooking", "Trout", policy=2),
        _direct(g, "dev-i", 2, 2, 9002, "cooking", "Trout", policy=1),
    ]
    out.append({"name": "cooking_level_gate", "game": g, "ops": ops})
    return out


# --------------------------------------------------------------------------
# SQL-side replay
# --------------------------------------------------------------------------

def sql_replay(container: str, game: str, user: str, ops: list, *,
               rebind: bool = True) -> dict:
    if rebind:
        psql(container, bind_game_sql(user, game, f"parity_sql_{game[:8]}"))
    psql(container, insert_ops_sql(game, user, ops))
    raw = psql(container,
               f"select public.evolved_replay('{game}'::uuid)::text;")
    line = raw.splitlines()[-1] if raw else ""
    if not line:
        raise ParityError(f"empty replay output for {game}")
    return json.loads(line)


def py_state(game: str, ops: list) -> dict:
    return py_replay(ops, load_rules(), game)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _normalize_outcome(value) -> dict:
    return {"skill": str(value.get("skill", "")),
            "xp_micro": int(value.get("xp_micro", 0)),
            "rewarded": bool(value.get("rewarded")),
            "outcome": str(value.get("outcome", ""))}


def compare(name: str, py: dict, sql: dict) -> list:
    problems = []
    keys = ("levels", "inventory", "counters", "achievements", "adjustments")
    for key in keys:
        if key == "achievements":
            a, b = sorted(py.get(key) or []), sorted(sql.get(key) or [])
        elif key == "adjustments":
            a, b = sorted(py.get(key) or []), sorted(sql.get(key) or [])
        else:
            a, b = py.get(key) or {}, sql.get(key) or {}
        if a != b:
            problems.append(f"{name}: {key} mismatch\n  py : {a}\n  sql: {b}")
    py_xp = {k: str(v) for k, v in (py.get("xp_micro") or {}).items()}
    sql_xp = {k: str(v) for k, v in (sql.get("xp_micro") or {}).items()}
    if py_xp != sql_xp:
        problems.append(f"{name}: xp_micro mismatch\n  py : {py_xp}\n  sql: {sql_xp}")
    py_out = {k: _normalize_outcome(v) for k, v in
              (py.get("review_outcomes") or {}).items()}
    sql_out = {k: _normalize_outcome(v) for k, v in
               (sql.get("review_outcomes") or {}).items()}
    if py_out != sql_out:
        diff_keys = sorted(set(py_out) | set(sql_out))
        detail = {k: {"py": py_out.get(k), "sql": sql_out.get(k)}
                  for k in diff_keys if py_out.get(k) != sql_out.get(k)}
        problems.append(f"{name}: review_outcomes mismatch\n  {detail}")
    return problems


# --------------------------------------------------------------------------
# API integration case
# --------------------------------------------------------------------------

def _http(method: str, url: str, *, payload=None, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2 * 1024 * 1024)
            text = raw.decode("utf-8")
            return resp.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read(1 * 1024 * 1024)
        text = raw.decode("utf-8", "replace")
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        return exc.code, body


def api_case(container: str, anon: str) -> None:
    username = "parity_api"
    user_id = uid_for(username)
    offered_game = str(uuid.uuid5(NS, "parity-api-game"))
    psql(container, reset_user_sql(user_id, username))
    psql(container, ensure_user_sql(user_id, username, confirmed=True))

    status, data = _http(
        "POST", f"{API_URL}/auth/v1/token?grant_type=password",
        payload={"email": f"{username}@parity.local", "password": PW},
        headers={"apikey": anon})
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise ParityError(f"API login failed: {status} {str(data)[:300]}")
    token = data["access_token"]

    auth = {"apikey": anon, "Authorization": f"Bearer {token}",
            "X-AnkiScape-Protocol": "2"}
    status, data = _http("POST", f"{API_URL}/rest/v1/rpc/link_game",
                         payload={"p_game_uuid": offered_game}, headers=auth)
    if status != 200:
        raise ParityError(f"link_game failed: {status} {str(data)[:300]}")
    # D5: the server owns the game uuid. Adopt the returned uuid as the only
    # game id used afterwards (S11/S16); created/resumed are pinned
    # complementary and `created` stays a key-presence rule.
    reply = data if isinstance(data, dict) else {}
    if reply.get("created") is not True or reply.get("resumed") is not False:
        raise ParityError(f"link_game reply violates the pin: {str(data)[:300]}")
    game = str(reply.get("game_uuid") or "")
    if not UUID_RE.match(game) or game == offered_game:
        raise ParityError(f"link_game returned no adoptable uuid: {str(data)[:300]}")

    ops = []
    for i in range(6):
        op = _direct(game, "api-dev", i + 1, i + 1, 9000 + i, "mining",
                     "Rune essence", policy=2)
        ops.append({k: op[k] for k in
                    ("op_id", "device_id", "device_seq", "lamport", "kind",
                     "payload")})
    status, data = _http("POST", f"{API_URL}/rest/v1/rpc/submit_operations",
                         payload={"p_game_uuid": game, "p_ops": ops},
                         headers=auth)
    if status != 200 or not isinstance(data, dict):
        raise ParityError(f"submit failed: {status} {str(data)[:300]}")
    if int(data.get("applied", -1)) != len(ops):
        raise ParityError(f"submit applied {data.get('applied')} != {len(ops)}")

    # Exact retry: accepted, applied 0, revision unchanged.
    status, state_before = _http(
        "POST", f"{API_URL}/rest/v1/rpc/get_game_state",
        payload={"p_game_uuid": game}, headers=auth)
    if status != 200:
        raise ParityError(f"get_game_state failed: {status} {state_before}")
    rev_before = int((state_before or {}).get("revision", -1))
    status, data = _http("POST", f"{API_URL}/rest/v1/rpc/submit_operations",
                         payload={"p_game_uuid": game, "p_ops": ops},
                         headers=auth)
    if status != 200 or int(data.get("applied", -1)) != 0:
        raise ParityError(f"exact retry moved state: {status} {str(data)[:300]}")
    status, state_after = _http(
        "POST", f"{API_URL}/rest/v1/rpc/get_game_state",
        payload={"p_game_uuid": game}, headers=auth)
    rev_after = int((state_after or {}).get("revision", -2))
    if rev_after != rev_before:
        raise ParityError(f"retry advanced revision {rev_before}->{rev_after}")

    # Unsupported policy is an explicit error, never a silent success.
    bad = dict(ops[0])
    bad["op_id"] = str(uuid.uuid5(NS, "parity-api-bad"))
    bad["device_seq"] = 99
    bad["payload"] = dict(bad["payload"], reward_policy=3)
    status, data = _http("POST", f"{API_URL}/rest/v1/rpc/submit_operations",
                         payload={"p_game_uuid": game, "p_ops": [bad]},
                         headers=auth)
    if status == 200:
        raise ParityError("unsupported policy 3 was accepted")

    # Hiscores + public_profile move with real submissions.
    status, rows = _http("POST", f"{API_URL}/rest/v1/rpc/hiscores",
                         payload={"p_skill": "mining", "p_limit": 50},
                         headers={"apikey": anon})
    if status != 200 or not isinstance(rows, list):
        raise ParityError(f"hiscores failed: {status} {str(rows)[:300]}")
    mine = next((r for r in rows if r.get("username") == username), None)
    if mine is None:
        raise ParityError("submitted player missing from hiscores")
    if int(str(mine.get("xp", "0"))) <= 0:
        raise ParityError("hiscores XP did not move after submissions")

    status, profile = _http(
        "POST", f"{API_URL}/rest/v1/rpc/public_profile",
        payload={"p_username_norm": username},
        headers={"apikey": anon})
    if status != 200 or not isinstance(profile, dict):
        raise ParityError(f"public_profile failed: {status} {str(profile)[:300]}")
    profile_xp = str(((profile.get("state") or {}).get("xp") or {})
                     .get("mining", "0"))
    if int(profile_xp) <= 0:
        raise ParityError("public_profile mining XP is still zero")

    # Retraction reverses the score through the same authoritative path.
    retract = {"op_id": str(uuid.uuid5(NS, "parity-api-retract")),
               "device_id": "api-dev", "device_seq": 50, "lamport": 50,
               "kind": "review_retract",
               "payload": {"target_review_key":
                           ops[0]["payload"]["review_key"],
                           "reason": "api_parity"}}
    status, data = _http("POST", f"{API_URL}/rest/v1/rpc/submit_operations",
                         payload={"p_game_uuid": game, "p_ops": [retract]},
                         headers=auth)
    if status != 200 or int(data.get("applied", -1)) != 1:
        raise ParityError(f"retract failed: {status} {str(data)[:300]}")
    status, profile2 = _http(
        "POST", f"{API_URL}/rest/v1/rpc/public_profile",
        payload={"p_username_norm": username}, headers={"apikey": anon})
    profile_xp2 = str(((profile2.get("state") or {}).get("xp") or {})
                      .get("mining", "0"))
    if int(profile_xp2) <= 0:
        # The first review was credited on the sixth answer's replay; retracting
        # it must still leave other credited reviews positive.
        raise ParityError("retraction zeroed unrelated rewards")
    if int(profile_xp2) >= int(profile_xp):
        raise ParityError(f"retraction did not reduce XP ({profile_xp}->{profile_xp2})")

    psql(container, reset_user_sql(user_id, username))


# --------------------------------------------------------------------------
# Rules snapshot + tier-multiplier parity (every resource, every tier)
# --------------------------------------------------------------------------

_TABLES = (("mining", "base_xp", "ores"),
           ("woodcutting", "base_xp", "trees"),
           ("fishing", "fishing_base_xp", "fish"),
           ("cooking", "cooking_base_xp", "fish"),
           ("smithing", "base_xp", "bars"),
           ("crafting", "base_xp", "crafting"))


def rules_snapshot_parity() -> list:
    """The embedded evolved_rules() literal must equal shared/rules-v1.json.

    Any drift silently changes server XP versus the client's expectations.
    Pure file check: works even when the database is unavailable.
    """
    from evolved.logic_pure import multiplied_base_micro

    problems = []
    with open(MIGRATION, encoding="utf-8") as fh:
        sql_text = fh.read()
    match = re.search(r"\$rules\$(.*?)\$rules\$", sql_text, re.S)
    if not match:
        return ["embedded $rules$ literal not found in migration 0004"]
    try:
        embedded = json.loads(match.group(1))
    except ValueError as exc:
        return [f"embedded rules literal is not valid JSON: {exc!r}"]
    file_rules = load_rules()
    if embedded != file_rules:
        problems.append("embedded evolved_rules() != shared/rules-v1.json")
    rules = file_rules
    for skill, xp_key, table in _TABLES:
        for entry in rules.get(table, []):
            base, tier = entry.get(xp_key), entry.get("tier")
            try:
                multiplied_base_micro(base, int(tier))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{skill}/{entry.get('display')}: "
                                f"multiplier raises {exc!r}")
    return problems


def sql_multiplier_parity(container: str) -> list:
    """Every resource row's multiplied and 25% (fail/burn) XP, SQL vs Python.

    Exercises all tiers including the 2.0 cap (tiers 21+), the Decimal
    base-XP conversion and the half-up rounding in one comparison.
    """
    from evolved.logic_pure import multiplied_base_micro, quarter_half_up

    query = """
with tables(skill, xp_key, table_name) as (
  values ('mining','base_xp','ores'),
         ('woodcutting','base_xp','trees'),
         ('fishing','fishing_base_xp','fish'),
         ('cooking','cooking_base_xp','fish'),
         ('smithing','base_xp','bars'),
         ('crafting','base_xp','crafting')
)
select t.skill || '|' || (v->>'display') || '|' || (v->>'tier') || '|' ||
       public._evolved_mult_micro(
         public._evolved_base_micro(v->t.xp_key), (v->>'tier')::int) || '|' ||
       public._evolved_mul_half_up(
         public._evolved_mult_micro(
           public._evolved_base_micro(v->t.xp_key),
           (v->>'tier')::int), 1, 4)
from tables t
cross join lateral (select public.evolved_rules() as r) rr
cross join lateral jsonb_array_elements(rr.r->t.table_name) v
order by t.skill, (v->>'tier')::int;
"""
    sql_rows = {}
    for line in psql(container, query).splitlines():
        if not line.strip():
            continue
        skill, display, tier, micro, quarter = line.split("|")
        sql_rows[(skill, display, int(tier))] = (int(micro), int(quarter))
    rules = load_rules()
    problems = []
    seen = set()
    for skill, xp_key, table in _TABLES:
        for entry in rules.get(table, []):
            display, tier = str(entry.get("display")), int(entry["tier"])
            key = (skill, display, tier)
            seen.add(key)
            want_micro = multiplied_base_micro(entry.get(xp_key), tier)
            want_quarter = quarter_half_up(want_micro)
            got = sql_rows.get(key)
            if got != (want_micro, want_quarter):
                problems.append(
                    f"{skill}/{display} tier {tier}: sql={got} "
                    f"python={(want_micro, want_quarter)}")
    for key in set(sql_rows) - seen:
        problems.append(f"sql returned unknown row {key}")
    return problems


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scoring_parity.py")
    parser.add_argument("--local", action="store_true",
                        help="run against the local Supabase PostgreSQL stack")
    args = parser.parse_args(argv)
    if not args.local:
        print("scoring_parity: pass --local (hosted parity is out of scope)",
              file=sys.stderr)
        return 2

    try:
        container = db_container()
    except ParityError as exc:
        print(f"scoring_parity: ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"scoring_parity: database container {container}")
    try:
        apply_migration(container)
    except ParityError as exc:
        print(f"scoring_parity: ERROR applying migrations: {exc}", file=sys.stderr)
        return 1

    failures = []
    try:
        problems = rules_snapshot_parity()
        if problems:
            failures.extend(problems)
            print("scoring_parity: rules_snapshot: MISMATCH")
        else:
            rows = sum(len(load_rules().get(t, []))
                       for _, _, t in _TABLES)
            print(f"scoring_parity: rules_snapshot: ok (file == embedded, "
                  f"{rows} resource rows)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"rules_snapshot: {exc!r}")
        print(f"scoring_parity: rules_snapshot: ERROR {exc!r}")
    try:
        problems = sql_multiplier_parity(container)
        if problems:
            failures.extend(problems)
            print("scoring_parity: multiplier: MISMATCH")
        else:
            rows = sum(len(load_rules().get(t, []))
                       for _, _, t in _TABLES)
            print(f"scoring_parity: multiplier: ok (all {rows} rows, "
                  f"tiers incl. 2x cap)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"multiplier: {exc!r}")
        print(f"scoring_parity: multiplier: ERROR {exc!r}")
    for vector in build_vectors():
        game, ops = vector["game"], vector["ops"]
        user = uid_for(f"sqltest{vector['name']}")
        username = f"parity_{vector['name']}"[:20]
        try:
            psql(container, reset_user_sql(user, username))
            psql(container, ensure_user_sql(user, username, confirmed=True))
            sql = sql_replay(container, game, user, ops)
            py = py_state(game, ops)
            problems = compare(vector["name"], py, sql)
            if problems:
                failures.extend(problems)
                print(f"scoring_parity: {vector['name']}: MISMATCH")
            else:
                print(f"scoring_parity: {vector['name']}: parity ok "
                      f"({len(ops)} ops)")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{vector['name']}: {exc!r}")
            print(f"scoring_parity: {vector['name']}: ERROR {exc!r}")
        finally:
            try:
                psql(container, reset_user_sql(user, username))
                psql(container, f"delete from public.game_operations where"
                                f" game_uuid = '{game}'::uuid;")
            except Exception:
                pass

    try:
        anon = api_anon_key()
        api_case(container, anon)
        print("scoring_parity: API integration case: ok")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"api_case: {exc!r}")
        print(f"scoring_parity: API integration case: ERROR {exc!r}")
        # Best-effort cleanup even on failure.
        try:
            psql(container, reset_user_sql(uid_for("parity_api"), "parity_api"))
        except Exception:
            pass

    if failures:
        print("\nscoring_parity: FAILED", file=sys.stderr)
        for problem in failures:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("scoring_parity: PASS (vectors + API, Python == PostgreSQL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
