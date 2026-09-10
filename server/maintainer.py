#!/usr/bin/env python3
# server/maintainer.py - Local-only maintainer CLI (contract F).
"""Inspect/clear flags, ban/unban, export or delete account data.

Privileged access ONLY: requires the service-role key via the
SUPABASE_SERVICE_ROLE_KEY environment variable (never a CLI arg, never
committed, never in the add-on/logs/artifacts). Every mutating command
requires --confirm <game-or-username> typed confirmation. Read-only
commands (status, show) need no confirmation.

Usage (all against one project; default = local stack):
  python3 server/maintainer.py status --game <uuid>
  python3 server/maintainer.py show --game <uuid>
  python3 server/maintainer.py flag --game <uuid> --confirm <uuid>
  python3 server/maintainer.py unflag --game <uuid> --confirm <uuid>
  python3 server/maintainer.py ban --game <uuid> --confirm <uuid>
  python3 server/maintainer.py unban --game <uuid> --confirm <uuid>
  python3 server/maintainer.py export --game <uuid> --out backup.json
  python3 server/maintainer.py delete-data --game <uuid> --confirm <uuid>

Environment:
  SUPABASE_URL (default http://127.0.0.1:55321)
  SUPABASE_SERVICE_ROLE_KEY (required)
  SUPABASE_ANON_KEY (only for status readable-shape check; optional)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("SUPABASE_URL", "http://127.0.0.1:55321")


def _key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        print("maintainer: ERROR: set SUPABASE_SERVICE_ROLE_KEY in the "
              "environment (never commit it)", file=sys.stderr)
        raise SystemExit(2)
    return key


def _req(method: str, path: str, body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "apikey": key or _key(),
                 "Authorization": f"Bearer {key or _key()}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode() or "{}"
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:500]


def _rpc(fn: str, params: dict):
    return _req("POST", f"/rest/v1/rpc/{fn}", params)


def cmd_status(args) -> int:
    status, rows = _req("GET", "/rest/v1/players?select=username_norm,status,"
                               f"game_uuid&game_uuid=eq.{args.game}", key=_key())
    print(json.dumps({"status": status, "players": rows}, indent=2))
    status, rows = _req("GET", "/rest/v1/game_state?select=game_uuid,revision,"
                               f"updated_at&game_uuid=eq.{args.game}", key=_key())
    print(json.dumps({"status": status, "game_state": rows}, indent=2))
    status, rows = _req("GET", "/rest/v1/game_operations?select=op_id&"
                               f"game_uuid=eq.{args.game}&limit=1", key=_key())
    print(json.dumps({"status": status, "operations_reachable": status == 200}))
    return 0


def cmd_show(args) -> int:
    status, rows = _req("GET", "/rest/v1/players?select=username_norm,status,"
                               "created_at,updated_at&"
                               f"game_uuid=eq.{args.game}", key=_key())
    print(json.dumps(rows, indent=2))
    return 0 if status == 200 else 1


def _set_status(args, status_value: str) -> int:
    if args.confirm != args.game:
        print(f"maintainer: ERROR: re-run with --confirm {args.game} "
              f"to confirm", file=sys.stderr)
        return 2
    status, body = _req("PATCH", "/rest/v1/players?game_uuid=eq." + args.game,
                        {"status": status_value})
    print(json.dumps({"status": status, "body": body}))
    return 0 if status in (200, 204) else 1


def cmd_flag(args) -> int:
    return _set_status(args, "flagged")


def cmd_unflag(args) -> int:
    return _set_status(args, "active")


def cmd_ban(args) -> int:
    return _set_status(args, "banned")


def cmd_unban(args) -> int:
    return _set_status(args, "active")


def cmd_export(args) -> int:
    status, ops = _req("GET", "/rest/v1/game_operations?select=*&"
                              f"game_uuid=eq.{args.game}&order=id&limit=100000")
    if status != 200:
        print(f"maintainer: export failed: {status} {ops}", file=sys.stderr)
        return 1
    bundle = {"game_uuid": args.game, "operations": ops}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
    print(f"maintainer: exported {len(ops)} operations to {args.out}")
    return 0


def cmd_delete_data(args) -> int:
    if args.confirm != args.game:
        print(f"maintainer: ERROR: re-run with --confirm {args.game} "
              f"to confirm", file=sys.stderr)
        return 2
    # Order matters (FK): operations first, then state, then unlink player.
    for table in ("game_operations", "game_state"):
        status, body = _req("DELETE", f"/rest/v1/{table}?"
                                      f"game_uuid=eq.{args.game}")
        print(f"maintainer: delete {table}: {status}")
        if status not in (200, 204):
            print(body, file=sys.stderr)
            return 1
    status, body = _req("PATCH", "/rest/v1/players?game_uuid=eq." + args.game,
                        {"game_uuid": None})
    print(f"maintainer: unlink player: {status}")
    return 0 if status in (200, 204) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="server/maintainer.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "show", "flag", "unflag", "ban", "unban",
                 "export", "delete-data"):
        child = sub.add_parser(name)
        child.add_argument("--game", required=True)
        if name in ("flag", "unflag", "ban", "unban", "delete-data"):
            child.add_argument("--confirm", required=True)
        if name == "export":
            child.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    import urllib.error  # noqa: E402
    globals()["urllib"] = __import__("urllib", fromlist=["error"])
    return {"status": cmd_status, "show": cmd_show, "flag": cmd_flag,
            "unflag": cmd_unflag, "ban": cmd_ban, "unban": cmd_unban,
            "export": cmd_export, "delete-data": cmd_delete_data}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
