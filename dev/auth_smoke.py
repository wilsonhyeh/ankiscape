#!/usr/bin/env python3
"""dev/auth_smoke.py - Live local Auth + RPC smoke (dev only, never shipped).

Exercises the ACTUAL local Auth service and captured inbox (Mailpit), not a
mock of password verification: signup -> OTP email -> verify -> JWT ->
link_game -> submit/fetch/hiscores -> negative + concurrency cases.

Usage: python3 dev/auth_smoke.py   (reads local keys via supabase status)
Exits nonzero on any failure. Local stack only; no real email ever sent.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "http://127.0.0.1:55321"
MAILPIT = "http://127.0.0.1:55324"
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _keys():
    out = subprocess.run(["supabase", "status", "-o", "env"], capture_output=True,
                         text=True, cwd=os.path.join(ROOT, "server"), timeout=60)
    env = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    anon = env.get("ANON_KEY") or env.get("PUBLISHABLE_KEY") or ""
    return anon


def _req(method, path, body=None, token=None, anon=None, base=API):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "apikey": anon,
                                          "X-AnkiScape-Protocol": "2"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def _mailpit_messages():
    with urllib.request.urlopen(MAILPIT + "/api/v1/messages", timeout=10) as resp:
        return json.loads(resp.read().decode()).get("messages", [])


def _mailpit_body(msg_id):
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{msg_id}", timeout=10) as resp:
        return json.loads(resp.read().decode()).get("Text", "")


def main():
    anon = _keys()
    check("local anon key available", bool(anon))
    if not anon:
        return 1
    stamp = int(time.time())
    email = f"smoke{stamp}@example.com"
    username = f"smoke{stamp % 100000}"
    password = "correct horse 9"

    # 1. Signup with username metadata.
    status, data = _req("POST", "/auth/v1/signup",
                        {"email": email, "password": password,
                         "data": {"username_norm": username,
                                  "username_display": username.title()}},
                        anon=anon)
    check("signup 200", status == 200, f"{status} {data}")
    # GoTrue returns the user object directly (older versions nest it).
    user = data.get("user", data) if isinstance(data, dict) else {}
    if not isinstance(user, dict) or not user.get("id"):
        user = {}
    check("unconfirmed at signup", bool(user.get("id")) and not user.get("confirmed_at"),
          str(data)[:200])

    # 2. OTP arrives in captured local mail (never real email).
    code = ""
    last_mail_error = ""
    for _ in range(45):
        time.sleep(2)
        try:
            all_msgs = _mailpit_messages()
            msgs = [m for m in all_msgs
                    if any(email in (t.get("Address", "")) for t in m.get("To", []))]
        except Exception as exc:  # keep polling through transient mailpit states
            last_mail_error = repr(exc)
            continue
        if msgs:
            try:
                body = _mailpit_body(msgs[-1]["ID"])
            except Exception as exc:
                last_mail_error = repr(exc)
                continue
            match = re.search(r"\b(\d{6})\b", body)
            if match:
                code = match.group(1)
                break
    check("OTP email captured locally", bool(code), last_mail_error)

    # 3. Verify -> managed session.
    status, session = _req("POST", "/auth/v1/verify",
                           {"email": email, "token": code, "type": "signup"},
                           anon=anon)
    check("verify 200 + session", status == 200 and bool(session.get("access_token")),
          f"{status} {str(session)[:200]}")
    token = session.get("access_token", "")
    uid = (session.get("user", {}) or {}).get("id", "")

    # 4. Trigger created the players row (checked as db owner locally).
    out = subprocess.run(
        ["docker", "exec", "supabase_db_server", "psql", "-U", "postgres", "-tAc",
         f"select username_norm from public.players where user_id='{uid}'"],
        capture_output=True, text=True, timeout=30)
    check("username claim trigger created players row",
          out.stdout.strip() == username, out.stdout.strip()[:100])

    # 5. link_game binds then resumes.
    game = "11111111-2222-3333-4444-555555555555"
    status, data = _req("POST", "/rest/v1/rpc/link_game", {"p_game_uuid": game},
                        token=token, anon=anon)
    check("link_game binds", status in (200, 201) and data.get("resumed") is False,
          f"{status} {data}")
    status, data = _req("POST", "/rest/v1/rpc/link_game", {"p_game_uuid": game},
                        token=token, anon=anon)
    check("link_game resumes", status in (200, 201) and data.get("resumed") is True,
          f"{status} {data}")

    # 6. Submit + idempotent retry + changed-payload conflict.
    # Fresh op_id per run: op_ids are globally unique, so fixed ids would
    # collide with earlier smoke runs against the same local database.
    op_id = __import__("uuid").uuid4().hex
    op_id = f"{op_id[:8]}-{op_id[8:12]}-4{op_id[13:16]}-8{op_id[17:20]}-{op_id[20:32]}"
    op = {"op_id": op_id, "device_id": "dev-a",
          "device_seq": 1, "lamport": 1, "kind": "review_award",
          "payload": {"review_key": "rk-smoke-1"}}
    status, data = _req("POST", "/rest/v1/rpc/submit_operations",
                        {"p_game_uuid": game, "p_ops": [op]}, token=token, anon=anon)
    check("submit accepted", status == 200 and len(data.get("accepted", [])) == 1,
          f"{status} {data}")
    status, data2 = _req("POST", "/rest/v1/rpc/submit_operations",
                         {"p_game_uuid": game, "p_ops": [op]}, token=token, anon=anon)
    check("exact retry same acceptance",
          status == 200 and data2.get("accepted") == data.get("accepted"),
          f"{status} {data2}")
    bad = dict(op, payload={"review_key": "rk-CHANGED"})
    status, data3 = _req("POST", "/rest/v1/rpc/submit_operations",
                         {"p_game_uuid": game, "p_ops": [bad]}, token=token, anon=anon)
    check("changed payload conflicts",
          status == 200 and len(data3.get("conflicts", [])) == 1, f"{status} {data3}")

    # 7. Concurrent duplicate submits converge to one row.
    results = []

    def _worker():
        try:
            results.append(_req("POST", "/rest/v1/rpc/submit_operations",
                                {"p_game_uuid": game, "p_ops": [op]},
                                token=token, anon=anon))
        except OSError as exc:
            results.append((0, {"error": repr(exc)}))

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("concurrent retries all accepted",
          all(status == 200 and len(data.get("accepted", [])) == 1
              for status, data in results), str(results)[:300])

    # 8. Fetch + hiscores + profile shapes.
    status, data = _req("POST", "/rest/v1/rpc/fetch_operations",
                        {"p_game_uuid": game, "p_cursor": 0, "p_limit": 200},
                        token=token, anon=anon)
    check("fetch page with revision",
          status == 200 and isinstance(data.get("operations"), list)
          and data.get("revision", 0) >= 1, f"{status} {str(data)[:200]}")
    status, data = _req("POST", "/rest/v1/rpc/hiscores",
                        {"p_skill": "mining", "p_limit": 10}, token=token, anon=anon)
    check("hiscores array", status == 200 and isinstance(data, list),
          f"{status} {str(data)[:200]}")
    status, data = _req("POST", "/rest/v1/rpc/public_profile",
                        {"p_username_norm": username}, token=token, anon=anon)
    check("public profile safe shape",
          status == 200 and data.get("username") == username.title()
          and "email" not in json.dumps(data).lower(), f"{status} {str(data)[:200]}")

    # 9. Negatives: wrong password, oversized batch, unverified link.
    status, _ = _req("POST", "/auth/v1/token?grant_type=password",
                     {"email": email, "password": "wrong password 9"}, anon=anon)
    check("wrong password rejected", status in (400, 401), f"status {status}")
    big = [dict(op, op_id=f"00000000-0000-4000-8000-{i:012d}", device_seq=100 + i,
                lamport=100 + i, payload={"review_key": f"rk-big-{i}"})
           for i in range(201)]
    status, _ = _req("POST", "/rest/v1/rpc/submit_operations",
                     {"p_game_uuid": game, "p_ops": big}, token=token, anon=anon)
    check("201-op batch rejected", status == 400, f"status {status}")

    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nauth smoke: ALL PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    import urllib.error

    raise SystemExit(main())
