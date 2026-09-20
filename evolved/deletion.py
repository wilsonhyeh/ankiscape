# evolved/deletion.py - Profile/game-bound account-deletion coordinator (Qt-free).
"""Owns the destructive lifecycle OUTSIDE any account window:

  pending -> (server result) -> deleted -> cleanup -> done
                             -> refusal -> resumed (account kept)
                             -> unknown -> explicit reconciliation only

The coordinator captures the endpoint project, original user, canonical
profile path, game UUID and the local-progress choice before dispatch. A
credential-free marker (`ankiscape-evolved/account-deletion.json`) is written
before the request, so a restart can resume as unknown instead of silently
recreating the deleted game. Passwords and tokens never touch the marker.

Unknown is never treated as "account kept": local progress is retained, online
work for that identity is suspended, and only an explicit authoritative
user-not-found result (or a user-confirmed retry that succeeds) advances the
cleanup. Server deletion is never repeated automatically.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

MANAGED_DIR = "ankiscape-evolved"
MARKER_NAME = "account-deletion.json"
MARKER_VERSION = 1
PHASES = ("pending", "unknown", "server_deleted", "cleanup_pending")

# Known non-destructive refusals: the server answered and the account was not
# deleted. These resume ordinary operation and keep the session/progress.
KNOWN_REFUSAL_STATUSES = frozenset({
    "invalid_session", "invalid_credentials", "invalid_confirmation",
    "account_unavailable", "demo_immutable", "rate_limited", "service_error",
})
# Ambiguous outcomes: the request may or may not have reached the server.
UNKNOWN_STATUSES = frozenset({"delete_unknown", "offline"})


def managed_root(profile_dir: str) -> str:
    return os.path.join(str(profile_dir), MANAGED_DIR)


def marker_path(profile_dir: str) -> str:
    return os.path.join(managed_root(profile_dir), MARKER_NAME)


def read_marker(profile_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(marker_path(profile_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("user_id"):
        return None
    return data


def write_marker(profile_dir: str, data: Dict[str, Any]) -> bool:
    """Atomic credential-free marker write (temp file + os.replace)."""
    root = managed_root(profile_dir)
    try:
        os.makedirs(root, exist_ok=True)
        path = marker_path(profile_dir)
        temp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        payload = dict(data)
        payload["version"] = MARKER_VERSION
        payload["updated_at"] = int(time.time())
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
        os.replace(temp, path)
        return True
    except OSError:
        return False


def clear_marker(profile_dir: str) -> bool:
    try:
        os.remove(marker_path(profile_dir))
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def marker_matches(marker: Optional[Dict[str, Any]], *, user_id: str = "",
                   game_uuid: str = "") -> bool:
    """True when a marker belongs to the queried identity/game (or the query
    omitted that field). Markers for other identities are preserved."""
    if not isinstance(marker, dict):
        return False
    if user_id and str(marker.get("user_id", "")) != str(user_id):
        return False
    if game_uuid and str(marker.get("game_uuid", "")) != str(game_uuid):
        return False
    return True


def canonical_game_dir(profile_dir: str, game_uuid: str) -> Optional[str]:
    """Validated absolute path of one game's journal directory.

    Rejects malformed UUIDs, symlinked game directories and any path that
    resolves outside `<profile>/ankiscape-evolved/`. The caller removes only
    the returned path.
    """
    try:
        normalized = str(uuid.UUID(str(game_uuid)))
    except (ValueError, TypeError, AttributeError):
        return None
    root = os.path.realpath(managed_root(profile_dir))
    candidate = os.path.join(root, normalized)
    if os.path.islink(candidate):
        return None
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return None
    if os.path.dirname(real) != root:
        return None
    return real


def remove_game_dir(game_dir: str) -> bool:
    """Remove one validated game directory (SQLite file + WAL/SHM inside)."""
    try:
        if not os.path.exists(game_dir):
            return True
        if os.path.islink(game_dir):
            return False
        shutil.rmtree(game_dir)
        return True
    except OSError:
        return False


def marker_for(user_id: str, game_uuid: str, *, endpoint_project: str,
               delete_local: bool, phase: str) -> Dict[str, Any]:
    """Credential-free marker payload. Never password/email/tokens."""
    return {"user_id": str(user_id), "game_uuid": str(game_uuid),
            "endpoint_project": str(endpoint_project),
            "delete_local": bool(delete_local),
            "phase": str(phase)}


def resume_pending_marker(profile_dir: str, marker: Dict[str, Any]) -> bool:
    """A pending marker without a conclusive reply resumes as unknown."""
    if not isinstance(marker, dict):
        return False
    updated = dict(marker)
    updated["phase"] = "unknown"
    return write_marker(profile_dir, updated)


@dataclass
class DeletionDeps:
    """Everything the coordinator needs; injectable so tests stay headless."""

    post: Callable[..., Any]
    endpoint: Any
    accounts: Any
    profile_dir: str
    user_id: str
    username: str
    game_uuid: str
    delete_local: bool
    access_token: str
    runner: Callable[[Callable[[], Any], Callable[[Any], None]], None]
    on_event: Callable[[str, Dict[str, Any]], None] = (
        lambda event, payload: None)
    # Reconciliation: True (exists), False (authoritatively absent), None.
    check_account_exists: Callable[[], Optional[bool]] = lambda: None
    # Cleanup steps. Each returns True on success.
    clear_identity: Callable[[], bool] = lambda: True
    detach_binding: Callable[[], bool] = lambda: True
    stop_local_work: Callable[[], None] = lambda: None
    await_workers: Callable[[], bool] = lambda: True
    close_journal: Callable[[], None] = lambda: None
    # Collection-config pointer cleanup. "done" | "deferred" | "failed".
    clear_pointer: Callable[[], str] = lambda: "done"
    fence_epoch: Callable[[], None] = lambda: None
    suspend_online: Callable[[], None] = lambda: None
    resume_online: Callable[[], None] = lambda: None


class DeletionCoordinator:
    """One destructive operation per profile/game identity at a time."""

    def __init__(self, deps: DeletionDeps):
        self.deps = deps
        self.phase = ""
        self.dispatched = False
        self.captured: Dict[str, Any] = {}

    # ---------------------------------------------------------------- events
    def _emit(self, event: str, **payload) -> None:
        try:
            self.deps.on_event(event, payload)
        except Exception:
            pass

    def _write_phase(self, phase: str) -> None:
        self.phase = phase
        write_marker(self.deps.profile_dir, marker_for(
            self.deps.user_id, self.deps.game_uuid,
            endpoint_project=str(getattr(self.deps.endpoint, "base_url", "")),
            delete_local=self.deps.delete_local, phase=phase))

    # ------------------------------------------------------------- dispatch
    def begin(self, password: str, *, username: str = "") -> bool:
        """Validate, persist the pending marker, fence, then dispatch once."""
        if self.phase in ("pending", "server_deleted", "cleanup_pending") \
                or self.dispatched:
            return False
        if username and username != self.deps.username:
            self._emit("refused", status="invalid_confirmation",
                       message="The username didn't match. Nothing was "
                               "deleted.")
            return False
        if not str(password or ""):
            self._emit("refused", status="invalid_credentials",
                       message="Enter your password. Nothing was deleted.")
            return False
        if not self.deps.user_id or not self.deps.game_uuid:
            self._emit("refused", status="account_unavailable",
                       message="This profile has no account to delete.")
            return False
        self.dispatched = True
        self.captured = {
            "user_id": self.deps.user_id,
            "game_uuid": self.deps.game_uuid,
            "endpoint_project": str(getattr(self.deps.endpoint, "base_url",
                                            "")),
            "delete_local": bool(self.deps.delete_local),
        }
        self._write_phase("pending")
        self.deps.fence_epoch()
        self.deps.suspend_online()
        self._emit("pending")
        deps = self.deps

        def work():
            return deps.accounts.delete_account(
                deps.post, deps.endpoint, access_token=deps.access_token,
                username=deps.username, password=password)

        def apply(result):
            self._apply(result)

        try:
            deps.runner(work, apply)
        except Exception:
            # The request never started: treat as a known refusal.
            self._known_failure("service_error",
                                "Couldn't start the deletion request.")
        return True

    def _apply(self, result) -> None:
        status = str(getattr(result, "status", "") or "")
        if status == "deleted" and getattr(result, "ok", False):
            self._server_deleted()
            return
        if status in UNKNOWN_STATUSES:
            self._mark_unknown(getattr(result, "error", "") or "")
            return
        if status in KNOWN_REFUSAL_STATUSES:
            self._known_failure(status, getattr(result, "error", "") or "")
            return
        # Anything unrecognized is ambiguous, never a confident refusal.
        self._mark_unknown(getattr(result, "error", "") or "")

    def _known_failure(self, status: str, message: str) -> None:
        self.dispatched = False
        self.phase = ""
        clear_marker(self.deps.profile_dir)
        self.deps.resume_online()
        self._emit("refused", status=status, message=message)

    def _mark_unknown(self, message: str) -> None:
        self.dispatched = True
        self._write_phase("unknown")
        self.deps.suspend_online()
        self._emit("unknown", message=message
                   or "Could not confirm deletion.")

    # ------------------------------------------------------------- success
    def _server_deleted(self) -> None:
        self._write_phase("server_deleted")
        self._emit("server_deleted")
        self._run_cleanup()

    def _run_cleanup(self) -> bool:
        problems = []
        # 1. Live session + credential vault, independent of disk cleanup.
        try:
            if not self.deps.clear_identity():
                problems.append("credentials")
        except Exception:
            problems.append("credentials")
        # 2. Always detach the binding, link state and account caches.
        try:
            if not self.deps.detach_binding():
                problems.append("binding")
        except Exception:
            problems.append("binding")
        # 3. Optional local-progress deletion for exactly the captured game.
        pointer_state = "done"
        if self.deps.delete_local:
            if not self._delete_local_progress():
                problems.append("local")
            try:
                pointer_state = str(self.deps.clear_pointer() or "failed")
            except Exception:
                pointer_state = "failed"
            if pointer_state == "failed":
                problems.append("pointer")
        if problems:
            self._write_phase("cleanup_pending")
            self._emit("cleanup_incomplete", problems=tuple(problems),
                       pointer=pointer_state)
            return False
        if pointer_state == "deferred":
            # Collection-config cleanup runs when that exact profile opens;
            # the marker keeps the erased game from being recreated until then.
            self._write_phase("cleanup_pending")
            self._emit("cleanup_incomplete", problems=(), pointer="deferred")
            return False
        clear_marker(self.deps.profile_dir)
        self.phase = "done"
        self.dispatched = False
        self.deps.resume_online()
        self._emit("deleted")
        return True

    def _delete_local_progress(self) -> bool:
        game_dir = canonical_game_dir(self.deps.profile_dir,
                                      self.deps.game_uuid)
        if game_dir is None:
            self._emit("refused", status="service_error",
                       message="Couldn't locate this game's local files; "
                               "nothing local was removed.")
            return False
        try:
            self.deps.stop_local_work()
        except Exception:
            pass
        try:
            if not self.deps.await_workers():
                self._emit("refused", status="service_error",
                           message="Local work was still running; try the "
                                   "local cleanup again.")
                return False
        except Exception:
            return False
        try:
            self.deps.close_journal()
        except Exception:
            pass
        return remove_game_dir(game_dir)

    def retry_local_cleanup(self) -> bool:
        """Local-only retry after a partial cleanup; never reissues DELETE."""
        if self.phase != "cleanup_pending":
            return False
        # Retry the identity steps FIRST, before the delete_local short-circuit.
        # They are the ones that can fail against the OS credential store, and
        # this method used to declare success without ever re-attempting them:
        # with delete_local=False it cleared the marker and emitted "deleted"
        # while the credentials that failed to clear were still in the vault.
        # Both calls are safe to repeat -- clear_identity() returns True when
        # the identity is already gone or superseded, detach_binding() rewrites
        # the same empty binding -- so a retry cannot make a good state worse.
        problems = []
        try:
            if not self.deps.clear_identity():
                problems.append("credentials")
        except Exception:
            problems.append("credentials")
        try:
            if not self.deps.detach_binding():
                problems.append("binding")
        except Exception:
            problems.append("binding")
        if problems:
            self._emit("cleanup_incomplete", problems=tuple(problems))
            return False
        if not self.deps.delete_local:
            clear_marker(self.deps.profile_dir)
            self.phase = "done"
            self._emit("deleted")
            return True
        if not self._delete_local_progress():
            return False
        try:
            pointer_state = str(self.deps.clear_pointer() or "failed")
        except Exception:
            pointer_state = "failed"
        if pointer_state == "failed":
            self._emit("cleanup_incomplete", problems=("pointer",))
            return False
        if pointer_state == "deferred":
            self._emit("cleanup_incomplete", problems=(), pointer="deferred")
            return False
        clear_marker(self.deps.profile_dir)
        self.phase = "done"
        self._emit("deleted")
        return True

    # -------------------------------------------------------- reconciliation
    def reconcile(self) -> Optional[bool]:
        """Resolve an unknown outcome through the ORIGINAL identity.

        True  = account still exists (marker cleared, retry allowed).
        False = authoritatively absent (cleanup proceeds).
        None  = inconclusive; unknown status stays.
        """
        if self.phase not in ("unknown", "cleanup_pending"):
            return None
        try:
            exists = self.deps.check_account_exists()
        except Exception:
            exists = None
        if exists is None:
            self._emit("inconclusive")
            return None
        if exists:
            self.dispatched = False
            self.phase = ""
            clear_marker(self.deps.profile_dir)
            self.deps.resume_online()
            self._emit("exists")
            return True
        self._server_deleted()
        return False
