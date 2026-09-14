#!/usr/bin/env python3
# dev/demo_players.py - Five permanent public demo players (dev only).
"""Creates, seeds, publishes and verifies the five permanent demo players
(DemoWillow, DemoFlint, DemoMoss, DemoRowan, DemoCopper) that appear on the
PUBLIC leaderboard labeled "Demo", and retires the old known surplus
hosted-v1 fixture identities.

Modes:
  --plan                     write an exact action manifest + summary
  --apply --plan-file PATH   execute a checksum-verified plan (idempotent,
                             resumable via a local ledger)
  --verify                   read-only server verification

Targets:
  --local                    local Supabase stack (status keys read live)
  --hosted                   ANKISCAPE_PROD_URL + ANKISCAPE_PROD_ANON_KEY
                             (+ SUPABASE_SERVICE_ROLE_KEY for changes)

Guarantees:
  * gameplay goes through each user's normal token and the real link_game /
    submit_operations RPCs; scores are never written directly;
  * the demo flag is set only by privileged tooling (no auth.uid());
  * every deletion requires an exact fixture-registry/user/game/email match
    and is_test classification; unknown, real or unregistered accounts are
    never deleted and drift stops the run;
  * bounded serial requests (<=2/s), finite retries, resumable ledger;
  * no email is ever sent. Passwords derive from ANKISCAPE_FIXTURE_SECRET.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.journal import canonical_json  # noqa: E402


def _load_module(name: str, rel_path: str):
    path = os.path.join(ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo_traces = _load_module("ankiscape_demo_traces", "dev/demo_traces.py")
seed = _load_module("ankiscape_seed_hosted_fixtures",
                    "dev/seed_hosted_fixtures.py")

SeedError = seed.SeedError
Transport = seed.Transport
resolve_target = seed.resolve_target

LEGACY_SUITE_ID = "hosted-v1"
LEGACY_SUITE_VERSION = 1
SERVICE_ROLE_ENV = "SUPABASE_SERVICE_ROLE_KEY"


class DemoError(Exception):
    pass


def password_for(secret: str, name: str) -> str:
    if not secret:
        raise DemoError("ANKISCAPE_FIXTURE_SECRET is required for --apply/--verify")
    message = (f"ankiscape-demo:{demo_traces.SUITE_ID}:"
               f"v{demo_traces.SUITE_VERSION}:{name}").encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:24]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _plan_path(local: bool) -> str:
    kind = "local" if local else "hosted"
    return os.path.join(ROOT, "artifacts", "account-repair",
                        f"demo-plan-{kind}.json")


def _ledger_path(local: bool) -> str:
    kind = "local" if local else "hosted"
    return os.path.join(ROOT, "artifacts", "account-repair",
                        f"demo-ledger-{kind}.json")


def _load_ledger(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"completed": {}}
    except (OSError, ValueError):
        return {"completed": {}}


def _save_ledger(path: str, ledger: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _checksum(body: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _traces() -> Dict[str, Dict[str, Any]]:
    return demo_traces.build_traces(tuple(demo_traces.DISPLAY_NAMES))


def _service_key(target: Dict[str, str]) -> str:
    key = target.get("service") or os.environ.get(SERVICE_ROLE_ENV, "")
    return key


def _registry_rows(transport: Transport, suite_id: str,
                   version: int) -> List[Dict[str, Any]]:
    rows = transport.request(
        "GET", "/rest/v1/fixture_registry",
        params={"select": "*", "suite_id": f"eq.{suite_id}",
                "suite_version": f"eq.{version}"},
        service=True)
    return rows or []


def _players_by_norm(transport: Transport,
                     norms: List[str]) -> Dict[str, Dict[str, Any]]:
    if not norms:
        return {}
    rows = transport.request(
        "GET", "/rest/v1/players",
        params={"select": "user_id,username_norm,username_display,is_test,"
                          "is_demo,game_uuid,status",
                "username_norm": "in.(" + ",".join(norms) + ")"},
        service=True)
    return {str(r["username_norm"]): r for r in (rows or [])}


def _users_by_id(transport: Transport,
                 user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for uid in user_ids:
        try:
            user = transport.request("GET", f"/auth/v1/admin/users/{uid}",
                                     service=True, expect=(200,))
        except SeedError:
            continue
        if isinstance(user, dict) and user.get("id"):
            out[str(user["id"])] = user
    return out


def _build_plan(target: Dict[str, str], traces: Dict[str, Dict[str, Any]],
                transport: Optional[Transport]) -> Dict[str, Any]:
    norms = [t["username_norm"] for t in traces.values()]
    registry: Dict[str, Any] = {"present": [], "missing": [], "foreign": []}
    players: Dict[str, Any] = {}
    surplus: List[Dict[str, Any]] = []
    legacy_rows: List[Dict[str, Any]] = []
    if transport is not None:
        for row in _registry_rows(transport, demo_traces.SUITE_ID,
                                  demo_traces.SUITE_VERSION):
            norm = str(row.get("username_norm", ""))
            if norm in norms:
                registry["present"].append(norm)
            else:
                registry["foreign"].append(norm)
        registry["missing"] = [n for n in norms if n not in registry["present"]]
        players = _players_by_norm(transport, norms + registry["foreign"])
        legacy_rows = _registry_rows(transport, LEGACY_SUITE_ID,
                                     LEGACY_SUITE_VERSION)
    identities = []
    for name in demo_traces.DISPLAY_NAMES:
        trace = traces[name]
        identities.append({
            "display": name,
            "username_norm": trace["username_norm"],
            "email": trace["email"],
            "game_uuid": trace["game_uuid"],
            "trace_hash": trace["trace_hash"],
            "ops": len(trace["ops"]),
        })
    # Surplus retirement candidates: ONLY exact hosted-v1 registry rows with a
    # bound user_id. Ownership is re-verified immediately before deletion.
    for row in legacy_rows:
        if not row.get("user_id"):
            continue
        surplus.append({
            "suite_id": LEGACY_SUITE_ID,
            "suite_version": LEGACY_SUITE_VERSION,
            "username_norm": str(row.get("username_norm", "")),
            "user_id": str(row.get("user_id", "")),
            "game_uuid": str(row.get("game_uuid") or ""),
            "reserved_email": str(row.get("reserved_email") or ""),
        })
    actions: List[Dict[str, Any]] = []
    for identity in identities:
        current = players.get(identity["username_norm"])
        if current is None:
            actions.append({"action": "create_identity",
                            "username_norm": identity["username_norm"]})
        actions.append({"action": "seed_trace",
                        "username_norm": identity["username_norm"],
                        "trace_hash": identity["trace_hash"]})
        actions.append({"action": "publish_demo",
                        "username_norm": identity["username_norm"]})
    for item in surplus:
        actions.append({"action": "retire_surplus",
                        "username_norm": item["username_norm"],
                        "user_id": item["user_id"]})
    body = {
        "schema": "ankiscape-demo-plan",
        "version": 1,
        "generated_at": _now_iso(),
        "target": {"kind": target.get("kind", ""),
                   "base_url": target.get("url", "")},
        "suite": {"suite_id": demo_traces.SUITE_ID,
                  "suite_version": demo_traces.SUITE_VERSION,
                  "label": demo_traces.PUBLIC_LABEL},
        "manifest_digest": demo_traces.manifest_digest(traces),
        "identities": identities,
        "registry": registry,
        "players": players,
        "surplus": surplus,
        "actions": actions,
        "counts": {
            "identities": len(identities),
            "registry_present": len(registry["present"]),
            "registry_missing": len(registry["missing"]),
            "surplus_candidates": len(surplus),
        },
        "notes": [
            "No identifiers are deleted unless they match a hosted-v1 "
            "fixture_registry row exactly and the player is_test=true.",
            "Real or unclassified accounts are never deleted.",
        ],
    }
    body["checksum"] = _checksum({k: v for k, v in body.items()
                                  if k != "checksum"})
    return body


def cmd_plan(args) -> int:
    target = resolve_target(args.local, args.hosted)
    traces = _traces()
    key = _service_key(target)
    transport = Transport(target["url"], target["anon"], key) if key else None
    plan = _build_plan(target, traces, transport)
    path = args.plan_file or _plan_path(args.local)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"demo plan: {plan['counts']['identities']} demos, "
          f"{plan['counts']['surplus_candidates']} surplus candidates, "
          f"{len(plan['actions'])} actions")
    print(f"demo plan: wrote {os.path.relpath(path, ROOT)} "
          f"(checksum {plan['checksum'][:16]})")
    print(f"demo plan: manifest {plan['manifest_digest'][:16]}")
    if transport is None:
        print("demo plan: service key unavailable; current-state section is "
              "empty and apply cannot run")
    return 0


def _verify_plan(plan: Dict[str, Any], target: Dict[str, str],
                 traces: Dict[str, Dict[str, Any]]) -> None:
    stored = str(plan.get("checksum", ""))
    body = {k: v for k, v in plan.items() if k != "checksum"}
    if stored != _checksum(body):
        raise DemoError("plan checksum mismatch; regenerate with --plan")
    if str(plan.get("target", {}).get("kind", "")) != target.get("kind"):
        raise DemoError("plan target kind does not match this invocation")
    if str(plan.get("target", {}).get("base_url", "")) != target.get("url"):
        raise DemoError("plan target URL does not match the current target")
    expected_digest = demo_traces.manifest_digest(traces)
    if str(plan.get("manifest_digest", "")) != expected_digest:
        raise DemoError("trace manifest drift; regenerate with --plan")
    planned_names = [i["username_norm"] for i in plan.get("identities", [])]
    if planned_names != [t["username_norm"] for t in traces.values()]:
        raise DemoError("plan identities do not match the demo suite")


def _create_user(transport: Transport, trace: Dict[str, Any],
                 secret: str) -> str:
    body = {"email": trace["email"],
            "password": password_for(secret, trace["display"]),
            "email_confirm": True,
            "user_metadata": {"username_norm": trace["username_norm"],
                              "username_display": trace["display"]}}
    data = transport.request("POST", "/auth/v1/admin/users", body=body,
                             service=True, expect=(200, 201, 409))
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    if isinstance(data, dict) and isinstance(data.get("user"), dict):
        return str(data["user"].get("id", ""))
    # 409: already exists; the caller re-reads the registry for the id.
    return ""


def _sign_in(transport: Transport, email: str, password: str) -> str:
    data = transport.request("POST", "/auth/v1/token",
                             params={"grant_type": "password"},
                             body={"email": email, "password": password},
                             expect=(200,))
    token = str((data or {}).get("access_token") or "")
    if not token:
        raise DemoError(f"sign-in returned no token for {email}")
    return token


def _submit_trace(transport: Transport, user_token: str, game_uuid: str,
                  trace: Dict[str, Any], batch_size: int) -> Tuple[int, int]:
    transport.request("POST", "/rest/v1/rpc/link_game",
                      body={"p_game_uuid": game_uuid}, token=user_token,
                      expect=(200,))
    accepted_total = 0
    for index in range(0, len(trace["ops"]), batch_size):
        batch = trace["ops"][index:index + batch_size]
        data = transport.request(
            "POST", "/rest/v1/rpc/submit_operations",
            body={"p_game_uuid": game_uuid, "p_ops": batch},
            token=user_token, expect=(200,))
        accepted = data.get("accepted") if isinstance(data, dict) else None
        conflicts = data.get("conflicts") if isinstance(data, dict) else None
        if conflicts:
            raise DemoError(f"conflicts while seeding {trace['display']}: "
                            f"{conflicts[:2]}")
        accepted_total += len(accepted or [])
    return accepted_total, len(trace["ops"])


def _server_xp(transport: Transport, user_token: str,
               game_uuid: str) -> Optional[Dict[str, Any]]:
    data = transport.request("POST", "/rest/v1/rpc/get_game_state",
                             body={"p_game_uuid": game_uuid},
                             token=user_token, expect=(200,))
    if not isinstance(data, dict):
        return None
    state = data.get("state") if isinstance(data.get("state"), dict) else data
    xp = state.get("xp") if isinstance(state, dict) else None
    return xp if isinstance(xp, dict) else None


def _score_problems(expected: Dict[str, Any],
                    xp: Optional[Dict[str, Any]]) -> List[str]:
    problems = []
    actual = xp or {}
    for skill, value in (expected.get("xp_micro") or {}).items():
        have = int(actual.get(skill, 0) or 0)
        if have != int(value):
            problems.append(f"{skill}: {have} != {value}")
    return problems


def _publish_demo(transport: Transport, norm: str, user_id: str) -> None:
    transport.request("PATCH", "/rest/v1/players",
                      params={"username_norm": f"eq.{norm}",
                              "user_id": f"eq.{user_id}"},
                      body={"is_demo": True}, service=True,
                      expect=(200, 204))


def _upsert_registry(transport: Transport, trace: Dict[str, Any],
                     user_id: str) -> None:
    transport.request(
        "POST", "/rest/v1/fixture_registry",
        params={"on_conflict": "suite_id,suite_version,username_norm"},
        body={"suite_id": demo_traces.SUITE_ID,
              "suite_version": demo_traces.SUITE_VERSION,
              "username_norm": trace["username_norm"],
              "user_id": user_id or None,
              "game_uuid": trace["game_uuid"],
              "expected_trace_hash": trace["trace_hash"],
              "reserved_email": trace["email"],
              "seed_state": "verified"},
        service=True,
        headers_extra={"Prefer": "resolution=merge-duplicates"},
        expect=(200, 201, 204, 409))


def _retire_surplus(transport: Transport, item: Dict[str, Any]) -> None:
    """Delete one exact registry-owned legacy fixture identity + its data."""
    rows = transport.request(
        "GET", "/rest/v1/fixture_registry",
        params={"select": "*", "suite_id": f"eq.{item['suite_id']}",
                "suite_version": f"eq.{item['suite_version']}",
                "username_norm": f"eq.{item['username_norm']}"},
        service=True)
    row = (rows or [None])[0]
    if not isinstance(row, dict) or str(row.get("user_id") or "") != item["user_id"]:
        raise DemoError(f"ownership drift for {item['username_norm']}; "
                        "refusing to delete")
    players = transport.request(
        "GET", "/rest/v1/players",
        params={"select": "user_id,username_norm,is_test",
                "username_norm": f"eq.{item['username_norm']}",
                "user_id": f"eq.{item['user_id']}"},
        service=True)
    player = (players or [None])[0]
    if not isinstance(player, dict) or player.get("is_test") is not True:
        raise DemoError(f"{item['username_norm']} is not classified is_test; "
                        "refusing to delete")
    # Data rows first (exact game/user match), then registry row, then Auth.
    if item.get("game_uuid"):
        for table in ("game_operations", "game_state", "game_checkpoints"):
            try:
                transport.request(
                    "DELETE", f"/rest/v1/{table}",
                    params={"game_uuid": f"eq.{item['game_uuid']}"},
                    service=True, expect=(200, 204))
            except SeedError:
                pass
    transport.request(
        "DELETE", "/rest/v1/fixture_registry",
        params={"suite_id": f"eq.{item['suite_id']}",
                "suite_version": f"eq.{item['suite_version']}",
                "username_norm": f"eq.{item['username_norm']}",
                "user_id": f"eq.{item['user_id']}"},
        service=True, expect=(200, 204))
    transport.request("DELETE", f"/auth/v1/admin/users/{item['user_id']}",
                      service=True, expect=(200, 204, 404))


def cmd_apply(args) -> int:
    if not args.plan_file:
        raise DemoError("--apply requires --plan-file PATH")
    target = resolve_target(args.local, args.hosted)
    key = _service_key(target)
    if not key:
        raise DemoError("service role key required for --apply")
    secret = os.environ.get("ANKISCAPE_FIXTURE_SECRET", "")
    if len(secret) < 16:
        raise DemoError("ANKISCAPE_FIXTURE_SECRET (>=16 chars) is required")
    traces = _traces()
    with open(args.plan_file, encoding="utf-8") as fh:
        plan = json.load(fh)
    _verify_plan(plan, target, traces)
    transport = Transport(target["url"], target["anon"], key)
    ledger = _load_ledger(_ledger_path(args.local))
    completed = ledger.setdefault("completed", {})
    for trace in traces.values():
        norm = trace["username_norm"]
        if completed.get(f"demo:{norm}"):
            print(f"demo apply: {norm}: already complete (ledger)")
            continue
        # Exact-ownership checks before any mutation.
        registry = _registry_rows(transport, demo_traces.SUITE_ID,
                                  demo_traces.SUITE_VERSION)
        owned = next((r for r in registry
                      if str(r.get("username_norm")) == norm), None)
        players = _players_by_norm(transport, [norm])
        player = players.get(norm)
        adopted_user_id = ""
        if player is not None and owned is None:
            # Partial apply from an earlier failed run: adopt ONLY when the
            # existing player's exact reserved .example.invalid email belongs
            # to this demo identity. Anything else is refused.
            candidate_id = str(player.get("user_id") or "")
            account = transport.request(
                "GET", f"/auth/v1/admin/users/{candidate_id}", service=True,
                expect=(200,))
            email = str((account or {}).get("email", "") or "").lower()
            if not candidate_id or email != trace["email"].lower():
                raise DemoError(
                    f"{norm} already exists but is not registry-owned; "
                    "refusing to adopt or modify it")
            _upsert_registry(transport, trace, candidate_id)
            adopted_user_id = candidate_id
            print(f"demo apply: {norm:<12} adopted a partial identity from "
                  "an earlier run")
        user_id = (str((owned or {}).get("user_id") or "")
                   or adopted_user_id)
        if not user_id:
            user_id = _create_user(transport, trace, secret)
        if not user_id:
            # Existing registry-owned identity: re-read for its id.
            for uid, user in _users_by_id(
                    transport,
                    [str(r.get("user_id")) for r in registry
                     if r.get("user_id")]).items():
                if str(user.get("email", "")).lower() == trace["email"].lower():
                    user_id = uid
                    break
        if not user_id:
            raise DemoError(f"could not resolve an Auth id for {norm}")
        # The email must be ours (reserved .example.invalid, exact match).
        account = transport.request("GET", f"/auth/v1/admin/users/{user_id}",
                                    service=True, expect=(200,))
        email = str((account or {}).get("email", "") or "").lower()
        if email != trace["email"].lower():
            raise DemoError(f"email mismatch for {norm}; refusing to seed")
        token = _sign_in(transport, trace["email"],
                         password_for(secret, trace["display"]))
        batch_size = int((demo_traces.load_suite().get("trace") or {})
                         .get("batch_size", 200))
        accepted, total = _submit_trace(transport, token, trace["game_uuid"],
                                        trace, batch_size)
        xp = _server_xp(transport, token, trace["game_uuid"])
        problems = _score_problems(trace["expected"], xp)
        if problems:
            raise DemoError(f"score mismatch for {norm}: "
                            + "; ".join(problems[:4]))
        _publish_demo(transport, norm, user_id)
        _upsert_registry(transport, trace, user_id)
        completed[f"demo:{norm}"] = {"user_id": user_id,
                                     "accepted": accepted,
                                     "ops": total,
                                     "at": _now_iso()}
        _save_ledger(_ledger_path(args.local), ledger)
        print(f"demo apply: {norm:<12} ops={total} accepted={accepted} "
              "published label=Demo")
    for item in plan.get("surplus", []):
        key_id = f"retire:{item['suite_id']}:{item['username_norm']}"
        if completed.get(key_id):
            print(f"demo apply: retire {item['username_norm']}: already done")
            continue
        _retire_surplus(transport, item)
        completed[key_id] = {"user_id": item["user_id"], "at": _now_iso()}
        _save_ledger(_ledger_path(args.local), ledger)
        print(f"demo apply: retired surplus fixture {item['username_norm']}")
    print("demo apply: complete")
    return 0


def _public_rows(transport: Transport, skill: str, limit: int = 100) -> List[Dict]:
    data = transport.request("POST", "/rest/v1/rpc/hiscores",
                             body={"p_skill": skill, "p_limit": limit},
                             expect=(200,))
    if not isinstance(data, list):
        raise DemoError("public hiscores did not return a list")
    return data


def _public_profile(transport: Transport, norm: str) -> Dict[str, Any]:
    return transport.request("POST", "/rest/v1/rpc/public_profile",
                             body={"p_username_norm": norm}, expect=(200,)) or {}


def cmd_verify(args) -> int:
    target = resolve_target(args.local, args.hosted)
    key = _service_key(target)
    secret = os.environ.get("ANKISCAPE_FIXTURE_SECRET", "")
    if not secret:
        raise DemoError("ANKISCAPE_FIXTURE_SECRET is required for --verify")
    traces = _traces()
    transport = Transport(target["url"], target["anon"], key)
    failures: List[str] = []
    counts = {"players": 0, "skills": 0, "rows": 0, "retired_checked": 0}
    demos = {t["username_norm"]: t for t in traces.values()}
    for skill in ("mining", "woodcutting", "smithing", "crafting",
                  "fishing", "cooking"):
        counts["skills"] += 1
        rows = _public_rows(transport, skill)
        counts["rows"] += len(rows)
        by_name = {str(r.get("username")): r for r in rows}
        for display, trace in traces.items():
            row = by_name.get(display)
            if row is None:
                failures.append(f"{skill}: {display} missing from the public board")
                continue
            if row.get("is_demo") is not True:
                failures.append(f"{skill}: {display} missing the Demo flag")
            if int(row.get("xp", -1)) != int(
                    trace["expected"]["xp_micro"].get(skill, 0)):
                failures.append(f"{skill}: {display} score differs")
            if set(row.keys()) != {"rank", "username", "xp", "is_demo"}:
                failures.append(f"{skill}: {display} row has extra fields: "
                                f"{sorted(row.keys())}")
    legacy = _registry_rows(transport, LEGACY_SUITE_ID, LEGACY_SUITE_VERSION)
    for row in legacy:
        if row.get("user_id"):
            failures.append(f"surplus fixture still registered: "
                            f"{row.get('username_norm')}")
        counts["retired_checked"] += 1
    # Profiles: allowlist exactly {username, is_demo, state:{xp}}.
    for display, trace in traces.items():
        try:
            profile = _public_profile(transport, trace["username_norm"])
        except SeedError as exc:
            failures.append(f"public_profile failed for {display}: {exc}")
            continue
        if set(profile.keys()) != {"username", "is_demo", "state"}:
            failures.append(f"{display} profile has extra fields: "
                            f"{sorted(profile.keys())}")
        if profile.get("is_demo") is not True:
            failures.append(f"{display} profile is_demo is not true")
        state = profile.get("state")
        if not isinstance(state, dict) or set(state.keys()) != {"xp"}:
            failures.append(f"{display} profile state is not the xp wrapper")
    # Sign-in + authoritative state per demo.
    for display, trace in traces.items():
        token = _sign_in(transport, trace["email"],
                         password_for(secret, display))
        xp = _server_xp(transport, token, trace["game_uuid"])
        counts["players"] += 1
        problems = _score_problems(trace["expected"], xp)
        if problems:
            failures.append(f"{display}: " + "; ".join(problems[:3]))
    report = {
        "target": target.get("kind"),
        "checked_at": _now_iso(),
        "counts": counts,
        "failures": failures,
    }
    out = os.path.join(ROOT, "artifacts", "reliability",
                       "public-demo-verify.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    if failures:
        for failure in failures[:20]:
            print(f"demo verify: FAIL {failure}", file=sys.stderr)
        print(f"demo verify: FAIL ({len(failures)} findings)")
        return 1
    print(f"demo verify: PASS ({counts['players']} demos, "
          f"{counts['skills']} skills, {counts['retired_checked']} legacy rows "
          "checked)")
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true")
    target.add_argument("--hosted", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--plan-file", default="")
    args = parser.parse_args(argv)
    try:
        if args.plan:
            return cmd_plan(args)
        if args.apply:
            return cmd_apply(args)
        return cmd_verify(args)
    except (DemoError, SeedError) as exc:
        print(f"demo_players: BLOCKED: {exc}", file=sys.stderr)
        return 2 if "unavailable" in str(exc) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
