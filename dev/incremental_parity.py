#!/usr/bin/env python3
"""dev/incremental_parity.py - incremental fold vs. full replay (--local).

Migration 0017 scores each upload by folding only the new operations onto a
stored per-game fold (game_fold), falling back to a full rebuild, or to a
background rebuild for large games, whenever it cannot prove the result equal
to a full replay. This harness proves that equality against the real local
PostgreSQL, through the real RPC:

  * random histories (direct and catch-up awards, skips, undo/redo mostly in
    the last 60 operations and sometimes deep, catch-up presets, duplicate
    claims, late insertions from a second device with lower lamports);
  * uploaded with public.submit_operations as `authenticated`, in random
    batch sizes;
  * after every batch the background job runs, then game_state must equal
    public.evolved_replay (XP, inventory, counters, achievements, levels,
    sorted diagnostics) and the fold must not be stale.

Half the cases set ankiscape.sync_rebuild_max_ops = 0, which forces every
fallback through the stale/background path. One JSON line per case is
appended to artifacts/incremental_parity/<timestamp>.jsonl as it runs.

Exit codes: 0 = no mismatches; nonzero = mismatch or error. Never touches
production; uses the local stack only. The synthetic user is removed at the
end.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "dev"))

from scoring_parity import (  # noqa: E402
    ParityError, bind_game_sql, db_container, ensure_user_sql, reset_user_sql)

NS = uuid.NAMESPACE_URL
USERNAME = "incparity"
USER_ID = str(uuid.uuid5(NS, "ankiscape-incremental-parity-user"))
GAME = str(uuid.uuid5(NS, "ankiscape-incremental-parity-game"))

DIRECT = [
    ("mining", "Copper ore"), ("mining", "Tin ore"), ("mining", "Clay"),
    ("mining", "Rune essence"), ("woodcutting", "Tree"),
    ("smithing", "Bronze bar"), ("crafting", "Soft clay"),
    ("fishing", "Shrimp"), ("cooking", "Shrimp"),
]
CATCHUP_SKILLS = ("mining", "woodcutting", "fishing")


def psql_script(container: str, sql: str):
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d",
         "postgres", "-v", "ON_ERROR_STOP=1", "-qtA", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout, proc.stderr


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def generate(rng: random.Random, n_ops: int, profile: str) -> list:
    """Random history in upload order.

    profile "production": the shapes production sees (2026-10-08: awards,
    undo/redo of a recent award; no duplicates, presets, skips or late
    insertions), so nearly every batch must take the fast path and its rewind.
    profile "chaos": adds every shape that must fall back."""
    chaos = profile == "chaos"
    ops = []
    seq = {"dev-a": 0, "dev-b": 0}
    lamport = 0
    awarded = []          # review keys with an award, in upload order
    fresh = 0
    for _ in range(n_ops):
        lamport += 1
        device, lam = "dev-a", lamport
        if chaos and rng.random() < 0.03 and lamport > 5:
            device, lam = "dev-b", rng.randint(0, lamport - 1)   # late insertion
        seq[device] += 1
        roll = rng.random()
        if roll < 0.07 and awarded:
            if not chaos or rng.random() < 0.85:
                target = rng.choice(awarded[-60:])
            elif rng.random() < 0.7:
                target = rng.choice(awarded)
            else:
                target = f"rk-unknown-{rng.randint(0, 9)}"
            kind = "review_retract" if rng.random() < 0.6 else "review_restore"
            payload = {"target_review_key": target, "reason": "anki_undo"}
        elif chaos and roll < 0.10:
            if awarded and rng.random() < 0.5:
                key = rng.choice(awarded)
            else:
                fresh += 1
                key = f"rk-{fresh}"
            kind = "review_skip"
            payload = {"review_key": key, "reason": "classic_mode", "provenance": "skip"}
        elif chaos and roll < 0.11:
            kind = "catchup_preset"
            payload = {"skill": rng.choice(CATCHUP_SKILLS),
                       "effective_ts": rng.randint(0, 4000)}
        else:
            kind = "review_award"
            if chaos and awarded and rng.random() < 0.03:
                key = rng.choice(awarded)                         # duplicate claim
            else:
                fresh += 1
                key = f"rk-{fresh}"
            policy = rng.choice([1, 2, 2, 2])
            if rng.random() < 0.8:
                skill, resource = rng.choice(DIRECT)
                payload = {"review_key": key, "review_ts": lamport, "provenance": "direct",
                           "reward_policy": policy, "skill": skill, "resource": resource}
            else:
                payload = {"review_key": key, "review_ts": rng.randint(0, 4000),
                           "provenance": "catchup", "reward_policy": policy}
            awarded.append(key)
        ops.append({"op_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                    "device_id": device, "device_seq": seq[device], "lamport": lam,
                    "kind": kind, "payload": payload})
    return ops


def batches(rng: random.Random, ops: list) -> list:
    out, i = [], 0
    while i < len(ops):
        size = rng.choice([1, 1, 2, 3, 5, 8, 13, 30, 60, 200])
        out.append(ops[i:i + size])
        i += size
    return out


ASSERT_SQL = """
do $$
declare
  r jsonb := public.evolved_replay({game});
  s record;
  bad text[] := '{{}}';
