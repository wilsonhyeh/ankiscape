#!/usr/bin/env python3
"""dev/account_functions.py - Local Edge Function lifecycle for the E2E.

Not shipped. Serves account-status, username-login and account-delete from
server/ for the disposable local stack, with PER-FUNCTION JWT settings read
from server/supabase/config.toml (account-delete must require a JWT; the two
public endpoints must not). Never passes a global --no-verify-jwt.

Reuse is allowed only when all three functions answer with the expected
configuration; otherwise a new `supabase functions serve` process is spawned
and only that process is stopped on release. A missing gateway or a mismatched
config blocks the caller instead of passing silently.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
API = "http://127.0.0.1:55321"
FUNCTIONS = ("account-status", "username-login", "account-delete")
READY_TIMEOUT_S = 90.0


class FunctionsUnavailable(RuntimeError):
    pass


def _run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True,
                          cwd=SERVER, **kwargs)


def read_function_config() -> dict:
    """Parse [functions.<name>] verify_jwt values from config.toml."""
    path = os.path.join(SERVER, "supabase", "config.toml")
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise FunctionsUnavailable(f"config.toml unreadable: {exc}") from exc
    current = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[functions.") and stripped.endswith("]"):
            current = stripped[len("[functions."):-1]
        elif current and stripped.startswith("verify_jwt"):
            _, _, value = stripped.partition("=")
            out[current] = value.strip().lower() == "true"
    return out


def verify_function_config() -> None:
    """Fail closed before serving: deletion must require JWT verification."""
    config = read_function_config()
    if config.get("account-delete") is not True:
        raise FunctionsUnavailable(
            "config.toml must set [functions.account-delete] verify_jwt = true")
    for name in ("account-status", "username-login"):
        if config.get(name) is True:
            raise FunctionsUnavailable(
                f"config.toml unexpectedly requires JWT for {name}")


def _probe(name: str) -> dict:
    """POST with an empty body and classify the response."""
    req = urllib.request.Request(
        f"{API}/functions/v1/{name}", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "apikey": "probe"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"status": resp.status, "body": resp.read(500).decode()}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(500).decode()
        except Exception:
            body = ""
        return {"status": exc.code, "body": body}
    except Exception:
        return {"status": 0, "body": ""}


def _function_ready(name: str) -> bool:
    probe = _probe(name)
    status = int(probe.get("status", 0) or 0)
    body = str(probe.get("body", "") or "")
    if status == 0:
        return False
    if name == "account-delete":
        # Platform JWT verification rejects an anonymous POST with a 401
        # whose body is NOT the function's own invalid_session payload.
        if status not in (401, 403):
            return False
        try:
            parsed = json.loads(body or "{}")
        except ValueError:
            parsed = {}
        return parsed.get("error") != "invalid_session"
    # Public functions answer an empty body themselves; 404 means missing.
    return status != 404


def reuse_if_current() -> bool:
    try:
        return all(_function_ready(name) for name in FUNCTIONS)
    except Exception:
        return False


class FunctionsLease:
    def __init__(self, process, env_path: str, spawned: bool):
        self.process = process
        self.env_path = env_path
        self.spawned = spawned

    def release(self) -> None:
        if self.spawned and self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=20)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        try:
            os.remove(self.env_path)
        except OSError:
            pass


def ensure_serving() -> FunctionsLease:
    """Return a lease on a correctly configured function server."""
    verify_function_config()
    if reuse_if_current():
        return FunctionsLease(None, "", spawned=False)
    secret = os.urandom(32).hex()
    env_fd, env_path = tempfile.mkstemp(prefix="ankiscape-fn-", suffix=".env")
    try:
        with os.fdopen(env_fd, "w", encoding="utf-8") as fh:
            fh.write(f"ACCOUNT_STATUS_HMAC_SECRET={secret}\n")
        process = subprocess.Popen(
            ["supabase", "functions", "serve", "--env-file", env_path,
             *FUNCTIONS],
            cwd=SERVER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        os.remove(env_path)
        raise FunctionsUnavailable(f"could not start functions serve: {exc}")
    deadline = time.time() + READY_TIMEOUT_S
    while time.time() < deadline:
        if process.poll() is not None:
            process.terminate()
            raise FunctionsUnavailable(
                "supabase functions serve exited during startup")
        if all(_function_ready(name) for name in FUNCTIONS):
            return FunctionsLease(process, env_path, spawned=True)
        time.sleep(2)
    process.terminate()
    raise FunctionsUnavailable(
        "local account functions did not become ready in "
        f"{READY_TIMEOUT_S:.0f}s")


if __name__ == "__main__":
    verify_function_config()
    try:
        lease = ensure_serving()
    except FunctionsUnavailable as exc:
        print(f"account_functions: BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
    print("account_functions: ready (config verified)")
    if lease.spawned:
        print("account_functions: serving in this process; Ctrl-C to stop")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            pass
    lease.release()
