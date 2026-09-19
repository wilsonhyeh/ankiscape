#!/usr/bin/env python3
# dev/prod_e2e.py - Prod-stack online/recovery journey (dev only, never shipped).
"""Runs the service layer against the REAL hosted project (no mocks, no real
email). Admin-provisions users via the Auth Admin API (service-role key from
env, pre-confirmed emails), then exercises the identical flow as the local
sync journey: link -> offline credit -> service upload -> second-device
download/merge -> id-conflict -> lost-ack retry.

Cleanup: test games are unlinked + operations deleted via REST (service
role); theotherm test users are deleted via Auth Admin. Always runs cleanup,
even on failure.

Environment:
  ANKISCAPE_PROD_URL (https://<ref>.supabase.co)
  ANKISCAPE_PROD_ANON_KEY (sb_publishable_... or anon JWT)
  SUPABASE_SERVICE_ROLE_KEY (service_role JWT; env only, never committed)

Usage: python3 dev/prod_e2e.py
Exits nonzero on any failure. Creates REAL rows in the prod project (test
users + test games, all prefixed e2e-) and deletes them afterwards.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import uuid
from fractions import Fraction

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

from evolved.auth import MemorySession  # noqa: E402
from evolved.draws import draw_r, frac_hits  # noqa: E402
from evolved import reviews as reviews_mod  # noqa: E402
from evolved.journal import Journal  # noqa: E402
from evolved.net import Endpoint, post_json  # noqa: E402
from evolved.service import ServiceConfig, SyncService, make_transport  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _admin(method, path, body=None):
    base = os.environ["ANKISCAPE_PROD_URL"]
    svc = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "apikey": svc,
                                          "Authorization": f"Bearer {svc}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def _authed(method, path, body, token, anon):
    base = os.environ["ANKISCAPE_PROD_URL"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "apikey": anon,
                                          "Authorization": f"Bearer {token}",
                                          "X-AnkiScape-Protocol": "2"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "{}"
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def _op(game_uuid, device_id, seq, lamport, review_key):
    return {"op_id": str(uuid.uuid4()), "game_uuid": game_uuid,
            "device_id": device_id, "device_seq": seq, "lamport": lamport,
            "kind": "review_award", "payload": {"review_key": review_key}}


def _registry_owned_user_ids() -> set:
    """User ids held by the private fixture registry (never disposable)."""
    try:
        status, rows = _admin("GET", "/rest/v1/fixture_registry?select=user_id")
        if status == 200 and isinstance(rows, list):
            return {str(row.get("user_id")) for row in rows if row.get("user_id")}
    except Exception:
        pass
    return set()


def main():
    for var in ("ANKISCAPE_PROD_URL", "ANKISCAPE_PROD_ANON_KEY",
                "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"prod_e2e: ERROR: set {var} in the environment")
            return 2
    anon = os.environ["ANKISCAPE_PROD_ANON_KEY"]
    endpoint = Endpoint(base_url=os.environ["ANKISCAPE_PROD_URL"],
                        project_key=anon)
    stamp = int(time.time())
    created_users, games = [], []
    journals = []

    def cleanup():
        # Registry-owned permanent fixtures are never deletable by disposable
        # smoke cleanup: refuse and report instead of taking over or erasing
        # the hosted test cohort.
        protected = _registry_owned_user_ids()
        blocked = [uid for uid in created_users if uid in protected]
        if blocked:
            print("prod_e2e: refusing to delete registry-owned fixture "
                  f"accounts: {blocked}")
            created_users[:] = [uid for uid in created_users if uid not in protected]
        for game in games:
            _admin("DELETE", f"/rest/v1/game_operations?game_uuid=eq.{game}")
            _admin("DELETE", f"/rest/v1/game_state?game_uuid=eq.{game}")
            _admin("PATCH", f"/rest/v1/players?game_uuid=eq.{game}",
                   {"game_uuid": None})
        for uid in created_users:
            _admin("DELETE", f"/auth/v1/admin/users/{uid}")
        for journal, tmp in journals:
            try:
                journal.close()
            except Exception:
                pass
            try:
                tmp.cleanup()
            except Exception:
                pass

    try:
        # 1. Admin-provision two users (pre-confirmed; @example.com never delivers).
        sessions = []
        usernames = {}
        for tag in ("a", "b"):
            username = f"e2ep{tag}{stamp % 100000}"
            usernames[tag] = username
            email = f"e2ep{tag}{stamp}@example.com"
            status, user = _admin("POST", "/auth/v1/admin/users",
                                  {"email": email, "password": "correct horse 9",
                                   "email_confirm": True,
                                   "user_metadata": {"username_norm": username,
                                                     "username_display": username.title()}})
            check(f"prod admin creates user {tag}", status in (200, 201),
                  f"{status} {str(user)[:150]}")
            if status not in (200, 201):
                return 1
            uid = user["id"] if isinstance(user, dict) else user.get("id", "")
            created_users.append(uid)
            # Trigger should have claimed the username; verify the row.
            status, rows = _admin("GET", f"/rest/v1/players?select=username_norm&user_id=eq.{uid}")
            ok = status == 200 and rows and rows[0].get("username_norm") == username
            check(f"prod trigger claimed username {tag}", ok, f"{status} {str(rows)[:150]}")
            # Password grant -> managed session.
            status, sess = _authed("POST", "/auth/v1/token?grant_type=password",
                                   {"email": email, "password": "correct horse 9"},
                                   token="", anon=anon)
            check(f"prod password grant {tag}",
                  status == 200 and bool(sess.get("access_token")),
                  f"{status} {str(sess)[:150]}")
            if status != 200:
                return 1
            mem = MemorySession()
            mem.set(access_token=sess["access_token"],
                    refresh_token=sess.get("refresh_token", ""),
                    user_id=sess["user"]["id"])
            sessions.append(mem)

        mem_a, mem_b = sessions
        offered_game = str(uuid.uuid4())

        # 2. A links, credits 2 ops offline, uploads via the real service.
        # D5: the server owns the game uuid; adopt the returned uuid as the
        # only game id used afterwards (S11/S16). created/resumed are pinned
        # complementary and `created` is a key-presence rule.
        status, data = _authed("POST", "/rest/v1/rpc/link_game",
                               {"p_game_uuid": offered_game},
                               token=mem_a.access_token, anon=anon)
        reply = data if isinstance(data, dict) else {}
        game_uuid = str(reply.get("game_uuid") or "")
        check("prod link_game creates with the pinned reply",
              status in (200, 201) and reply.get("created") is True
              and reply.get("resumed") is False, f"{status} {data}")
        check("prod link_game returns the server-owned uuid to adopt",
              bool(_UUID_RE.match(game_uuid)) and game_uuid != offered_game,
              f"{status} {data}")
        games.append(game_uuid)
        tmp_a = tempfile.TemporaryDirectory()
        journal_a = Journal(os.path.join(tmp_a.name, "a.sqlite3"))
        journals.append((journal_a, tmp_a))
        op_a1 = _op(game_uuid, "dev-a", 1, 1, f"rk-prod-a1-{stamp}")
        op_a2 = _op(game_uuid, "dev-a", 2, 2, f"rk-prod-a2-{stamp}")
        journal_a.append_operation(op_a1)
        journal_a.append_operation(op_a2)
        cfg_a = ServiceConfig(endpoint=endpoint, game_uuid=game_uuid, post=post_json)
        svc_a = SyncService(generation=21, game_uuid=game_uuid, journal=journal_a,
                            transport=make_transport(cfg_a, mem_a),
                            apply_remote=lambda page: None,
                            get_user_id=lambda: mem_a.user_id)
        out = svc_a.maybe_sync(manual=True)
        check("prod service upload ok", out.get("ok") is True, str(out)[:200])
        check("prod outbox drained", journal_a.pending_operations() == [])

        # 3. Second device downloads + merges.
        tmp_b = tempfile.TemporaryDirectory()
        journal_b = Journal(os.path.join(tmp_b.name, "b.sqlite3"))
        journals.append((journal_b, tmp_b))
        svc_b = SyncService(generation=22, game_uuid=game_uuid, journal=journal_b,
                            transport=make_transport(cfg_a, mem_a),
                            apply_remote=lambda page: None,
                            get_user_id=lambda: mem_a.user_id)
        out = svc_b.maybe_sync(manual=True)
        check("prod download+merge ok", out.get("ok") is True, str(out)[:200])
        ids_b = sorted(journal_b.operation_ids())
        check("prod second device converged",
              ids_b == sorted([op_a1["op_id"], op_a2["op_id"]]),
              str(ids_b)[:150])

        # 4. Changed-payload conflict reported by the server.
        poison = dict(op_a1, device_id="dev-b", device_seq=99, lamport=99,
                      payload={"review_key": "rk-PROD-POISON"})
        status, data = _authed("POST", "/rest/v1/rpc/submit_operations",
                               {"p_game_uuid": game_uuid, "p_ops": [poison]},
                               token=mem_a.access_token, anon=anon)
        check("prod id-conflict reported",
              status == 200 and len(data.get("conflicts", [])) == 1,
              f"{status} {data}")

        # 5. Lost-ack retry converges with no dupes.
        op_a3 = _op(game_uuid, "dev-a", 3, 3, f"rk-prod-a3-{stamp}")
        journal_a.append_operation(op_a3)
        real_post = post_json
        dropped = {"n": 0}

        def _flaky_post(ep, path, payload, access_token=None, headers=None):
            kwargs = {"access_token": access_token}
            if headers is not None:
                kwargs["headers"] = headers
            if path.endswith("submit_operations") and dropped["n"] == 0:
                dropped["n"] += 1
                real_post(ep, path, payload, **kwargs)
                raise ConnectionError("ack lost after commit")
            return real_post(ep, path, payload, **kwargs)

        cfg_flaky = ServiceConfig(endpoint=endpoint, game_uuid=game_uuid,
                                  post=_flaky_post)
        svc_flaky = SyncService(
            generation=21, game_uuid=game_uuid, journal=journal_a,
            transport=make_transport(cfg_flaky, mem_a),
            apply_remote=lambda page: None,
            get_user_id=lambda: mem_a.user_id)
        out = svc_flaky.maybe_sync(manual=True)
        check("prod lost-ack surfaces transient", out.get("ok") is False,
              str(out)[:150])
        svc_retry = SyncService(
            generation=21, game_uuid=game_uuid, journal=journal_a,
            transport=make_transport(cfg_a, mem_a),
            apply_remote=lambda page: None,
            get_user_id=lambda: mem_a.user_id)
        out = svc_retry.maybe_sync(manual=True)
        check("prod retry converges", out.get("ok") is True, str(out)[:150])
        check("prod no dupes pending", journal_a.pending_operations() == [])

        # 6. Server-authoritative scoring: the hosted project recomputes XP
        # from stored operations, and a retraction reverses it.
        status, profile = _authed(
            "POST", "/rest/v1/rpc/public_profile",
            {"p_username_norm": usernames["a"]},
            token=mem_a.access_token, anon=anon)
        try:
            xp_before = int(((profile.get("state") or {}).get("xp") or {})
                            .get("mining", "0"))
        except (AttributeError, TypeError, ValueError):
            xp_before = -1
        check("prod scoring wrote XP", status == 200 and xp_before > 0,
              f"{status} mining={xp_before}")

        retract = {"op_id": str(uuid.uuid4()), "device_id": "dev-a",
                   "device_seq": 50, "lamport": 50,
                   "kind": "review_retract",
                   "payload": {"target_review_key":
                               op_a1["payload"]["review_key"],
                               "reason": "prod_e2e"}}
        status, data = _authed(
            "POST", "/rest/v1/rpc/submit_operations",
            {"p_game_uuid": game_uuid, "p_ops": [retract]},
            token=mem_a.access_token, anon=anon)
        check("prod retraction accepted",
              status == 200 and int(data.get("applied", -1)) == 1,
              f"{status} {str(data)[:150]}")
        status, profile2 = _authed(
            "POST", "/rest/v1/rpc/public_profile",
            {"p_username_norm": usernames["a"]},
            token=mem_a.access_token, anon=anon)
        try:
            xp_after = int(((profile2.get("state") or {}).get("xp") or {})
                           .get("mining", "0"))
        except (AttributeError, TypeError, ValueError):
            xp_after = -1
        check("prod retraction reversed XP",
              status == 200 and 0 <= xp_after < xp_before,
              f"mining {xp_before} -> {xp_after}")

        # 7. An old client without the protocol header is told to update
        # instead of being silently mis-scored.
        try:
            req = urllib.request.Request(
                os.environ["ANKISCAPE_PROD_URL"]
                + "/rest/v1/rpc/submit_operations",
                data=json.dumps({"p_game_uuid": game_uuid,
                                 "p_ops": [op_a1]}).encode(),
                method="POST",
                headers={"Content-Type": "application/json",
                         "apikey": anon,
                         "Authorization": f"Bearer {mem_a.access_token}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                legacy_status, legacy_body = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            legacy_status = exc.code
            legacy_body = exc.read().decode()
        check("prod old client update required",
              legacy_status == 400 and "client_update_required" in legacy_body,
              f"{legacy_status} {legacy_body[:120]}")

        # 8. Username login via the deployed Edge Function (generic failures).
        status, sess = _authed("POST", "/functions/v1/username-login",
                               {"username": f"e2epa{stamp % 100000}",
                                "password": "correct horse 9"},
                               token="", anon=anon)
        check("prod username-login works",
              status == 200 and bool(sess.get("access_token")),
              f"{status} {str(sess)[:150]}")
        status, _ = _authed("POST", "/functions/v1/username-login",
                            {"username": f"e2epa{stamp % 100000}",
                             "password": "wrong password 9"},
                            token="", anon=anon)
        check("prod username-login rejects bad password", status == 401,
              f"status {status}")

        # 9. Migration 0005 behavior: a mined gem is real loot. Submits one
        # Clay mining op whose review key deterministically rolls success +
        # gem drop + sapphire for a fresh level-1 game, then reads the
        # replayed server state. Also proves the bonus XP used the ore's
        # tier multiplier (5 x 1.05 + 50 x 1.05 = 57.75).
        username_c = f"e2epc{stamp % 100000}"
        status, user = _admin("POST", "/auth/v1/admin/users",
                              {"email": f"e2epc{stamp}@example.com",
                               "password": "correct horse 9",
                               "email_confirm": True,
                               "user_metadata": {"username_norm": username_c,
                                                 "username_display": username_c.title()}})
        check("prod admin creates gem-check user", status in (200, 201),
              f"{status} {str(user)[:150]}")
        if status in (200, 201):
            uid_c = user["id"]
            created_users.append(uid_c)
            status, sess = _authed(
                "POST", "/auth/v1/token?grant_type=password",
                {"email": f"e2epc{stamp}@example.com",
                 "password": "correct horse 9"}, token="", anon=anon)
            check("prod gem-check password grant",
                  status == 200 and bool(sess.get("access_token")),
                  f"{status} {str(sess)[:150]}")
            mem_c = MemorySession()
            mem_c.set(access_token=sess.get("access_token", ""),
                      refresh_token=sess.get("refresh_token", ""),
                      user_id=(sess.get("user") or {}).get("id", ""))
            offered_c = str(uuid.uuid4())
            status_c, reply_c = _authed(
                "POST", "/rest/v1/rpc/link_game", {"p_game_uuid": offered_c},
                token=mem_c.access_token, anon=anon)
            reply_c = reply_c if isinstance(reply_c, dict) else {}
            game_c = str(reply_c.get("game_uuid") or "")
            # The gem search, the op envelope and every later call use the
            # server-returned uuid only (S16).
            check("prod gem-check link adopts the pinned reply",
                  status_c in (200, 201) and reply_c.get("created") is True
                  and reply_c.get("resumed") is False
                  and bool(_UUID_RE.match(game_c)), f"{status_c} {reply_c}")
            if game_c:
                games.append(game_c)
                # Fresh level-1 Clay: success chance min(.80+.02, .95) x .90.
                prob = Fraction(82, 100) * Fraction(9, 10)
                gem_key = ""
                for rid in range(1, 200_001):
                    candidate = reviews_mod.make_review_key(game_c, rid,
                                                            rid + 1000)
                    if (frac_hits(draw_r(1, game_c, candidate, "action"), prob)
                            and frac_hits(draw_r(1, game_c, candidate,
                                                 "gem_drop"), Fraction(1, 256))
                            and frac_hits(draw_r(1, game_c, candidate,
                                                 "gem_pick"), Fraction(1, 4))):
                        gem_key = candidate
                        break
                check("prod deterministic gem key found", bool(gem_key))
                if gem_key:
                    status, data = _authed(
                        "POST", "/rest/v1/rpc/submit_operations",
                        {"p_game_uuid": game_c, "p_ops": [{
                            "op_id": str(uuid.uuid4()), "game_uuid": game_c,
                            "device_id": "dev-c", "device_seq": 1, "lamport": 1,
                            "kind": "review_award",
                            "payload": {"review_key": gem_key,
                                        "review_ts": 1_800_000_000, "rating": 3,
                                        "review_kind": "review",
                                        "provenance": "direct",
                                        "reward_policy": 2, "skill": "mining",
                                        "resource": "Clay"}}]},
                        token=mem_c.access_token, anon=anon)
                    check("prod gem op accepted",
                          status == 200 and not data.get("conflicts"),
                          f"{status} {str(data)[:200]}")
                    status, state = _authed(
                        "POST", "/rest/v1/rpc/get_game_state",
                        {"p_game_uuid": game_c}, token=mem_c.access_token,
                        anon=anon)
                    state = state if isinstance(state, dict) else {}
                    inventory = state.get("inventory") or {}
                    xp = (state.get("xp") or {}).get("mining", 0)
                    check("prod mined gem granted to Bank",
                          status == 200 and inventory.get("Uncut sapphire") == 1,
                          f"{status} {str(state)[:200]}")
                    check("prod gem bonus XP applied (57.75)",
                          int(xp) == 57_750_000, f"xp={xp}")
    finally:
        cleanup()
        check("prod cleanup ran", True)
    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nprod journey: ALL PASS")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    import urllib.error  # noqa: E402

    raise SystemExit(main())
