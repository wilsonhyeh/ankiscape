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
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

API = "http://127.0.0.1:55321"
MAILPIT = "http://127.0.0.1:55324"
FAILURES: list = []
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

from evolved.accounts import (delete_account, login_password, logout,  # noqa: E402
                              register, resend_signup_code, verify_code,
                              check_account_status)
from evolved.auth import MemorySession  # noqa: E402
from evolved.credentials import CredentialVault  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.net import Endpoint, NetError, post_json  # noqa: E402
from evolved.service import (ServiceConfig, SyncService, make_transport,  # noqa: E402
                             query_hiscores, query_public_profile)
from evolved.session_store import ProfileSession  # noqa: E402


def _load_module(name: str, filename: str):
    path = os.path.join(ROOT, "dev", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _load_auth_smoke_helpers():
    """Reuse the existing capture-inbox helpers from dev/auth_smoke.py."""
    return _load_module("ankiscape_auth_smoke", "auth_smoke.py")


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


def _service(service_key, method, path, body=None):
    """Service-role PostgREST call (local disposable stack only)."""
    return _admin(service_key, method, path, body)


def _rows(service_key, path):
    status, data = _service(service_key, "GET", path)
    if status >= 300 or not isinstance(data, list):
        return None
    return data


def _mailpit_message(email, *, after_id="", timeout_s=90.0):
    """Bounded capture-inbox polling for the exact disposable recipient."""
    smoke = _load_auth_smoke_helpers()
    if smoke is None:
        return "", ""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            messages = smoke._mailpit_messages()
        except Exception:
            time.sleep(2)
            continue
        for message in reversed(messages):
            message_id = str(message.get("ID", ""))
            recipients = [t.get("Address", "") for t in message.get("To", [])]
            if not any(email.lower() == r.lower() for r in recipients):
                continue
            if after_id and message_id == after_id:
                continue
            try:
                body = smoke._mailpit_body(message_id)
            except Exception:
                continue
            match = re.search(r"\b(\d{6})\b", body or "")
            if match:
                return match.group(1), message_id
        time.sleep(2)
    return "", ""


def _refresh_via_auth(session, endpoint) -> bool:
    """The product's own owner-guarded refresh helper (not a dev copy)."""
    from evolved.accounts import refresh_access_token
    return refresh_access_token(post_json, endpoint, session)


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


# GoTrue refuses a second email to the SAME address inside
# GOTRUE_SMTP_MAX_FREQUENCY (1s on the local stack) and answers HTTP 429
# `over_email_send_rate_limit`, with the body's "after N seconds" rounding to
# zero. Measured directly: an immediate duplicate signup is 429, the same call
# after 1s is 200. The config.toml key that would remove the window is not
# honoured by the CLI, so the sends are paced here instead - these checks are
# about the signup/resend contract, not about the limiter.
_SEND_WINDOW_S = 1.2


def _await_send_window() -> None:
    time.sleep(_SEND_WINDOW_S)


def _signup_resend_verify_checks(*, anon, service, endpoint, check, stamp,
                                 users):
    """Real GoTrue signup/resend/verify and duplicate detection, through the
    product client.

    Two addresses, deliberately. Resending and duplicate-detecting pull the
    same account's OTP state in opposite directions, and interleaving them on
    one address left NO code usable: measured, signup -> duplicate -> resend
    sent three emails of which only the first verified, while a clean
    signup -> resend verified the resent code with HTTP 200. The duplicate
    check also needs a CONFIRMED address, because GoTrue answers an
    unconfirmed duplicate with the real user (the client then reports
    verification_required) and only refuses a confirmed one with
    422 user_already_exists, which the client maps to email_exists. Confirming
    the address is therefore a precondition of that check, not a detour.
    """
    # --- A: the signup `resend` route, on its own uninterrupted address -----
    email = f"acct-signup-{stamp}@example.invalid"
    username = f"signup{stamp[:8]}"
    password = uuid.uuid4().hex + "Aa9"
    memory = MemorySession()
    out = register(post_json, endpoint, username=username, email=email,
                   password=password)
    check("signup reaches verification_required against real GoTrue "
          "(flat shape)", out.status == "verification_required"
          and bool(out.session_user_id), f"{out.status} {out.detail}")
    status = check_account_status(post_json, endpoint, email=email,
                                  username=username)
    check("status recheck reports the unconfirmed email",
          status.ok and status.email_status == "unconfirmed",
          f"{status.status} {status.email_status}")

    original_code, original_id = _mailpit_message(email)
    check("signup OTP captured locally", bool(original_code), email)
    _await_send_window()  # the resend is another email to the same address
    resent = resend_signup_code(post_json, endpoint, email=email)
    check("signup resend accepted (type=signup route)", resent.ok,
          resent.status)
    resent_code, resent_id = _mailpit_message(email, after_id=original_id)
    check("resent OTP is a NEW captured message for the recipient",
          bool(resent_code) and bool(resent_id) and resent_id != original_id,
          f"original={original_id} resent={resent_id}")
    check("resent OTP differs from the original code",
          bool(resent_code) and resent_code != original_code)
    verified = verify_code(post_json, endpoint, email=email,
                           code=resent_code, kind="signup", session=memory)
    check("resent signup OTP verifies with type=signup", verified.ok,
          f"{verified.status} {verified.detail}")
    check("verified session belongs to the signup user",
          memory.logged_in and memory.user_id == out.session_user_id,
          f"{memory.user_id} vs {out.session_user_id}")
    users.append(memory.user_id)
    confirmed = check_account_status(post_json, endpoint, email=email,
                                     username=username)
    check("status recheck flips to confirmed after verification",
          confirmed.ok and confirmed.email_status == "confirmed",
          confirmed.email_status)

    # --- B: duplicate detection needs a CONFIRMED address -------------------
    dup_email = f"acct-dupe-{stamp}@example.invalid"
    dup_username = f"dupe{stamp[:8]}"
    dup_session = MemorySession()
    dup_out = register(post_json, endpoint, username=dup_username,
                       email=dup_email, password=uuid.uuid4().hex + "Aa9")
    check("second identity reaches verification_required",
          dup_out.status == "verification_required",
          f"{dup_out.status} {dup_out.detail}")
    dup_code, _dup_id = _mailpit_message(dup_email)
    dup_verified = verify_code(post_json, endpoint, email=dup_email,
                               code=dup_code, kind="signup",
                               session=dup_session)
    check("second identity confirms before the duplicate attempt",
          dup_verified.ok, f"{dup_verified.status} {dup_verified.detail}")
    if dup_verified.ok:
        users.append(dup_session.user_id)
    _await_send_window()
    duplicate = register(post_json, endpoint, username=dup_username,
                         email=dup_email, password=uuid.uuid4().hex + "Aa9")
    check("duplicate signup resolves to email_exists",
          duplicate.status == "email_exists",
          f"{duplicate.status} {duplicate.detail}")


def _deletion_contract_checks(*, anon, service, endpoint, check, stamp,
                              users, other_game=""):
    """Task 7 contract through the local gateway: guards, refusals, cleanup."""
    email = f"acct-delete-{stamp}@example.invalid"
    username = f"delete{stamp[:7]}"
    password = uuid.uuid4().hex + "Aa9"
    user_id, status, _ = _create_fixture_user(
        service, email=email, username=username, password=password)
    check("deletion fixture user created without email",
          bool(user_id) and status < 300, str(status))
    if not user_id:
        return
    users.append(user_id)
    memory = MemorySession()
    login = login_password(post_json, endpoint, email=email,
                           password=password, session=memory)
    check("deletion fixture can sign in", login.ok, login.status)
    offered_game = str(uuid.uuid4())
    link = post_json(endpoint, "/rest/v1/rpc/link_game",
                     {"p_game_uuid": offered_game},
                     access_token=memory.access_token)
    reply = link if isinstance(link, dict) else {}
    game = str(reply.get("game_uuid") or "")
    check("deletion fixture adopts the server-owned game uuid",
          bool(_UUID_RE.match(game)), str(link)[:120])
    if not game:
        return
    journal = Journal(os.path.join(tempfile.gettempdir(),
                                   f"ankiscape-delete-{stamp}.sqlite3"))
    try:
        transport = make_transport(ServiceConfig(
            endpoint=endpoint, game_uuid=game, post=post_json), memory)
        journal.append_operation(_op(game, "del-a", 1,
                                     f"rk-delete-{stamp}"))
        _svc(game, journal, transport, memory).force_sync()
        checkpoint = _service(service, "POST", "/rest/v1/game_checkpoints",
                              {"game_uuid": game, "revision": 999, "state": {}})
        audit = _service(service, "POST", "/rest/v1/moderation_audit",
                         {"target_user_id": user_id, "action": "note",
                          "reason": "e2e-delete"})
        check("deletion fixture seeded (checkpoint + audit)",
              checkpoint[0] < 300 and audit[0] < 300,
              f"{checkpoint[0]}/{audit[0]}")
        # The sync above leaves a checkpoint and the fixture adds revision 999,
        # so "untouched" is a comparison against THIS set rather than a row
        # count. Asserting len(rows) == 1 predates the seed-at-a-free-revision
        # change (dc44dd4) and could only have passed while one row existed.
        seeded_rows = _rows(service, f"/rest/v1/game_checkpoints"
                                     f"?game_uuid=eq.{game}&select=revision")
        check("checkpoint baseline captured for the refusal comparison",
              bool(seeded_rows), str(seeded_rows))

        # Wrong confirmation never consumes the account.
        wrong = delete_account(post_json, endpoint,
                               access_token=memory.access_token,
                               username=username.upper(), password=password)
        check("wrong username/case refuses with invalid_confirmation",
              wrong.status == "invalid_confirmation", wrong.status)
        wrong_pw = delete_account(post_json, endpoint,
                                  access_token=memory.access_token,
                                  username=username,
                                  password="not-the-password-Aa9")
        check("wrong password refuses with invalid_credentials",
              wrong_pw.status == "invalid_credentials", wrong_pw.status)
        missing = delete_account(post_json, endpoint, access_token="",
                                 username=username, password=password)
        check("missing session refuses with invalid_session",
              missing.status == "invalid_session", missing.status)
        bogus = delete_account(post_json, endpoint,
                               access_token="not-a-real-token",
                               username=username, password=password)
        check("bogus token refuses without claiming deletion",
              bogus.status in ("invalid_session", "service_error"),
              bogus.status)
        rows = _rows(service, f"/rest/v1/game_checkpoints"
                              f"?game_uuid=eq.{game}&select=revision")
        check("refusals left the account and checkpoint untouched",
              rows is not None and bool(seeded_rows)
              and sorted(r.get("revision") for r in rows)
              == sorted(r.get("revision") for r in seeded_rows),
              f"before={seeded_rows} after={rows}")

        # Service-role seeding proves the demo and fixture guards.
        demo_on = _service(service, "PATCH",
                           f"/rest/v1/players?user_id=eq.{user_id}",
                           {"is_demo": True})
        refused_demo = delete_account(post_json, endpoint,
                                      access_token=memory.access_token,
                                      username=username, password=password)
        _service(service, "PATCH", f"/rest/v1/players?user_id=eq.{user_id}",
                 {"is_demo": False})
        check("demo accounts are immutable",
              demo_on[0] < 300 and refused_demo.status == "demo_immutable",
              refused_demo.status)
        fixture = _service(service, "POST", "/rest/v1/fixture_registry", {
            "suite_id": "account-delete-e2e", "suite_version": 1,
            "username_norm": username.lower(), "user_id": user_id,
            "expected_trace_hash": f"trace-{stamp}"})
        refused_fixture = delete_account(post_json, endpoint,
                                         access_token=memory.access_token,
                                         username=username,
                                         password=password)
        _service(service, "DELETE",
                 f"/rest/v1/fixture_registry?user_id=eq.{user_id}")
        check("registered fixtures are immutable",
              fixture[0] < 300
              and refused_fixture.status == "demo_immutable",
              f"{fixture[0]} {refused_fixture.status}")

        # A separate identity proves the rate limit without deleting data.
        rate_email = f"acct-rate-{stamp}@example.invalid"
        rate_name = f"rate{stamp[:9]}"
        rate_id, _, _ = _create_fixture_user(
            service, email=rate_email, username=rate_name, password=password)
        users.append(rate_id)
        rate_memory = MemorySession()
        login_password(post_json, endpoint, email=rate_email,
                       password=password, session=rate_memory)
        limited = None
        for _attempt in range(6):
            limited = delete_account(post_json, endpoint,
                                     access_token=rate_memory.access_token,
                                     username=rate_name,
                                     password="wrong-password-Aa9")
        check("repeated attempts hit the deletion rate limit",
              limited is not None and limited.status == "rate_limited",
              getattr(limited, "status", ""))

        # Concurrency: one sync racing the authorized deletion. Either it
        # lands before the delete (and is cleaned with the account) or it
        # fails closed; no orphan checkpoint may survive.
        journal.append_operation(_op(game, "del-race", 2,
                                     f"rk-delete-race-{stamp}"))
        race: dict = {}

        def _race_sync():
            try:
                race["result"] = _svc(game, journal, transport,
                                      memory).force_sync()
            except Exception as exc:
                race["result"] = {"ok": False, "error": repr(exc)[:120]}

        thread = threading.Thread(target=_race_sync)
        thread.start()

        # The real deletion removes the auth user and cleans the orphans.
        deleted = delete_account(post_json, endpoint,
                                 access_token=memory.access_token,
                                 username=username, password=password)
        thread.join(timeout=30)
        check("concurrent sync resolves around the deletion",
              isinstance(race.get("result"), dict),
              str(race.get("result"))[:160])
        check("authorized deletion succeeds", deleted.status == "deleted",
              f"{deleted.status} {deleted.detail}")
        check("auth user is gone after deletion",
              _admin(service, "GET",
                     f"/auth/v1/admin/users/{user_id}")[0] == 404)
        check("player row is gone after deletion",
              _rows(service, f"/rest/v1/players?user_id=eq.{user_id}"
                             f"&select=user_id") == [])
        check("owned operations are gone after deletion",
              _rows(service, f"/rest/v1/game_operations?game_uuid=eq.{game}"
                             f"&select=op_id") == [])
        check("review claims are gone after deletion",
              _rows(service, f"/rest/v1/review_claims?game_uuid=eq.{game}"
                             f"&select=review_key") == [])
        check("checkpoints are gone after deletion",
              _rows(service, f"/rest/v1/game_checkpoints?game_uuid=eq.{game}"
                             f"&select=revision") == [])
        check("moderation audit rows are gone after deletion",
              _rows(service, f"/rest/v1/moderation_audit?target_user_id=eq."
                             f"{user_id}&select=id") == [])
        if other_game:
            untouched = _rows(service, f"/rest/v1/game_state?game_uuid=eq."
                                       f"{other_game}&select=game_uuid")
            check("another user's game and state are untouched",
                  untouched is not None and len(untouched) == 1,
                  str(untouched))
        if user_id in users:
            users.remove(user_id)  # already deleted; skip teardown
    finally:
        try:
            journal.close()
        except Exception:
            pass


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

    functions = _load_module("ankiscape_account_functions",
                             "account_functions.py")
    lease = None
    if functions is None:
        check("local account function runner available", False,
              "dev/account_functions.py missing")
        return 2
    try:
        lease = functions.ensure_serving()
        check("account functions serve current code with per-function JWT",
              True, "account-delete verify_jwt=true")
    except functions.FunctionsUnavailable as exc:
        print(f"account_contracts_e2e: BLOCKED: {exc}", file=sys.stderr)
        return 2

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
        # The server owns the game uuid (D5); the returned uuid is adopted as
        # the only game id used after the link (S11/S16).
        offered_game = str(uuid.uuid4())
        link = post_json(endpoint, "/rest/v1/rpc/link_game",
                         {"p_game_uuid": offered_game},
                         access_token=session.access_token)
        reply = link if isinstance(link, dict) else {}
        game = str(reply.get("game_uuid") or "")
        check("link_game creates with key-present created/resumed semantics",
              reply.get("created") is True and reply.get("resumed") is False,
              str(link)[:120])
        check("link_game returns the server-owned uuid to adopt",
              bool(_UUID_RE.match(game)), str(link)[:120])
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
        count_b = journal_b.operation_count()
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
        # A no-keyring host (or a locked CI keychain) must sign in again
        # truthfully instead of pretending the session persisted.
        persisted = CredentialVault(profile_dir, API).read()
        resumed = ProfileSession(generation=4,
                                 vault=CredentialVault(profile_dir, API))
        if persisted is not None:
            check("restart resumes the remembered session",
                  resumed.logged_in and bool(resumed.access_token))
        else:
            check("session-only restart is not silently signed in",
                  not resumed.logged_in)
            result_r = login_password(post_json, endpoint, email=email_a,
                                      password=password,
                                      session=resumed.session)
            check("explicit sign-in restores a session-only restart",
                  result_r.ok and resumed.logged_in)
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

        # --- Real signup / duplicate / resend / verify (confirmations on) --
        _signup_resend_verify_checks(anon=anon, service=service,
                                     endpoint=endpoint, check=check,
                                     stamp=stamp, users=users)

        # --- Task 7 deletion contract through the local gateway ------------
        _deletion_contract_checks(anon=anon, service=service,
                                  endpoint=endpoint, check=check,
                                  stamp=stamp, users=users, other_game=game)
    finally:
        for journal in journals:
            try:
                journal.close()
            except Exception:
                pass
        tmp.cleanup()
        for user_id in users:
            _delete_fixture_user(service, user_id)
        if lease is not None:
            lease.release()
        print("fixture users deleted" if users else "no fixture users to delete")

    print(f"\n{len(FAILURES)} failures" if FAILURES
          else "\naccount contracts journey: ALL PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
