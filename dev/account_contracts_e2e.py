#!/usr/bin/env python3
"""dev/account_contracts_e2e.py --local - real account/service journey.

Not shipped. Runs against the alternate-port LOCAL Supabase stack (same one
dev/sync_e2e.py uses) with the production runtime pieces only:

  * ProfileSession + the OS credential vault (isolated temp profile)
  * accounts.login_password (the exact callable the real login dialog runs;
    the dialog button wiring itself is driven by dev/ui_ux_verify.py's
    `dialogs` scenario in real Anki)
  * make_transport + SyncService/SyncJob over the real post_json transport
  * query_hiscores and query_public_profile

Fixture accounts are created through the local admin API with email_confirm
(no email is sent) and deleted at the end. Calls under test use ordinary
account tokens. No MemorySession, no injected rows, no callback recorders.
Run:  python3 dev/account_contracts_e2e.py --local
Exits 2 (blocked, verification pending) when Docker/Supabase is unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

API = "http://127.0.0.1:55321"
FAILURES: list = []

from evolved.accounts import login_password, logout  # noqa: E402
from evolved.credentials import CredentialVault  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.net import Endpoint, NetError, post_json  # noqa: E402
from evolved.service import (ServiceConfig, SyncService, make_transport,  # noqa: E402
                             query_hiscores, query_public_profile)
from evolved.session_store import ProfileSession  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _supabase_env() -> dict:
    try:
        out = subprocess.run(["supabase", "status", "-o", "env"],
                             capture_output=True, text=True,
                             cwd=os.path.join(ROOT, "server"), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    env = {}
    for line in (out.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    env["ANON_KEY"] = env.get("ANON_KEY") or env.get("PUBLISHABLE_KEY", "")
    return env


def _admin(service_key: str, method: str, path: str, body=None):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "apikey": service_key,
                 "Authorization": f"Bearer {service_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def _create_fixture_user(service_key, *, email, username, password):
    status, data = _admin(service_key, "POST", "/auth/v1/admin/users", {
        "email": email, "password": password, "email_confirm": True,
        "user_metadata": {"username_norm": username,
                          "username_display": username}})
    if status >= 300:
        return "", status, data
    return str(data.get("id") or (data.get("user") or {}).get("id") or ""), \
        status, data


def _delete_fixture_user(service_key, user_id):
    if user_id:
        _admin(service_key, "DELETE", f"/auth/v1/admin/users/{user_id}")


def _refresh_via_auth(session, endpoint) -> bool:
    """Same contract as the product's _evolved_refresh_session."""
    if not session.refresh_token:
        return False
    try:
        data = post_json(endpoint, "/auth/v1/token?grant_type=refresh_token",
                         {"refresh_token": session.refresh_token})
    except NetError as exc:
        if exc.kind in ("unauthorized", "invalid", "forbidden"):
            session.clear()
        return False
    access, refresh = data.get("access_token"), data.get("refresh_token")
    user = data.get("user") or {}
    if not access or not refresh or not user.get("id"):
        return False
    session.set(access_token=access, refresh_token=refresh,
                user_id=user["id"], username=session.username)
    return True


def _op(game_uuid, device, seq, review_key):
    return {"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
            "device_id": device, "device_seq": seq, "lamport": seq,
            "kind": "review_award",
            "payload": {"review_key": review_key,
                        "review_ts": 1_800_000_000 + seq,
                        "rating": 3, "review_kind": "review",
                        "provenance": "direct", "reward_policy": 2,
                        "skill": "mining", "resource": "Rune essence"}}


