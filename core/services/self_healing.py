"""Safe, no-throw bridge from runtime failures to the Codex repair runner.

The runtime-facing functions in this module only persist a bounded incident and
start a detached helper.  They never run Codex synchronously and never apply a
generated patch to the active checkout.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import psutil

from core.services.incident_learning import Incident, IncidentLearningStore
from core.services.repair_safety import is_repair_process


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_ROOT = ROOT / "logs" / "self_healing"
RUNNER_PATH = ROOT / "self_heal_runner.py"
REPAIR_ENV_VAR = "HEIYUE_CODEX_REPAIR"
RUNNER_ENV_VAR = "HEIYUE_SELF_HEALING_RUNNER"
STORAGE_ENV_VAR = "HEIYUE_SELF_HEALING_STORAGE_ROOT"
GLOBAL_CLAIM_ENV_VAR = "HEIYUE_SELF_HEALING_GLOBAL_CLAIM"
GLOBAL_TOKEN_ENV_VAR = "HEIYUE_SELF_HEALING_GLOBAL_TOKEN"
DEFAULT_COOLDOWN_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Outcome of a best-effort incident submission."""

    incident_path: Path | None
    fingerprint: str = ""
    dispatched: bool = False
    reason: str = ""


def _recursive_environment(*, allow_during_tests: bool) -> str | None:
    if is_repair_process():
        return REPAIR_ENV_VAR
    if str(os.environ.get(RUNNER_ENV_VAR, "")).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return RUNNER_ENV_VAR
    if not allow_during_tests and os.environ.get("PYTEST_CURRENT_TEST"):
        return "PYTEST_CURRENT_TEST"
    return None


def _read_git_revision() -> str:
    """Read HEAD without spawning Git on the application's failure path."""

    try:
        git_entry = ROOT / ".git"
        git_dir = git_entry
        if git_entry.is_file():
            marker = git_entry.read_text(encoding="utf-8", errors="replace").strip()
            if not marker.casefold().startswith("gitdir:"):
                return "unknown"
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (ROOT / git_dir).resolve()
        head = (git_dir / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
        if not head.startswith("ref:"):
            return head[:64] or "unknown"
        ref_name = head.split(":", 1)[1].strip()
        loose_ref = git_dir / ref_name
        if loose_ref.is_file():
            return loose_ref.read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:64]
        common_dir = git_dir
        commondir_path = git_dir / "commondir"
        if commondir_path.is_file():
            common_dir = (
                git_dir
                / commondir_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            ).resolve()
        packed_refs = common_dir / "packed-refs"
        if packed_refs.is_file():
            suffix = f" {ref_name}"
            for line in packed_refs.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.endswith(suffix):
                    return line.split(" ", 1)[0][:64]
    except OSError:
        pass
    return "unknown"


def _tail(path: Path, *, max_bytes: int = 24_000, max_lines: int = 80) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            payload = stream.read(max_bytes)
    except OSError:
        return ""
    text = payload.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max_lines:])


def _runtime_snapshot() -> Mapping[str, Any] | None:
    try:
        from core.services.runtime_control import runtime_owner

        return runtime_owner(clean_stale=False)
    except Exception:  # noqa: BLE001 - incident reporting must never cascade
        return None


def _base_context(*, include_recent_log: bool) -> dict[str, Any]:
    context: dict[str, Any] = {
        "git_revision": _read_git_revision(),
        "python": sys.version.split()[0],
        "process_id": os.getpid(),
        "runtime_owner": _runtime_snapshot(),
    }
    if include_recent_log:
        recent_log = _tail(ROOT / "logs" / "debug.log")
        if recent_log:
            context["recent_debug_log"] = recent_log
    return context