begin
  select * into s from public.game_state where game_uuid = {game};
  if s.xp is distinct from r->'xp_micro' then bad := bad || 'xp'::text; end if;
  if s.inventory is distinct from r->'inventory' then bad := bad || 'inventory'::text; end if;
  if s.counters is distinct from r->'counters' then bad := bad || 'counters'::text; end if;
  if s.achievements is distinct from r->'achievements' then bad := bad || 'achievements'::text; end if;
  if s.checkpoint->'levels' is distinct from r->'levels' then bad := bad || 'levels'::text; end if;
  if s.checkpoint->'diagnostics' is distinct from
     (select coalesce(jsonb_agg(d order by d), '[]'::jsonb)
        from jsonb_array_elements_text(r->'diagnostics') d) then
    bad := bad || 'diagnostics'::text;
  end if;
  if cardinality(bad) > 0 then
    raise exception 'MISMATCH batch {batch} in %: state=% replay=%', bad,
      jsonb_build_object('xp', s.xp, 'inv', s.inventory, 'counters', s.counters,
                         'ach', s.achievements, 'ckpt', s.checkpoint),
      r - 'review_outcomes';
  end if;
  if exists (select 1 from public.game_fold where game_uuid = {game} and stale) then
    raise exception 'STALE batch {batch}: fold still stale after the background job';
  end if;
end $$;
"""


def case_sql(ops_batches: list, sync_limit: int) -> str:
    game = lit(GAME)
    parts = [
        "set client_min_messages = warning;",
        f"delete from public.game_operations where game_uuid = {game};",
        f"delete from public.game_fold where game_uuid = {game};",
        f"delete from public.game_fold_snapshots where game_uuid = {game};",
        f"delete from public.game_review_keys where game_uuid = {game};",
        f"update public.game_state set revision = 0, xp = '{{}}', inventory = '{{}}',"
        f" counters = '{{}}', achievements = '[]', checkpoint = '{{}}' where game_uuid = {game};",
        "create temp table if not exists _ip(batch int, path text);",
        "truncate _ip;",
        f"select set_config('ankiscape.sync_rebuild_max_ops', '{sync_limit}', false);",
        "select set_config('request.jwt.claims', " +
        lit(json.dumps({"sub": USER_ID, "role": "authenticated"})) + ", false);",
    ]
    for i, batch in enumerate(ops_batches, 1):
        wire = json.dumps(batch, separators=(",", ":"))
        parts.append(
            f"create temp table if not exists _ipb(rebuilt timestamptz, folded bigint);"
            f" truncate _ipb; insert into _ipb select rebuilt_at, folded_ops"
            f" from public.game_fold where game_uuid = {game};")
        parts.append("set role authenticated;")
        parts.append(f"select 1 from public.submit_operations({game}, {lit(wire)}::jsonb);")
        parts.append("reset role;")
        parts.append(
            f"insert into _ip select {i}, case"
            f" when f.stale then 'stale'"
            f" when b.rebuilt is distinct from f.rebuilt_at then 'rebuild'"
            f" else 'fast' end"
            f" from public.game_fold f left join _ipb b on true where f.game_uuid = {game};")
        parts.append("select 1 from public._evolved_fold_rebuild_stale(100);")
        parts.append(ASSERT_SQL.format(game=game, batch=i))
    parts.append("select path || '=' || count(*) from _ip group by path order by path;")
    return "\n".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="incremental_parity.py")
    parser.add_argument("--local", action="store_true",
                        help="run against the local Supabase PostgreSQL stack")
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261008)
    args = parser.parse_args(argv)
    if not args.local:
        print("incremental_parity: pass --local", file=sys.stderr)
        return 2
    try:
        container = db_container()
    except ParityError as exc:
        print(f"incremental_parity: ERROR: {exc}", file=sys.stderr)
        return 2
    out_dir = os.path.join(ROOT, "artifacts", "incremental_parity")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S") + ".jsonl")

    setup = (reset_user_sql(USER_ID, USERNAME)
             + ensure_user_sql(USER_ID, USERNAME, confirmed=True)
             + bind_game_sql(USER_ID, GAME, USERNAME))
    rc, _, err = psql_script(container, setup)
    if rc != 0:
        print(f"incremental_parity: setup failed:\n{err[:2000]}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    totals = {"fast": 0, "rebuild": 0, "stale": 0}
    failures = 0
    started = time.monotonic()
    try:
        for case in range(args.cases):
            n_ops = 2500 if rng.random() < 0.04 else rng.choice([5, 20, 60, 150, 300, 500])
            sync_limit = 0 if case % 2 else 3000
            profile = "production" if case % 4 < 2 else "chaos"
            ops = generate(rng, n_ops, profile)
            plan = batches(rng, ops)
            t0 = time.monotonic()
            rc, out, err = psql_script(container, case_sql(plan, sync_limit))
            paths = {}
            for line in out.split():
                if "=" in line:
                    k, v = line.split("=", 1)
                    paths[k] = int(v)
                    totals[k] = totals.get(k, 0) + int(v)
            record = {"case": case, "seed": args.seed, "profile": profile,
                      "ops": n_ops, "batches": len(plan),
                      "sync_limit": sync_limit, "ok": rc == 0, "paths": paths,
                      "seconds": round(time.monotonic() - t0, 2)}
            if rc != 0:
                failures += 1
                record["error"] = err.strip()[-1500:]
                print(f"incremental_parity: case {case} FAILED ({n_ops} ops, "
                      f"{len(plan)} batches, sync_limit={sync_limit}):\n"
                      f"{err.strip()[-1500:]}", file=sys.stderr)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            if failures >= 3:
                break
    finally:
        psql_script(container, reset_user_sql(USER_ID, USERNAME))
    elapsed = round(time.monotonic() - started, 1)
    print(f"incremental_parity: cases={case + 1} failures={failures} "
          f"batches fast={totals.get('fast', 0)} rebuild={totals.get('rebuild', 0)} "
          f"stale->background={totals.get('stale', 0)} seconds={elapsed}")
    print(f"incremental_parity: evidence {os.path.relpath(log_path, ROOT)}")
    if failures:
        print("incremental_parity: FAILED")
        return 1
    print("incremental_parity: PASS (fold == full replay after every batch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
