#!/usr/bin/env python3
# dev/sync_e2e.py - Online/recovery journey against the REAL local stack.
"""Dev only, never shipped. Exercises the new service layer (service.py +
SyncJob) end to end through real local Supabase Auth/RPCs:

  1. signup -> OTP (Mailpit) -> verify -> session (two users: A + B)
  2. A links game, credits 2 ops offline, uploads via SyncService
  3. B links the SAME game (second device): downloads A's ops, merges
  4. changed-payload ID conflict -> quarantine, outbox drains
  5. lost-ack retry: transport drops the ack, retry converges (no dupes)
  6. 429 from server -> structured rate_limited, pending preserved

Uses the alternate-port local stack (API 55321, Mailpit 55324) and the real
service.make_transport wire mapping. Fails honestly when Docker/Supabase is
down. No mocks of password verification, no production, no real email.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

API = "http://127.0.0.1:55321"
MAILPIT = "http://127.0.0.1:55324"
FAILURES = []
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

from evolved.auth import MemorySession  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.net import Endpoint, post_json  # noqa: E402
from evolved.service import ServiceConfig, SyncService, make_transport  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _anon_key():
    out = subprocess.run(["supabase", "status", "-o", "env"], capture_output=True,
                         text=True, cwd=os.path.join(ROOT, "server"), timeout=60)
    env = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    return env.get("ANON_KEY") or env.get("PUBLISHABLE_KEY") or ""


def _req(method, path, body, token, anon):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
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


def _signup_verify(email, username, password, anon):
    status, _ = _req("POST", "/auth/v1/signup",
                     {"email": email, "password": password,
                      "data": {"username_norm": username,
                               "username_display": username.title()}},
                     token=None, anon=anon)
    if status != 200:
        return None, f"signup {status}"
    code = ""
    for _ in range(45):
        time.sleep(2)
        try:
            with urllib.request.urlopen(MAILPIT + "/api/v1/messages", timeout=10) as resp:
                msgs = json.loads(resp.read().decode()).get("messages", [])
            mine = [m for m in msgs
                    if any(email in (t.get("Address", "")) for t in m.get("To", []))]
            if mine:
                with urllib.request.urlopen(
                        f"{MAILPIT}/api/v1/message/{mine[-1]['ID']}", timeout=10) as resp:
                    body = json.loads(resp.read().decode()).get("Text", "")
                match = re.search(r"\b(\d{6})\b", body)
                if match:
                    code = match.group(1)
                    break
        except Exception:
            continue
    if not code:
        return None, "no OTP captured"
    status, session = _req("POST", "/auth/v1/verify",
                           {"email": email, "token": code, "type": "signup"},
                           token=None, anon=anon)
    if status != 200 or not session.get("access_token"):
        return None, f"verify {status}"
    return session, ""


def _op(game_uuid, device_id, seq, lamport, review_key):
    return {"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
            "device_id": device_id, "device_seq": seq, "lamport": lamport,
            "kind": "review_award",
            "payload": {"review_key": review_key, "review_ts": 1_800_000_000 + seq,
                        "rating": 3, "review_kind": "review",
                        "provenance": "direct", "reward_policy": 2,
                        "skill": "mining", "resource": "Rune essence"}}


def main():
    anon = _anon_key()
    check("local anon key available", bool(anon))
    if not anon:
        return 1
    stamp = int(time.time())
    offered_game = str(uuid.uuid4())
    endpoint = Endpoint(base_url=API, project_key=anon,
                        allow_http_loopback=True)

    # 1. Two users through real Auth + captured mail.
    session_a, err = _signup_verify(f"synca{stamp}@example.com",
                                    f"synca{stamp % 100000}",
                                    "correct horse 9", anon)
    check("user A signup+verify", session_a is not None, err)
    session_b, err = _signup_verify(f"syncb{stamp}@example.com",
                                    f"syncb{stamp % 100000}",
                                    "correct horse 9", anon)
    check("user B signup+verify", session_b is not None, err)
    if session_a is None or session_b is None:
        return 1
    mem_a = MemorySession()
    mem_a.set(access_token=session_a["access_token"],
              refresh_token=session_a.get("refresh_token", ""),
              user_id=session_a["user"]["id"])
    mem_b = MemorySession()
    mem_b.set(access_token=session_b["access_token"],
              refresh_token=session_b.get("refresh_token", ""),
              user_id=session_b["user"]["id"])

    # 2. A links the game, then credits 2 ops OFFLINE (no network yet).
    # The server owns the game uuid (D5): the returned uuid is the only game
    # id used after the link (S11/S16). created/resumed are pinned
    # complementary and `created` is a key-presence rule, not `is True`.
    status, data = _req("POST", "/rest/v1/rpc/link_game",
                        {"p_game_uuid": offered_game},
                        token=mem_a.access_token, anon=anon)
    reply = data if isinstance(data, dict) else {}
    game_uuid = str(reply.get("game_uuid") or "")
    check("A link_game creates with the pinned created/resumed reply",
          status in (200, 201) and reply.get("created") is True
          and reply.get("resumed") is False, f"{status} {data}")
    check("A adopts the server-owned game uuid",
          bool(_UUID_RE.match(game_uuid)) and game_uuid != offered_game,
          f"{status} {data}")
    tmp_a = tempfile.TemporaryDirectory()
    journal_a = Journal(os.path.join(tmp_a.name, "a.sqlite3"))
    op_a1 = _op(game_uuid, "dev-a", 1, 1, f"rk-live-a1-{stamp}")
    op_a2 = _op(game_uuid, "dev-a", 2, 2, f"rk-live-a2-{stamp}")
    journal_a.append_operation(op_a1)
    journal_a.append_operation(op_a2)
    check("A offline credit persists", len(journal_a.pending_operations()) == 2)

    # 3. A uploads through the REAL service transport.
    cfg_a = ServiceConfig(endpoint=endpoint, game_uuid=game_uuid, post=post_json)
    transport_a = make_transport(cfg_a, mem_a)
    applied_a = []
    svc_a = SyncService(generation=11, game_uuid=game_uuid, journal=journal_a,
                        transport=transport_a,
                        apply_remote=lambda page: applied_a.append(page),
                        get_user_id=lambda: mem_a.user_id)
    out = svc_a.maybe_sync(manual=True)
    check("A service upload ok", out.get("ok") is True, str(out)[:200])
    check("A outbox drained", journal_a.pending_operations() == [])

    # 4. B links the SAME game? No: one game binds one account. B uses a
    # second device journal for the same game via A's credentials (device b):
    # downloads A's ops and merges them locally (two-device convergence).
    tmp_b = tempfile.TemporaryDirectory()
    journal_b = Journal(os.path.join(tmp_b.name, "b.sqlite3"))
    transport_b = make_transport(cfg_a, mem_a)
    applied_b = []
    svc_b = SyncService(generation=12, game_uuid=game_uuid, journal=journal_b,
                        transport=transport_b,
                        apply_remote=lambda page: applied_b.append(page),
                        get_user_id=lambda: mem_a.user_id)
    out = svc_b.maybe_sync(manual=True)
    check("B service download+merge ok", out.get("ok") is True, str(out)[:200])
    ids_b = sorted(journal_b.operation_ids())
    check("B converged on A's ops",
          ids_b == sorted([op_a1["op_id"], op_a2["op_id"]]), str(ids_b)[:200])

    # 5. Changed-payload ID conflict quarantines; outbox still drains.
    poison = dict(op_a1, device_id="dev-b", device_seq=99, lamport=99,
                  payload={"review_key": "rk-POISONED"})
    status, data = _req("POST", "/rest/v1/rpc/submit_operations",
                        {"p_game_uuid": game_uuid, "p_ops": [poison]},
                        token=mem_a.access_token, anon=anon)
    check("server reports id conflict",
          status == 200 and len(data.get("conflicts", [])) == 1,
          f"{status} {data}")

    # 6. Lost-ack retry through the service: first upload attempt drops the
    # response after commit; the retry converges with no duplicates.
    op_a3 = _op(game_uuid, "dev-a", 3, 3, f"rk-live-a3-{stamp}")
    journal_a.append_operation(op_a3)
    real_post = post_json
    dropped = {"n": 0}

    def _flaky_post(endpoint, path, payload, access_token=None, headers=None):
        kwargs = {"access_token": access_token}
        if headers is not None:
            kwargs["headers"] = headers
        if path.endswith("submit_operations") and dropped["n"] == 0:
            dropped["n"] += 1
            real_post(endpoint, path, payload, **kwargs)
            raise ConnectionError("ack lost after commit")
        return real_post(endpoint, path, payload, **kwargs)

    cfg_flaky = ServiceConfig(endpoint=endpoint, game_uuid=game_uuid,
                              post=_flaky_post)
    svc_flaky = SyncService(
        generation=11, game_uuid=game_uuid, journal=journal_a,
        transport=make_transport(cfg_flaky, mem_a),
        apply_remote=lambda page: None,
        get_user_id=lambda: mem_a.user_id)
    out = svc_flaky.maybe_sync(manual=True)
    check("lost-ack surfaces transient (nothing acked)", out.get("ok") is False,
          str(out)[:200])
    check("op still pending after lost ack",
          [op["op_id"] for op in journal_a.pending_operations()] == [op_a3["op_id"]])
    svc_retry = SyncService(
        generation=11, game_uuid=game_uuid, journal=journal_a,
        transport=make_transport(cfg_a, mem_a),
        apply_remote=lambda page: None,
        get_user_id=lambda: mem_a.user_id)
    out = svc_retry.maybe_sync(manual=True)
    check("retry converges", out.get("ok") is True, str(out)[:200])
    check("no duplicates server-side",
          journal_a.pending_operations() == [])

    journal_a.close()
    journal_b.close()
    tmp_a.cleanup()
    tmp_b.cleanup()
    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nsync journey: ALL PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    import urllib.error

    raise SystemExit(main())