def _enrich_incident(value: Incident | Mapping[str, Any]) -> Incident | Mapping[str, Any]:
    if isinstance(value, Incident):
        document = value.to_dict(include_fingerprint=False)
    else:
        document = dict(value)
    raw_context = document.get("context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    for key, item in _base_context(include_recent_log=True).items():
        context.setdefault(key, item)
    document["context"] = context
    return document


def _claim_dispatch(
    storage_root: Path,
    fingerprint: str,
    *,
    cooldown_seconds: float,
) -> tuple[Path | None, str]:
    claims_dir = storage_root / "dispatch"
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{fingerprint}.json"
    now = time.time()
    for _attempt in range(2):
        try:
            descriptor = os.open(
                claim_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                age = max(0.0, now - claim_path.stat().st_mtime)
            except OSError:
                continue
            if age < cooldown_seconds:
                return None, "cooldown"
            try:
                claim_path.unlink()
            except OSError:
                return None, "claim_busy"
            continue
        except OSError:
            return None, "claim_failed"
        try:
            payload = json.dumps(
                {"claimed_at": now, "pid": os.getpid()},
                ensure_ascii=False,
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return claim_path, "claimed"
    return None, "claim_busy"


def _global_claim_path(storage_root: Path) -> Path:
    return storage_root / "dispatch" / "global-runner.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _process_is_current(document: Mapping[str, Any]) -> bool:
    if document.get("state") == "launching":
        try:
            return time.time() - float(document.get("claimed_at", 0)) < 60.0
        except (TypeError, ValueError):
            return False
    try:
        process = psutil.Process(int(document["pid"]))
        expected = float(document.get("create_time", 0))
        return process.is_running() and (
            not expected or abs(process.create_time() - expected) < 1.0
        )
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def claim_global_dispatch(storage_root: Path) -> tuple[Path, str] | None:
    """Claim the one automatic Codex runner slot across all fingerprints."""

    path = _global_claim_path(storage_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        token = uuid.uuid4().hex
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            document = _read_json(path)
            if document and _process_is_current(document):
                return None
            if document is None:
                try:
                    age = max(0.0, time.time() - path.stat().st_mtime)
                except OSError:
                    return None
                # Another process may have won O_EXCL but not finished writing
                # its launching claim yet. Treat a fresh invalid file as busy;
                # only reclaim it after the launching lease expires.
                if age < 60.0:
                    return None
            try:
                path.unlink()
            except OSError:
                return None
            continue
        except OSError:
            return None
        try:
            payload = json.dumps(
                {
                    "token": token,
                    "state": "launching",
                    "pid": os.getpid(),
                    "claimed_at": time.time(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path, token
    return None


def adopt_global_dispatch(path: Path, token: str) -> bool:
    document = _read_json(path)
    if not document or document.get("token") != token:
        return False
    try:
        create_time = psutil.Process(os.getpid()).create_time()
    except psutil.Error:
        create_time = 0.0
    _write_json_atomic(
        path,
        {
            "token": token,
            "state": "running",
            "pid": os.getpid(),
            "create_time": create_time,
            "claimed_at": document.get("claimed_at", time.time()),
        },
    )
    return True


def release_global_dispatch(path: Path, token: str) -> None:
    document = _read_json(path)
    if document and document.get("token") == token:
        path.unlink(missing_ok=True)


def _queue_path(storage_root: Path, incident_id: str) -> Path:
    safe_id = hashlib.sha256(
        incident_id.encode("utf-8", errors="replace")
    ).hexdigest()[:32]
    return storage_root / "dispatch" / "pending" / f"{safe_id}.json"


def _enqueue_dispatch(
    incident: Incident,
    *,
    allow_repair: bool,
    storage_root: Path,
) -> Path:
    if incident.persisted_path is None:
        raise ValueError("incident must be persisted before enqueue")
    path = _queue_path(storage_root, incident.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "incident_path": str(incident.persisted_path),
        "incident_id": incident.id,
        "fingerprint": incident.fingerprint,
        "mode": "repair" if allow_repair else "diagnose",
        "queued_at": time.time(),
    }
    # Publish only a complete JSON document. The draining runner must never see
    # the empty/partial window produced by creating the final path before write.
    _write_json_atomic(path, document)
    return path


def pending_dispatches(storage_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    pending_dir = storage_root / "dispatch" / "pending"
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(pending_dir.glob("*.json")):
        document = _read_json(path)
        if document:
            results.append((path, document))
        else:
            path.unlink(missing_ok=True)
    return results


def _runner_command() -> list[str]:
    return [
        sys.executable,
        str(RUNNER_PATH),
        "drain",
        "--enabled",
    ]


def _spawn_runner(
    *,
    storage_root: Path,
    global_claim: Path,
    global_token: str,
) -> None:
    environment = os.environ.copy()
    environment[RUNNER_ENV_VAR] = "1"
    environment[STORAGE_ENV_VAR] = str(storage_root)
    environment[GLOBAL_CLAIM_ENV_VAR] = str(global_claim)
    environment[GLOBAL_TOKEN_ENV_VAR] = global_token
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": False,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(_runner_command(), **kwargs)


def _ensure_global_runner(storage_root: Path) -> str:
    if not pending_dispatches(storage_root):
        return "empty"
    global_claim = claim_global_dispatch(storage_root)
    if global_claim is None:
        return "active"
    global_path, global_token = global_claim
    try:
        _spawn_runner(
            storage_root=storage_root,
            global_claim=global_path,
            global_token=global_token,
        )
    except Exception:  # noqa: BLE001 - runtime reporting must remain no-throw
        release_global_dispatch(global_path, global_token)
        return "spawn_failed"
    return "spawned"


def _dispatch_saved(
    incident: Incident,
    *,
    allow_repair: bool,
    storage_root: Path,
    cooldown_seconds: float,
) -> SubmissionResult:
    path = incident.persisted_path
    if path is None:
        return SubmissionResult(None, incident.fingerprint, False, "not_persisted")
    context = incident.context if isinstance(incident.context, Mapping) else {}
    if context.get("dispatch_allowed") is False:
        return SubmissionResult(path, incident.fingerprint, False, "dispatch_disallowed")
    claim, reason = _claim_dispatch(
        storage_root,
        incident.fingerprint,
        cooldown_seconds=cooldown_seconds,
    )
    if claim is None:
        # A previous enqueue may have succeeded while its detached runner failed
        # to start. A repeated incident inside the cooldown window is also a
        # safe opportunity to wake that durable pending queue.
        if pending_dispatches(storage_root):
            _ensure_global_runner(storage_root)
        return SubmissionResult(path, incident.fingerprint, False, reason)
    try:
        _enqueue_dispatch(
            incident,
            allow_repair=allow_repair,
            storage_root=storage_root,
        )
    except OSError:
        claim.unlink(missing_ok=True)
        return SubmissionResult(path, incident.fingerprint, False, "enqueue_failed")
    runner_state = _ensure_global_runner(storage_root)
    if runner_state == "active":
        return SubmissionResult(path, incident.fingerprint, False, "queued")
    if runner_state == "spawn_failed":
        return SubmissionResult(path, incident.fingerprint, False, "spawn_failed")
    return SubmissionResult(path, incident.fingerprint, True, "dispatched")


def submit_incident(
    incident: Incident | Mapping[str, Any],
    *,
    dispatch: bool,
    allow_repair: bool = False,
    storage_root: str | os.PathLike[str] | None = None,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    allow_during_tests: bool = False,
) -> SubmissionResult:
    """Persist one incident and optionally wake the detached Codex runner.

    This function intentionally swallows all reporting errors.  A broken
    observer must never alter the task outcome that it is observing.
    """

    recursive_marker = _recursive_environment(
        allow_during_tests=allow_during_tests
    )
    if recursive_marker:
        return SubmissionResult(None, reason=f"recursive:{recursive_marker}")
    root = Path(storage_root or DEFAULT_STORAGE_ROOT).resolve()
    try:
        store = IncidentLearningStore(root)
        saved = store.record_incident(_enrich_incident(incident))
        if not dispatch:
            return SubmissionResult(
                saved.persisted_path,
                saved.fingerprint,
                False,
                "recorded_only",
            )
        return _dispatch_saved(
            saved,
            allow_repair=bool(allow_repair),
            storage_root=root,
            cooldown_seconds=max(0.0, float(cooldown_seconds)),
        )
    except Exception:  # noqa: BLE001 - best-effort failure observer
        return SubmissionResult(None, reason="record_failed")


def discover_log_incidents(
    *,
    dispatch: bool,
    allow_repair: bool = False,
    storage_root: str | os.PathLike[str] | None = None,
    include_existing: bool = False,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    allow_during_tests: bool = False,
) -> list[SubmissionResult]:
    """Persist anomalies appended to runtime logs and optionally dispatch them."""

    recursive_marker = _recursive_environment(
        allow_during_tests=allow_during_tests
    )
    if recursive_marker:
        return []
    root = Path(storage_root or DEFAULT_STORAGE_ROOT).resolve()
    try:
        store = IncidentLearningStore(root)
        discovered = store.discover_log_anomalies(
            ROOT / "logs" / "debug.log",
            include_existing=include_existing,
            base_context=_base_context(include_recent_log=False),
        )
    except Exception:  # noqa: BLE001 - log monitoring must never crash runtime
        return []
    results: list[SubmissionResult] = []
    for incident in discovered:
        if not dispatch:
            results.append(
                SubmissionResult(
                    incident.persisted_path,
                    incident.fingerprint,
                    False,
                    "recorded_only",
                )
            )
            continue
        results.append(
            _dispatch_saved(
                incident,
                allow_repair=bool(allow_repair),
                storage_root=root,
                cooldown_seconds=max(0.0, float(cooldown_seconds)),
            )
        )
    if dispatch:
        _ensure_global_runner(root)
    return results


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "GLOBAL_CLAIM_ENV_VAR",
    "GLOBAL_TOKEN_ENV_VAR",
    "SubmissionResult",
    "adopt_global_dispatch",
    "claim_global_dispatch",
    "discover_log_incidents",
    "pending_dispatches",
    "release_global_dispatch",
    "submit_incident",
]
