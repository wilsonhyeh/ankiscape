#!/usr/bin/env python3
# dev/seed_hosted_fixtures.py - Provision and seed the permanent hosted test
# cohort (dev only; never shipped).
"""Modes:

  --plan    show the suite, expected trace hashes and intended actions
  --apply   provision identities + replay the deterministic traces
  --verify  compare server state, cohort isolation and standings

Targets:
  --local   local Supabase stack (status keys read live)
  --hosted  production project from ANKISCAPE_PROD_URL +
            SUPABASE_SERVICE_ROLE_KEY (provisioning only) + anon key

Guarantees:
  * classification happens BEFORE any public player state exists: the private
    registry reservation is inserted first, and the registration trigger
    creates the players row already is_test=true;
  * passwords derive from ANKISCAPE_FIXTURE_SECRET (approved secret storage);
    nothing is written into the package or evidence;
  * gameplay goes through each user's normal token and the real upload/replay
    RPCs; authoritative scores are never written directly;
  * reruns adopt registry-owned identities and replay identical operations
    (deterministic op ids); nothing is deleted and history never grows;
  * one run at a time, <=2 requests/second, bounded retries, abort on
    repeated 429/5xx. No production load or denial test.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib.util  # noqa: E402


def _load_fixture_traces():
    path = os.path.join(ROOT, "dev", "fixture_traces.py")
    spec = importlib.util.spec_from_file_location("ankiscape_fixture_traces",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


traces_mod = _load_fixture_traces()

MAX_RPS = 2.0
TIMEOUT_S = 30
MAX_RETRIES = 3
ABORT_AFTER = 3
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class SeedError(Exception):
    pass


class Transport:
    """Minimal stdlib HTTP with rate limiting, retries and abort bounds."""

    def __init__(self, base_url: str, anon_key: str, service_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.anon_key = anon_key
        self.service_key = service_key
        self._last = 0.0
        self._server_errors = 0

    def request(self, method: str, path: str, *, body: Any = None,
                params: Optional[Dict[str, str]] = None,
                token: str = "", service: bool = False,
                headers_extra: Optional[Dict[str, str]] = None,
                expect: Tuple[int, ...] = (200, 201, 204)) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = None
        headers = {"apikey": self.service_key if service else self.anon_key,
                   "Accept": "application/json",
                   # Same protocol marker the shipped client sends; servers
                   # with authoritative scoring reject old-protocol uploads.
                   "X-AnkiScape-Protocol": "2"}
        for name, value in (headers_extra or {}).items():
            headers[str(name)] = str(value)
        if service:
            headers["Authorization"] = f"Bearer {self.service_key}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            # <=2 requests/second
            wait = self._last + (1.0 / MAX_RPS) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            request = urllib.request.Request(url, data=payload, headers=headers,
                                             method=method)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
                    raw = resp.read()
                    if resp.status in expect:
                        return json.loads(raw.decode("utf-8")) if raw else None
                    raise SeedError(f"unexpected status {resp.status}: {raw[:200]!r}")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code in expect:
                    return json.loads(raw.decode("utf-8")) if raw else None
                if exc.code in (429,) or exc.code >= 500:
                    self._server_errors += 1
                    if self._server_errors >= ABORT_AFTER:
                        raise SeedError(
                            f"aborting after {ABORT_AFTER} server errors "
                            f"(last {exc.code})")
                    if attempt < MAX_RETRIES:
                        attempt += 1
                        time.sleep(min(8.0, 2.0 * attempt))
                        continue
                detail = raw.decode("utf-8", errors="replace")[:300]
                raise SeedError(f"http {exc.code}: {detail}")
            except urllib.error.URLError as exc:
                if attempt < MAX_RETRIES:
                    attempt += 1
                    time.sleep(min(8.0, 2.0 * attempt))
                    continue
                raise SeedError(f"transport unavailable: {exc!r}")


def password_for(secret: str, name: str) -> str:
    if not secret:
        raise SeedError("ANKISCAPE_FIXTURE_SECRET is required for --apply")
    message = f"ankiscape-fixture:{traces_mod.SUITE_ID}:" \
              f"v{traces_mod.SUITE_VERSION}:{name}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:24]


def _supabase_status_keys() -> Dict[str, str]:
    try:
        proc = subprocess.run(["supabase", "status", "-o", "env"],
                              capture_output=True, text=True, timeout=30,
                              cwd=os.path.join(ROOT, "server"))
    except (OSError, subprocess.SubprocessError):
        return {}
    keys = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, _sep, value = line.partition("=")
            keys[key.strip()] = value.strip().strip('"')
    return keys


def resolve_target(local: bool, hosted: bool) -> Dict[str, str]:
    if local and hosted:
        raise SeedError("choose --local or --hosted, not both")
    if hosted:
        url = os.environ.get("ANKISCAPE_PROD_URL", "").rstrip("/")
        anon = os.environ.get("ANKISCAPE_PROD_ANON_KEY", "")
        service = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url:
            raise SeedError("ANKISCAPE_PROD_URL is required for --hosted")
        if not anon:
            raise SeedError("ANKISCAPE_PROD_ANON_KEY is required for --hosted")
        return {"kind": "hosted", "url": url, "anon": anon, "service": service}
    status = _supabase_status_keys()
    url = os.environ.get("ANKISCAPE_LOCAL_URL") or status.get("API_URL", "")
    anon = (os.environ.get("ANKISCAPE_LOCAL_ANON_KEY")
            or status.get("ANON_KEY", ""))
    service = (os.environ.get("ANKISCAPE_LOCAL_SERVICE_ROLE_KEY")
               or status.get("SERVICE_ROLE_KEY", ""))
    if not url:
        raise SeedError("local stack URL unavailable; run `supabase start` "
                        "or set ANKISCAPE_LOCAL_URL")
    return {"kind": "local", "url": url, "anon": anon, "service": service}


def suite_traces() -> Dict[str, Dict[str, Any]]:
    suite = traces_mod.load_suite()
    names = suite["display_names"]
    if len(names) != 24 or len(set(traces_mod.username_norm(n) for n in names)) != 24:
        raise SeedError("hosted-v1 must define exactly 24 unique display names")
    return traces_mod.build_traces(names)


def _registry(transport: Transport) -> List[Dict[str, Any]]:
    rows = transport.request(
        "GET", "/rest/v1/fixture_registry",
        params={"select": "*", "suite_id": f"eq.{traces_mod.SUITE_ID}",
                "suite_version": f"eq.{traces_mod.SUITE_VERSION}"},
        service=True)
    return rows or []


def _reserve(transport: Transport, norm: str, trace_hash: str,
             reserved_email: str) -> None:
    transport.request(
        "POST", "/rest/v1/fixture_registry",
        body={"suite_id": traces_mod.SUITE_ID,
              "suite_version": traces_mod.SUITE_VERSION,
              "username_norm": norm, "expected_trace_hash": trace_hash,
              "reserved_email": reserved_email},
        service=True, expect=(200, 201, 204, 409))


def _update_registry(transport: Transport, norm: str, fields: Dict[str, Any]) -> None:
    transport.request(
        "PATCH", "/rest/v1/fixture_registry",
        params={"suite_id": f"eq.{traces_mod.SUITE_ID}",
                "suite_version": f"eq.{traces_mod.SUITE_VERSION}",
                "username_norm": f"eq.{norm}"},
        body=fields, service=True, expect=(200, 204))


def _players_by_norm(transport: Transport, norms: List[str]) -> Dict[str, Dict]:
    if not norms:
        return {}
    rows = transport.request(
        "GET", "/rest/v1/players",
        params={"select": "username_norm,is_test,game_uuid,status",
                "username_norm": "in.(" + ",".join(norms) + ")"},
        service=True)
    return {str(r["username_norm"]): r for r in (rows or [])}


def _create_user(transport: Transport, name: str, norm: str,
                 secret: str) -> Dict[str, Any]:
    body = {"email": traces_mod.email_for(name),
            "password": password_for(secret, name),
            "email_confirm": True,
            "user_metadata": {"username_norm": norm,
                              "username_display": traces_mod.display_name(name)}}
    return transport.request("POST", "/auth/v1/admin/users", body=body,
                             service=True, expect=(200, 201))


def _sign_in(transport: Transport, email: str, password: str) -> str:
    data = transport.request(
        "POST", "/auth/v1/token",
        params={"grant_type": "password"},
        body={"email": email, "password": password}, expect=(200,))
    token = str((data or {}).get("access_token") or "")
    if not token:
        raise SeedError(f"sign-in returned no token for {email}")
    return token


def _provision_one(transport: Transport, name: str, trace: Dict[str, Any],
                   secret: str, reserved: Dict[str, Dict]) -> Dict[str, Any]:
    norm = traces_mod.username_norm(name)
    email = traces_mod.email_for(name)
    row = reserved.get(norm, {})
    user_id = row.get("user_id")
    if not user_id:
        try:
            created = _create_user(transport, name, norm, secret)
            user_id = str((created.get("id") or created.get("user", {}).get("id")))
        except SeedError as exc:
            if "username_taken" not in str(exc) and "already" not in str(exc):
                raise
            # Natural name belongs to an ordinary player: suffix 2..99.
            for suffix in range(2, 100):
                candidate = f"{name}{suffix}"
                if len(traces_mod.username_norm(candidate)) > 20:
                    continue
                norm = traces_mod.username_norm(candidate)
                if norm in reserved:
                    continue
                email = traces_mod.email_for(candidate)
                _reserve(transport, norm, trace["trace_hash"], email)
                try:
                    created = _create_user(transport, candidate, norm, secret)
                except SeedError as exc2:
                    if "username_taken" in str(exc2) or "already" in str(exc2):
                        continue
                    raise
                user_id = str((created.get("id") or created.get("user", {}).get("id")))
                reserved[norm] = {"username_norm": norm, "user_id": user_id,
                                  "expected_trace_hash": trace["trace_hash"],
                                  "reserved_email": email}
                break
        if not user_id:
            raise SeedError(f"could not provision a free name for {name}")
    token = _sign_in(transport, email, password_for(secret, name))
    return {"user_id": user_id, "token": token, "username_norm": norm,
            "email": email}


def _linked_game_uuid(transport: Transport, trace: Dict[str, Any],
                      token: str) -> str:
    """Call link_game and return the server-owned uuid to use from then on.

    D5: the offered trace uuid is ignored for identity. `created` is a
    key-presence rule, never `created is True`, and resumed/created are pinned
    complementary (S11/S16). Returns "" when the reply violates the contract."""
    reply = transport.request("POST", "/rest/v1/rpc/link_game",
                              body={"p_game_uuid": trace["game_uuid"]},
                              token=token, expect=(200,))
    reply = reply if isinstance(reply, dict) else {}
    created = reply.get("created")
    adopted = str(reply.get("game_uuid") or "")
    if (created is not True and created is not False) \
            or reply.get("resumed") != (not created) \
            or not _UUID_RE.match(adopted):
        return ""
    return adopted


def _submit_trace(transport: Transport, trace: Dict[str, Any], token: str,
                  batch_size: int) -> Dict[str, Any]:
    """Link, then replay the trace under the SERVER-owned game uuid (D5).

    p_game_uuid is offered only; the reply's `game_uuid` is adopted and is the
    only game id used for the submits (S11/S16). The returned dict carries the
    adopted uuid so callers key `get_game_state` and registry writes on it."""
    adopted = _linked_game_uuid(transport, trace, token)
    if not adopted:
        raise SeedError(f"{trace['display']}: link_game reply violates the "
                        "created/resumed contract")
    accepted = 0
    applied = 0
    ops = trace["ops"]
    for start in range(0, len(ops), batch_size):
        batch = ops[start:start + batch_size]
        result = transport.request("POST", "/rest/v1/rpc/submit_operations",
                                   body={"p_game_uuid": adopted, "p_ops": batch},
                                   token=token, expect=(200,))
        accepted += len((result or {}).get("accepted") or [])
        applied += int((result or {}).get("applied") or 0)
        conflicts = (result or {}).get("conflicts") or []
        if conflicts:
            raise SeedError(f"{trace['display']}: conflicts {conflicts[:2]}")
    return {"accepted": accepted, "applied": applied, "ops": len(ops),
            "game_uuid": adopted}


def _server_state(transport: Transport, game: str, token: str) -> Dict:
    return transport.request("POST", "/rest/v1/rpc/get_game_state",
                             body={"p_game_uuid": game}, token=token,
                             expect=(200,))


def _compare_state(trace: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    problems = []
    expected = trace["expected"]
    xp = state.get("xp") or {}
    for skill, value in expected["xp_micro"].items():
        if int(xp.get(skill, 0) or 0) != int(value):
            problems.append(f"{skill}: xp {xp.get(skill)} != {value}")
    for item, value in expected["inventory"].items():
        if int(value) and int((state.get("inventory") or {}).get(item, 0)) != int(value):
            problems.append(f"inventory {item}: "
                            f"{(state.get('inventory') or {}).get(item)} != {value}")
    checkpoint = state.get("checkpoint") or {}
    levels = checkpoint.get("levels") or {}
    for skill, value in expected["levels"].items():
        if int(levels.get(skill, 0) or 0) != int(value):
            problems.append(f"{skill}: level {levels.get(skill)} != {value}")
    return problems


def _expected_standings(traces: Dict[str, Dict[str, Any]], skill: str):
    rows = []
    for name, trace in traces.items():
        xp = int(trace["expected"]["xp_micro"].get(skill, 0))
        rows.append((traces_mod.username_norm(name), xp))
    rows.sort(key=lambda t: (-t[1], t[0]))
    ranks = {}
    previous = None
    for index, (norm, xp) in enumerate(rows, 1):
        if previous is not None and xp == previous[1]:
            ranks[norm] = ranks[previous[0]]
        else:
            ranks[norm] = index
        previous = (norm, xp)
    return rows, ranks


def cmd_plan(transport: Optional[Transport]) -> int:
    suite = traces_mod.suite_summary()
    print(f"fixture suite {suite['suite_id']} v{suite['suite_version']}: "
          f"{suite['players']} players, {suite['total_ops']} ops "
          f"(max {suite['max_player_ops']}/player, batches of {suite['batch_size']})")
    for name, info in suite["traces"].items():
        print(f"  {name:<14} level {info['total_level']:<3} "
              f"ops {info['ops']:<4} {info['trace_hash'][:16]}")
    if transport is None:
        print("plan: no target configured; nothing will be provisioned")
        return 0
    reserved = {r["username_norm"]: r for r in _registry(transport)}
    existing = _players_by_norm(transport, list(reserved.keys()))
    conflicts = [n for n, row in existing.items() if not row.get("is_test")]
    print(f"plan: registry rows={len(reserved)} "
          f"provisioned={sum(1 for r in reserved.values() if r.get('user_id'))} "
          f"ordinary-name-conflicts={conflicts}")
    print("plan: apply would create missing identities, replay traces in "
          "<=200-op batches, and never delete or reset state.")
    return 0


def cmd_apply(args) -> int:
    target = resolve_target(args.local, args.hosted)
    secret = os.environ.get("ANKISCAPE_FIXTURE_SECRET", "")
    if not secret:
        raise SeedError("ANKISCAPE_FIXTURE_SECRET is required for --apply")
    if len(secret) < 16:
        raise SeedError("ANKISCAPE_FIXTURE_SECRET must be at least 16 characters")
    transport = Transport(target["url"], target["anon"], target["service"])
    if not target["service"]:
        raise SeedError("service role key is required for provisioning")
    suite = traces_mod.load_suite()
    traces = suite_traces()
    batch = int(suite["trace"]["batch_size"])
    reserved = {r["username_norm"]: r for r in _registry(transport)}
    created = 0
    seeded = 0
    for name in suite["display_names"]:
        trace = traces[name]
        norm = traces_mod.username_norm(name)
        if norm not in reserved:
            _reserve(transport, norm, trace["trace_hash"], trace["email"])
            reserved[norm] = {"username_norm": norm, "user_id": None,
                              "expected_trace_hash": trace["trace_hash"],
                              "reserved_email": trace["email"]}
        provisioned = _provision_one(transport, name, trace, secret, reserved)
        created += 1 if not reserved.get(
            provisioned["username_norm"], {}).get("user_id") else 0
        stats = _submit_trace(transport, trace, provisioned["token"], batch)
        state = _server_state(transport, stats["game_uuid"],
                              provisioned["token"])
        problems = _compare_state(trace, state)
        if problems:
            raise SeedError(f"{name}: server state mismatch: {problems[:3]}")
        _update_registry(transport, provisioned["username_norm"], {
            "user_id": provisioned["user_id"],
            "game_uuid": stats["game_uuid"],
            "expected_trace_hash": trace["trace_hash"], "seed_state": "seeded"})
        seeded += 1
        print(f"seed: {name:<14} ops={stats['ops']} accepted={stats['accepted']} "
              f"newly-applied={stats['applied']}")
    print(f"seed: {seeded} players verified in the {target['kind']} cohort "
          f"({created} identities provisioned this run; nothing deleted)")
    return 0


def cmd_verify(args) -> int:
    target = resolve_target(args.local, args.hosted)
    secret = os.environ.get("ANKISCAPE_FIXTURE_SECRET", "")
    if not secret:
        raise SeedError("ANKISCAPE_FIXTURE_SECRET is required for --verify")
    transport = Transport(target["url"], target["anon"], target["service"])
    suite = traces_mod.load_suite()
    traces = suite_traces()
    failures: List[str] = []
    counts = {"players": 0, "skills": 0, "rank_checks": 0}
    for name in suite["display_names"]:
        trace = traces[name]
        token = _sign_in(transport, traces_mod.email_for(name),
                         password_for(secret, name))
        game_uuid = _linked_game_uuid(transport, trace, token)
        if not game_uuid:
            failures.append(f"{name}: link_game reply violates the "
                            "created/game_uuid contract")
            continue
        state = _server_state(transport, game_uuid, token)
        problems = _compare_state(trace, state)
        if problems:
            failures.extend(f"{name}: {p}" for p in problems[:3])
        counts["players"] += 1
    # Cohort isolation: public paths never show test names; test RPCs do.
    public_rows: Dict[str, List[Dict]] = {}
    test_rows: Dict[str, List[Dict]] = {}
    any_token = _sign_in(transport, traces_mod.email_for(
        suite["display_names"][0]),
        password_for(secret, suite["display_names"][0]))
    for skill in ("mining", "woodcutting", "smithing", "crafting",
                  "fishing", "cooking"):
        public_rows[skill] = transport.request(
            "POST", "/rest/v1/rpc/hiscores",
            body={"p_skill": skill, "p_limit": 100}, expect=(200,)) or []
        test_rows[skill] = transport.request(
            "POST", "/rest/v1/rpc/test_hiscores",
            body={"p_skill": skill, "p_limit": 100}, token=any_token,
            expect=(200,)) or []
        counts["skills"] += 1
        public_names = {str(r.get("username")) for r in public_rows[skill]}
        test_names = {str(r.get("username")) for r in test_rows[skill]}
        fixture_displays = {traces_mod.display_name(n) for n in suite["display_names"]}
        leaked = public_names & fixture_displays
        if leaked:
            failures.append(f"public {skill} leaks test names: {sorted(leaked)[:3]}")
        missing = fixture_displays - test_names
        if missing:
            failures.append(f"test {skill} missing players: {sorted(missing)[:3]}")
        expected_rows, expected_ranks = _expected_standings(traces, skill)
        actual = {str(r.get("username")): int(r.get("rank") or 0)
                  for r in test_rows[skill]}
        for norm, rank in expected_ranks.items():
            display = next(traces_mod.display_name(n) for n in suite["display_names"]
                           if traces_mod.username_norm(n) == norm)
            if int(actual.get(display, 0)) != int(rank):
                failures.append(
                    f"test {skill} rank {display}: {actual.get(display)} != {rank}")
            counts["rank_checks"] += 1
    # Retry stability: exact resubmission returns acceptance without growth.
    sample = suite["display_names"][0]
    trace = traces[sample]
    token = _sign_in(transport, traces_mod.email_for(sample),
                     password_for(secret, sample))
    game_uuid = _linked_game_uuid(transport, trace, token)
    if not game_uuid:
        failures.append("retry sample: link_game reply violates the "
                        "created/game_uuid contract")
        before = after = {}
    else:
        before = _server_state(transport, game_uuid, token)
        stats = _submit_trace(transport, trace, token,
                              int(suite["trace"]["batch_size"]))
        after = _server_state(transport, game_uuid, token)
        if int(stats.get("applied") or 0) != 0:
            failures.append(f"retry applied {stats.get('applied')} new operations")
        if before.get("revision") != after.get("revision"):
            failures.append("retry changed the server revision")
    report = {"target": target["kind"], "counts": counts,
              "failures": failures,
              "public_top": {s: public_rows[s][:3] for s in public_rows},
              "test_top": {s: test_rows[s][:3] for s in test_rows}}
    out = os.path.join(ROOT, "artifacts", "reliability",
                       "hosted-fixtures-verify.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    if failures:
        for failure in failures:
            print(f"verify: FAIL {failure}", file=sys.stderr)
        return 1
    print(f"verify: PASS ({counts['players']} players, {counts['skills']} skills, "
          f"{counts['rank_checks']} rank checks) -> {out}")
    return 0


def main(argv=None) -> int:
    print("seed_hosted_fixtures: RETIRED — the hosted-v1 24-account suite is "
          "no longer provisioned. Use dev/demo_players.py (five public demos) "
          "instead. This script refuses to recreate retired users.",
          file=sys.stderr)
    return 2


def _legacy_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="seed_hosted_fixtures.py")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--local", action="store_true",
                        help="local Supabase stack")
    target.add_argument("--hosted", action="store_true",
                        help="production project from environment")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.plan:
            transport = None
            if args.local or args.hosted:
                resolved = resolve_target(args.local, args.hosted)
                transport = Transport(resolved["url"], resolved["anon"],
                                      resolved.get("service", ""))
            return cmd_plan(transport)
        if args.apply:
            return cmd_apply(args)
        return cmd_verify(args)
    except SeedError as exc:
        print(f"seed: ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