def _svc(game_uuid, journal, transport, session, generation=1):
    return SyncService(generation=generation, game_uuid=game_uuid,
                       journal=journal, transport=transport,
                       apply_remote=lambda page: None,
                       get_user_id=lambda: session.user_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", required=True,
                        help="run against the local Supabase stack")
    parser.parse_args(argv)

    env = _supabase_env()
    anon = env.get("ANON_KEY") or ""
    service = env.get("SERVICE_ROLE_KEY") or ""
    if not anon or not service:
        print("account_contracts_e2e: BLOCKED: local Supabase stack "
              "unavailable (start Docker Desktop + `supabase start`); "
              "verification stays pending", file=sys.stderr)
        return 2
    endpoint = Endpoint(base_url=API, project_key=anon,
                        allow_http_loopback=True)

    stamp = uuid.uuid4().hex[:10]
    password = uuid.uuid4().hex
    users = []
    created: list = []
    tmp = tempfile.TemporaryDirectory()
    journals: list = []
    try:
        email_a = f"acct-a-{stamp}@example.invalid"
        name_a = f"accta{stamp[:8]}"
        email_b = f"acct-b-{stamp}@example.invalid"
        name_b = f"acctb{stamp[:8]}"
        id_a, status_a, _ = _create_fixture_user(
            service, email=email_a, username=name_a, password=password)
        id_b, status_b, _ = _create_fixture_user(
            service, email=email_b, username=name_b, password=password)
        users = [id_a, id_b]
        check("fixture users created without email",
              bool(id_a and id_b) and status_a < 300 and status_b < 300,
              f"{status_a}/{status_b}")

        profile_dir = os.path.join(tmp.name, "profile")
        os.makedirs(profile_dir, exist_ok=True)
        vault = CredentialVault(profile_dir, API)
        check("isolated credential vault available (or cleanly unavailable)",
              isinstance(vault.available, bool))

        # --- Login through the production submit the real dialog calls -----
        session = ProfileSession(generation=1, vault=vault)
        result = login_password(post_json, endpoint, email=email_a,
                                password=password, session=session.session)
        check("login submit succeeds", result.ok, result.error)
        check("profile wrapper reports logged in", session.logged_in)
        check("ProfileSession.access_token delegates",
              isinstance(session.access_token, str)
              and bool(session.access_token))
        check("remembered sign-in persisted",
              not vault.available or vault.read() is not None)

        # --- Hiscores before any game is linked ---------------------------
        baseline = query_hiscores(post_json, endpoint, session,
                                  skill="mining", limit=50)
        check("baseline hiscores is a list", isinstance(baseline, list))
        check("unlinked account is not ranked",
              all(row["username"] != name_a for row in baseline))

        # --- link_game + real manual sync ---------------------------------
        game = str(uuid.uuid4())
        link = post_json(endpoint, "/rest/v1/rpc/link_game",
                         {"p_game_uuid": game},
                         access_token=session.access_token)
        check("link_game binds the game",
              isinstance(link, dict) and link.get("resumed") is False,
              str(link)[:120])
        journal = Journal(os.path.join(tmp.name, "game.sqlite3"))
        journals.append(journal)
        transport = make_transport(ServiceConfig(
            endpoint=endpoint, game_uuid=game, post=post_json), session)
        op1 = _op(game, "dev-a", 1, f"rk-acct-1-{stamp}")
        journal.append_operation(op1)
        svc = _svc(game, journal, transport, session)
        out = svc.force_sync()
        check("manual sync uploads and acks", out.get("ok") is True,
              str(out)[:200])
        check("outbox drained", journal.pending_operations() == [])

        rows = query_hiscores(post_json, endpoint, session, skill="mining")
        mine = [row for row in rows if row["username"] == name_a]
        check("hiscores shows the ranked player", len(mine) == 1,
              f"rows={len(rows)} mine={len(mine)}")
        if mine:
            check("rank is a positive integer", mine[0]["rank"] >= 1)
            check("server-authoritative micro-XP is present",
                  int(mine[0]["xp"]) > 0)
            check("XP display divides micro units",
                  mine[0]["xp_display"] not in ("", "0"))

        # --- Player lookup -------------------------------------------------
        found = query_public_profile(post_json, endpoint, session,
                                     username=name_a, skill="mining")
        check("lookup finds the player", found.get("ok") is True,
              str(found)[:160])
        if found.get("ok"):
            check("lookup rank matches hiscores",
                  found["profile"]["rank"] == (mine[0]["rank"] if mine else None))
        mixed = query_public_profile(post_json, endpoint, session,
                                     username=name_a.upper(), skill="mining")
        check("lookup normalizes mixed case", mixed.get("ok") is True)
        missing = query_public_profile(post_json, endpoint, session,
                                       username=f"nobody{stamp[:6]}",
                                       skill="mining")
        check("missing player is friendly not-found",
              missing.get("ok") is False and missing.get("not_found") is True)

        # --- Second device downloads the remote op (real transport) --------
        journal_b = Journal(os.path.join(tmp.name, "device-b.sqlite3"))
        journals.append(journal_b)
        out_b = _svc(game, journal_b, transport, session, generation=2) \
            .force_sync()
        count_b = journal_b._conn.execute(
            "select count(*) from operations").fetchone()[0]
        check("second device downloads and merges", out_b.get("ok") is True
              and count_b == 1, f"ok={out_b.get('ok')} ops={count_b}")

        # --- Forced expiry -> one refresh -> retry with rotated token ------
        op2 = _op(game, "dev-a", 2, f"rk-acct-2-{stamp}")
        journal.append_operation(op2)
        issued = {"count": 0}

        def _refresh(memory):
            issued["count"] += 1
            return _refresh_via_auth(memory, endpoint)

        session.bind_refresh(_refresh)
        session.session.set(access_token="expired-token",
                            refresh_token=session.session.refresh_token,
                            user_id=session.user_id,
                            username=session.username)
        out2 = _svc(game, journal, transport, session, generation=3) \
            .force_sync()
        check("401 refreshes once and retries", issued["count"] == 1
              and out2.get("ok") is True, f"{issued} {str(out2)[:120]}")
        check("access token rotated", session.access_token != "expired-token")
        check("retried op acknowledged", journal.pending_operations() == [])

        # --- Restart + resumed remembered session --------------------------
        resumed = ProfileSession(generation=4,
                                 vault=CredentialVault(profile_dir, API))
        check("restart resumes the remembered session",
              resumed.logged_in and bool(resumed.access_token))
        resumed_rows = query_hiscores(post_json, endpoint, resumed,
                                      skill="mining")
        check("resumed session reaches the service",
              any(row["username"] == name_a for row in resumed_rows))

        # --- Offline progress -> reconnect ---------------------------------
        op3 = _op(game, "dev-a", 3, f"rk-acct-3-{stamp}")
        journal.append_operation(op3)
        dead = Endpoint(base_url="http://127.0.0.1:1", project_key=anon,
                        allow_http_loopback=True)
        off = _svc(game, journal,
                   make_transport(ServiceConfig(endpoint=dead,
                                                game_uuid=game,
                                                post=post_json), resumed),
                   resumed, generation=5).force_sync()
        check("offline sync fails without losing progress",
              off.get("ok") is False
              and len(journal.pending_operations()) == 1)
        on = _svc(game, journal,
                  make_transport(ServiceConfig(endpoint=endpoint,
                                               game_uuid=game,
                                               post=post_json), resumed),
                  resumed, generation=6).force_sync()
        check("reconnect drains pending", on.get("ok") is True
              and journal.pending_operations() == [])

        # --- Logout clears the remembered session --------------------------
        logout(resumed.session)
        check("logout clears live session", not resumed.logged_in)
        after = ProfileSession(generation=7,
                               vault=CredentialVault(profile_dir, API))
        check("logout clears the remembered session", not after.logged_in
              and vault.read() is None)
    finally:
        for journal in journals:
            try:
                journal.close()
            except Exception:
                pass
        tmp.cleanup()
        for user_id in users:
            _delete_fixture_user(service, user_id)
        print("fixture users deleted" if users else "no fixture users to delete")

    print(f"\n{len(FAILURES)} failures" if FAILURES
          else "\naccount contracts journey: ALL PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
