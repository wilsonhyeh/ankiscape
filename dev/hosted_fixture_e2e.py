#!/usr/bin/env python3
# dev/hosted_fixture_e2e.py - Hosted fixture end-to-end check (dev only).
"""Extends dev/seed_hosted_fixtures.py --verify with profile lookup, cohort
denial checks and a recorded ranking snapshot:

  * all six leaderboards (public excluded / test included with exact ranks),
  * test_public_profile safe fields for every fixture player,
  * public lookup denial indistinguishable for test and unknown names,
  * exact retry stability (no new operations, no revision change),
  * expected/actual rankings and retained fixture counts recorded under
    artifacts/reliability/.

Usage:
  python3 dev/hosted_fixture_e2e.py --local
  python3 dev/hosted_fixture_e2e.py --hosted

The native Test leaderboard UI journey (ui-test-leaderboard) runs on the
platform lanes; this script proves the real transport/server paths.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SKILLS = ("mining", "woodcutting", "smithing", "crafting", "fishing", "cooking")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load("ankiscape_seed_hosted", os.path.join(ROOT, "dev",
                                                  "seed_hosted_fixtures.py"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hosted_fixture_e2e.py")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true")
    target.add_argument("--hosted", action="store_true")
    args = parser.parse_args(argv)
    secret = os.environ.get("ANKISCAPE_FIXTURE_SECRET", "")
    if not secret:
        print("hosted_fixture_e2e: ERROR: ANKISCAPE_FIXTURE_SECRET is required",
              file=sys.stderr)
        return 1
    resolved = seed.resolve_target(args.local, args.hosted)
    transport = seed.Transport(resolved["url"], resolved["anon"],
                               resolved.get("service", ""))
    suite = seed.traces_mod.load_suite()
    traces = seed.suite_traces()
    failures: List[str] = []
    rankings: Dict[str, Any] = {}

    # 1. Profile lookup: safe allowlist fields match expected XP/levels.
    for name in suite["display_names"]:
        trace = traces[name]
        token = seed._sign_in(transport, seed.traces_mod.email_for(name),
                              seed.password_for(secret, name))
        profile = transport.request(
            "POST", "/rest/v1/rpc/test_public_profile",
            body={"p_username_norm": seed.traces_mod.username_norm(name)},
            token=token, expect=(200,))
        if not profile or profile.get("is_test") is not True:
            failures.append(f"{name}: test profile missing")
            continue
        skills = profile.get("skills") or {}
        for skill in SKILLS:
            expected_xp = int(trace["expected"]["xp_micro"].get(skill, 0))
            got_xp = int((skills.get(skill) or {}).get("xp", -1))
            expected_level = int(trace["expected"]["levels"].get(skill, 1))
            got_level = int((skills.get(skill) or {}).get("level", -1))
            if got_xp != expected_xp or got_level != expected_level:
                failures.append(
                    f"{name}:{skill} profile xp/level {got_xp}/{got_level} "
                    f"!= {expected_xp}/{expected_level}")
        if "state" in profile:
            failures.append(f"{name}: test profile exposed full private state")

    # 2. Public lookup denial is indistinguishable (same error code).
    token = seed._sign_in(transport,
                          seed.traces_mod.email_for(suite["display_names"][0]),
                          seed.password_for(secret, suite["display_names"][0]))
    for label, norm in (("test-name", seed.traces_mod.username_norm(
            suite["display_names"][0])), ("unknown", "definitely_not_a_player")):
        try:
            transport.request("POST", "/rest/v1/rpc/public_profile",
                              body={"p_username_norm": norm}, expect=(200,))
            failures.append(f"public lookup unexpectedly served {label}")
        except seed.SeedError as exc:
            if "no_profile" not in str(exc):
                failures.append(f"public lookup {label} raised {exc}")

    # 3. Leaderboards + ranks recorded for every skill.
    failures.extend(_leaderboard_failures(transport, suite, traces, rankings,
                                          token))

    # 4. Retry stability on a fresh signed-in token.
    sample = suite["display_names"][0]
    trace = traces[sample]
    token = seed._sign_in(transport, seed.traces_mod.email_for(sample),
                          seed.password_for(secret, sample))
    before = seed._server_state(transport, trace["game_uuid"], token)
    stats = seed._submit_trace(transport, trace, token,
                               int(suite["trace"]["batch_size"]))
    after = seed._server_state(transport, trace["game_uuid"], token)
    if int(stats.get("applied") or 0) != 0:
        failures.append(f"retry applied {stats.get('applied')} operations")
    if before.get("revision") != after.get("revision"):
        failures.append("retry changed the revision")

    counts = {"players": len(suite["display_names"]),
              "ops": sum(len(t["ops"]) for t in traces.values()),
              "skills": len(SKILLS)}
    report = {"target": resolved["kind"], "counts": counts,
              "rankings": rankings, "failures": failures,
              "checked_at": int(time.time())}
    out = os.path.join(ROOT, "artifacts", "reliability",
                       "hosted-fixture-e2e.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    if failures:
        for failure in failures:
            print(f"hosted_fixture_e2e: FAIL {failure}", file=sys.stderr)
        return 1
    print(f"hosted_fixture_e2e: PASS ({counts['players']} players, "
          f"{counts['skills']} leaderboards, retry stable) -> {out}")
    return 0


def _leaderboard_failures(transport, suite, traces, rankings: Dict[str, Any],
                          token: str) -> List[str]:
    failures: List[str] = []
    display_by_norm = {seed.traces_mod.username_norm(n): seed.traces_mod.display_name(n)
                       for n in suite["display_names"]}
    for skill in SKILLS:
        rows = transport.request("POST", "/rest/v1/rpc/test_hiscores",
                                 body={"p_skill": skill, "p_limit": 100},
                                 token=token, expect=(200,)) or []
        public = transport.request("POST", "/rest/v1/rpc/hiscores",
                                   body={"p_skill": skill, "p_limit": 100},
                                   expect=(200,)) or []
        expected_rows, expected_ranks = seed._expected_standings(traces, skill)
        actual = {str(r.get("username")): int(r.get("rank") or 0)
                  for r in rows}
        actual_xp = {str(r.get("username")): int(r.get("xp") or 0)
                     for r in rows}
        leaked = set(actual) & set(display_by_norm.values())
        public_names = {str(r.get("username")) for r in public}
        leaked_public = public_names & set(display_by_norm.values())
        if leaked_public:
            failures.append(f"{skill}: public board leaked {sorted(leaked_public)[:3]}")
        if missing := set(display_by_norm.values()) - set(actual):
            failures.append(f"{skill}: test board missing {sorted(missing)[:3]}")
        for norm, rank in expected_ranks.items():
            display = display_by_norm[norm]
            if actual.get(display) != rank:
                failures.append(f"{skill}: {display} rank {actual.get(display)} != {rank}")
        rankings[skill] = {
            "expected": [{"username": display_by_norm[n], "xp": xp,
                          "rank": expected_ranks[n]} for n, xp in expected_rows],
            "actual": rows}
        _ = actual_xp, leaked
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
