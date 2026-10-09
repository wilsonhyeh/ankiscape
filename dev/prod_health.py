#!/usr/bin/env python3
"""dev/prod_health.py - read-only production capacity check (nightly).

Calls public.ops_health() (migration 0017; service_role only) and fails when
production is drifting toward a limit that would break sync for players:

  * database over 350 MB      (Supabase Free caps it at 500 MB; on 2026-10-08
                               it reached 443 MB unnoticed)
  * a scoring fold stale for over 15 minutes (the background rebuild runs
    every minute; a fold this old means the job is failing)
  * a game missing its fold   (every game with operations should have one)
  * a game over 20,000 live operations (the size where Phase 2 archiving of
    HQ plans/ankiscape-incremental-scoring-and-compaction.md must be live)
  * any failed pg_cron run in the last 24 hours

Environment (same names as the hosted nightly lane):
  ANKISCAPE_PROD_URL         https://<ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  service_role key (env only, never committed)

Exit codes: 0 healthy; 1 a threshold is exceeded; 2 cannot check.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.request

DB_BYTES_MAX = 350 * 1024 * 1024
STALE_MINUTES_MAX = 15
LIVE_OPS_PER_GAME_MAX = 20_000


def fetch() -> dict:
    base = os.environ["ANKISCAPE_PROD_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    req = urllib.request.Request(
        base + "/rest/v1/rpc/ops_health", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "apikey": key,
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ops_health must return an object")
    return data


def problems(h: dict, now: _dt.datetime) -> list:
    out = []
    if int(h.get("db_bytes") or 0) > DB_BYTES_MAX:
        out.append(f"database is {int(h['db_bytes']) // (1024 * 1024)} MB "
                   f"(limit {DB_BYTES_MAX // (1024 * 1024)} MB of the 500 MB Free cap)")
    oldest = h.get("oldest_stale_since")
    if oldest:
        since = _dt.datetime.fromisoformat(str(oldest).replace("Z", "+00:00"))
        minutes = (now - since).total_seconds() / 60
        if minutes > STALE_MINUTES_MAX:
            out.append(f"{h.get('stale_folds')} scoring fold(s) stale; oldest for "
                       f"{minutes:.0f} min (background rebuild failing?)")
    if int(h.get("missing_folds") or 0) > 0:
        out.append(f"{h['missing_folds']} game(s) with operations have no scoring fold")
    if int(h.get("largest_game_ops") or 0) > LIVE_OPS_PER_GAME_MAX:
        out.append(f"largest game has {h['largest_game_ops']} live operations "
                   f"(limit {LIVE_OPS_PER_GAME_MAX}; archiving must be live)")
    if int(h.get("cron_failures_24h") or 0) > 0:
        out.append(f"{h['cron_failures_24h']} failed pg_cron run(s) in the last 24 h")
    return out


def main() -> int:
    for var in ("ANKISCAPE_PROD_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"prod_health: ERROR: {var} is not set", file=sys.stderr)
            return 2
    try:
        h = fetch()
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"prod_health: ERROR: {exc!r}", file=sys.stderr)
        return 2
    print("prod_health: " + json.dumps(h, sort_keys=True))
    found = problems(h, _dt.datetime.now(_dt.timezone.utc))
    for item in found:
        print(f"prod_health: FAIL {item}")
    if found:
        return 1
    print("prod_health: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
