"""Durable incident discovery and experience tracking.

This module deliberately contains no remediation runner.  It records failures,
discovers new log anomalies, and keeps a small experience/playbook store that a
separate, policy-aware dispatcher can consult.
"""

from __future__ import annotations

import codecs
import hashlib
import itertools
import json
import math
import os
import re
import threading
import traceback as traceback_module
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_ROOT = ROOT / "logs" / "self_healing"
REDACTED = "[REDACTED]"
MAX_REPAIR_HISTORY = 100
MAX_SANITISE_DEPTH = 8
MAX_COLLECTION_ITEMS = 50
MAX_TOTAL_ITEMS = 1_000
MAX_TEXT_CHARS = 20_000
MAX_TOTAL_TEXT_CHARS = 100_000
MAX_KEY_CHARS = 200
MAX_LOG_READ_BYTES = 1_000_000
MAX_LOG_REMAINDER_CHARS = 20_000
MAX_LOG_INCIDENTS_PER_SCAN = 100


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalise_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return _utc_now_text()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    return str(value)


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).replace("-", "_").casefold()


_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|pwd|secret|token|api_?key|access_?key|"
    r"private_?key|authorization|auth|cookie|credential|session_?id|cdk|"
    r"license_?key)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>[\"']?(?:password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|authorization|auth|cookie|credential|"
    r"session[_-]?id|cdk|license[_-]?key)[\"']?\s*[:=]\s*)"
    r"(?P<value>[\"'][^\"'\r\n]*[\"']|[^\s,;}&]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\b"
)


def _is_secret_key(key: object) -> bool:
    return bool(_SECRET_KEY_RE.search(_camel_to_snake(str(key))))


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    value = _JWT_RE.sub(REDACTED, value)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", value
    )


def redact_secrets(value: Any) -> Any:
    """Return a bounded JSON-safe copy with credential material removed."""

    return _sanitise(
        value,
        seen=set(),
        depth=0,
        budget={"items": MAX_TOTAL_ITEMS, "text": MAX_TOTAL_TEXT_CHARS},
    )


def _bounded_redacted_text(value: str, budget: dict[str, int]) -> str:
    available = min(MAX_TEXT_CHARS, max(0, budget["text"]))
    if available <= 0:
        return "<text-budget-exhausted>"

    if len(value) > available:
        marker = f"... <truncated {len(value) - available} characters>"
        prefix_limit = max(0, available - len(marker))
        safe = _redact_text(value[:prefix_limit])[:prefix_limit] + marker
    else:
        safe = _redact_text(value)[:available]
    budget["text"] = max(0, budget["text"] - len(safe))
    return safe


def _bounded_key(value: object) -> str:
    text = str(value)
    text = _redact_text(text[: MAX_KEY_CHARS + 64])
    if len(text) <= MAX_KEY_CHARS:
        return text
    return f"{text[: MAX_KEY_CHARS - 14]}...<truncated>"


def _sanitise(
    value: Any,
    *,
    seen: set[int],
    depth: int,
    budget: dict[str, int],
    key_hint: object | None = None,
) -> Any:
    if key_hint is not None and _is_secret_key(key_hint):
        return REDACTED
    if budget["items"] <= 0:
        return "<item-budget-exhausted>"
    budget["items"] -= 1
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_redacted_text(value, budget)
    if isinstance(value, bytes):
        bounded = value[: MAX_TEXT_CHARS * 4]
        text = bounded.decode("utf-8", errors="replace")
        if len(value) > len(bounded):
            text += f"... <truncated {len(value) - len(bounded)} bytes>"
        return _bounded_redacted_text(text, budget)
    if isinstance(value, Path):
        return _bounded_redacted_text(str(value), budget)
    if isinstance(value, BaseException):
        return _bounded_redacted_text(f"{type(value).__name__}: {value}", budget)
    if depth >= MAX_SANITISE_DEPTH:
        return "<max-depth>"

    identity = id(value)
    if identity in seen:
        return "<recursive>"
    seen.add(identity)
    try:
        if isinstance(value, Incident):
            return _sanitise(
                value._raw_dict(), seen=seen, depth=depth + 1, budget=budget
            )
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            items = list(itertools.islice(value.items(), MAX_COLLECTION_ITEMS + 1))
            for key, item in items[:MAX_COLLECTION_ITEMS]:
                raw_key = str(key)
                text_key = _bounded_key(raw_key)
                result[text_key] = _sanitise(
                    item,
                    seen=seen,
                    depth=depth + 1,
                    budget=budget,
                    key_hint=raw_key[: MAX_KEY_CHARS + 64],
                )
            if len(items) > MAX_COLLECTION_ITEMS:
                result["_truncated_items"] = "more items omitted"
            return result
        if isinstance(value, (list, tuple)):
            items = list(itertools.islice(value, MAX_COLLECTION_ITEMS + 1))
            result = [
                _sanitise(
                    item, seen=seen, depth=depth + 1, budget=budget
                )
                for item in items[:MAX_COLLECTION_ITEMS]
            ]
            if len(items) > MAX_COLLECTION_ITEMS:
                result.append("<more-items-omitted>")
            return result
        if isinstance(value, (set, frozenset)):
            items = list(itertools.islice(value, MAX_COLLECTION_ITEMS + 1))
            result = [
                _sanitise(
                    item, seen=seen, depth=depth + 1, budget=budget
                )
                for item in items[:MAX_COLLECTION_ITEMS]
            ]
            if len(items) > MAX_COLLECTION_ITEMS:
                result.append("<more-items-omitted>")
            return result
        if is_dataclass(value):
            result = {}
            item_fields = fields(value)
            for item_field in item_fields[:MAX_COLLECTION_ITEMS]:
                result[item_field.name] = _sanitise(
                    getattr(value, item_field.name),
                    seen=seen,
                    depth=depth + 1,
                    budget=budget,
                    key_hint=item_field.name,
                )
            if len(item_fields) > MAX_COLLECTION_ITEMS:
                result["_truncated_items"] = "more fields omitted"
            return result
        return _bounded_redacted_text(repr(value), budget)
    finally:
        seen.remove(identity)


_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_LONG_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.IGNORECASE)
_LONG_NUMBER_RE = re.compile(r"\b\d{8,}\b")
_PID_RE = re.compile(
    r"\b(pid|process(?:_id)?|thread(?:_id)?)\s*[:=#]?\s*\d+\b",
    re.IGNORECASE,
)
_LINE_NUMBER_RE = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|sec(?:onds?)?|minutes?)\b",
    re.IGNORECASE,
)
_TRACE_FILE_RE = re.compile(
    r"File\s+([\"'])(?:.*[\\/])?([^\\/\"']+)\1",
    re.IGNORECASE,
)


def _normalise_text_for_fingerprint(value: str) -> str:
    if len(value) > MAX_TEXT_CHARS:
        half = MAX_TEXT_CHARS // 2
        value = value[:half] + " <middle-truncated> " + value[-half:]
    value = _redact_text(value)
    value = _TRACE_FILE_RE.sub(lambda match: f'File "<path>/{match.group(2)}"', value)
    value = _ISO_TIMESTAMP_RE.sub("<timestamp>", value)
    value = _DATE_RE.sub("<date>", value)
    value = _TIME_RE.sub("<time>", value)
    value = _UUID_RE.sub("<uuid>", value)
    value = _LONG_HEX_RE.sub("<hex>", value)
    value = _PID_RE.sub(lambda match: f"{match.group(1)}=<number>", value)
    value = _LINE_NUMBER_RE.sub("line <number>", value)
    value = _DURATION_RE.sub("<duration>", value)
    value = _LONG_NUMBER_RE.sub("<number>", value)
    return " ".join(value.split()).casefold()


def incident_fingerprint(incident: "Incident | Mapping[str, Any]") -> str:
    """Create a deterministic signature while ignoring common volatile values."""

    if isinstance(incident, Incident):
        value: Mapping[str, Any] = incident._raw_dict()
    elif isinstance(incident, Mapping):
        value = dict(incident)
    else:
        raise TypeError("incident must be an Incident or mapping")
    context = value.get("context")
    context = context if isinstance(context, Mapping) else {}
    traceback_text = str(value.get("traceback") or "")
    stack_frames = [
        line.strip()
        for line in traceback_text.splitlines()
        if line.lstrip().startswith("File ")
    ]
    # Keep fingerprint inputs deliberately narrow. Full evidence and the recent
    # log tail are persisted, but including them here would defeat cooldown
    # deduplication whenever an unrelated log line changed.
    identity = {
        "git_revision": str(context.get("git_revision") or "unknown")
        .strip()
        .casefold(),
        "source": str(value.get("source") or "unknown").strip().casefold(),
        "task_key": str(value.get("task_key") or "").strip().casefold(),
        "failure_kind": str(value.get("failure_kind") or "unknown")
        .strip()
        .casefold(),
        "message": _normalise_text_for_fingerprint(
            str(value.get("message") or "")
        ),
        "top_stack_frame": _normalise_text_for_fingerprint(
            stack_frames[-1] if stack_frames else ""
        ),
    }
    if identity["failure_kind"] == "unexpected_result":
        observed = redact_secrets(value.get("observed"))
        if isinstance(observed, (Mapping, list)):
            observed_text = json.dumps(
                observed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            observed_text = str(observed)
        identity["observed"] = _normalise_text_for_fingerprint(
            observed_text[:2000]
        )
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# A compatibility-friendly verb spelling for callers outside this module.
fingerprint_incident = incident_fingerprint


@dataclass(slots=True)
class Incident:
    """A structured observation of one failed or unexpected operation."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_utc_now_text)
    source: str = "unknown"
    task_key: str = ""
    task_name: str = ""
    failure_kind: str = "unknown"
    message: str = ""
    expected: Any = None
    observed: Any = None
    traceback: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    evidence: Any = field(default_factory=list)
    persisted_path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def fingerprint(self) -> str:
        return incident_fingerprint(self)

    def _raw_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "task_key": self.task_key,
            "task_name": self.task_name,
            "failure_kind": self.failure_kind,
            "message": self.message,
            "expected": self.expected,
            "observed": self.observed,
            "traceback": self.traceback,
            "context": self.context,
            "evidence": self.evidence,
        }

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result = redact_secrets(self._raw_dict())
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, persisted_path: Path | None = None
    ) -> "Incident":
        context = value.get("context")
        evidence = value.get("evidence")
        return cls(
            id=str(value.get("id") or uuid.uuid4().hex),
            timestamp=str(value.get("timestamp") or _utc_now_text()),
            source=str(value.get("source") or "unknown"),
            task_key=str(value.get("task_key") or ""),
            task_name=str(value.get("task_name") or ""),
            failure_kind=str(value.get("failure_kind") or "unknown"),
            message=str(value.get("message") or ""),
            expected=value.get("expected"),
            observed=value.get("observed"),
            traceback=str(value.get("traceback") or ""),
            context=dict(context) if isinstance(context, Mapping) else {},
            evidence=[] if evidence is None else evidence,
            persisted_path=persisted_path,
        )

    @classmethod
    def from_exception(
        cls,
        exception: BaseException,
        *,
        source: str,
        task_key: str = "",
        task_name: str = "",
        failure_kind: str | None = None,
        expected: Any = None,
        observed: Any = None,
        context: Mapping[str, Any] | None = None,
        evidence: Iterable[Any] | None = None,
        incident_id: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> "Incident":
        return cls(
            id=incident_id or uuid.uuid4().hex,
            timestamp=_normalise_timestamp(timestamp),
            source=source,
            task_key=task_key,
            task_name=task_name,
            failure_kind=failure_kind or type(exception).__name__,
            message=str(exception),
            expected=expected,
            observed=observed,
            traceback="".join(
                traceback_module.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            ),
            context=dict(context or {}),
            evidence=list(evidence or []),
        )


_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.resolve()))
    with _LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


@contextmanager
def _process_file_lock(path: Path):
    """Serialize experience read-modify-write cycles across GUI/runner processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    payload = json.dumps(
        redact_secrets(value), ensure_ascii=False, indent=2, sort_keys=True
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_mapping(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _safe_filename(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value) and value not in {".", ".."}:
        return value
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _truncate_text(value: Any, limit: int = 500) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 14]}...<truncated>"


def _concise(value: Any, *, depth: int = 0) -> Any:
    value = redact_secrets(value)
    if isinstance(value, str):
        return _truncate_text(value)
    if depth >= 3:
        return _truncate_text(value, 200)
    if isinstance(value, Mapping):
        # Keep enough room for the fixed experience metadata plus the recent
        # repair list; arbitrary caller context is still bounded.
        items = list(value.items())[:20]
        result = {str(key): _concise(item, depth=depth + 1) for key, item in items}
        if len(value) > len(items):
            result["_truncated_keys"] = len(value) - len(items)
        return result
    if isinstance(value, list):
        items = value[:10]
        result = [_concise(item, depth=depth + 1) for item in items]
        if len(value) > len(items):
            result.append(f"...<{len(value) - len(items)} more>")
        return result
    return value


class IncidentLearningStore:
    """Atomic incident and experience repository scoped to one storage root."""

    def __init__(
        self,
        storage_root: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime | str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.storage_root = Path(storage_root or DEFAULT_STORAGE_ROOT).resolve()
        self.incidents_dir = self.storage_root / "incidents"
        self.experience_path = self.storage_root / "experience.json"
        self.cursor_path = self.storage_root / "log_cursors.json"
        self.lock_path = self.storage_root / ".store.lock"
        self._clock = clock or _utc_now_text
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = _lock_for(self.storage_root)

    def _now(self) -> str:
        return _normalise_timestamp(self._clock())

    def new_incident(self, **values: Any) -> Incident:
        values.setdefault("id", self._id_factory())
        values.setdefault("timestamp", self._now())
        return Incident(**values)

    def incident_path(self, incident_id: str) -> Path:
        return self.incidents_dir / f"{_safe_filename(str(incident_id))}.json"

    def load_incident(self, path_or_id: str | os.PathLike[str]) -> Incident | None:
        candidate = Path(path_or_id)
        if not candidate.is_absolute() and candidate.parent == Path("."):
            if candidate.suffix != ".json":
                candidate = self.incident_path(str(path_or_id))
            else:
                candidate = self.incidents_dir / candidate
        data = _read_mapping(candidate)
        if not data:
            return None
        return Incident.from_dict(data, persisted_path=candidate.resolve())

    def _load_experience(self) -> dict[str, Any]:
        value = _read_mapping(self.experience_path)
        if not value or not isinstance(value.get("experiences"), dict):
            return {"version": 1, "experiences": {}}
        value.setdefault("version", 1)
        return value

    @staticmethod
    def _new_experience(fingerprint: str) -> dict[str, Any]:
        return {
            "fingerprint": fingerprint,
            "occurrence_count": 0,
            "first_seen": "",
            "last_seen": "",
            "sources": {},
            "repair_attempt_count": 0,
            "repair_outcomes": {},
            "repair_attempts": [],
        }

    def record_incident(
        self, incident: Incident | Mapping[str, Any]
    ) -> Incident:
        if isinstance(incident, Mapping):
            incident = Incident.from_dict(incident)
        if not isinstance(incident, Incident):
            raise TypeError("incident must be an Incident or mapping")

        document = incident.to_dict(include_fingerprint=True)
        path = self.incident_path(str(document["id"]))
        with self._lock, _process_file_lock(self.lock_path):
            existing = _read_mapping(path) if path.exists() else None
            is_new = existing is None
            if existing and str(existing.get("id")) != str(document["id"]):
                raise ValueError(f"incident id collision at {path}")
            if existing != document:
                _atomic_write_json(path, document)
            if is_new:
                experience = self._load_experience()
                entries = experience["experiences"]
                fingerprint = str(document["fingerprint"])
                entry = entries.setdefault(
                    fingerprint, self._new_experience(fingerprint)
                )
                seen_at = str(document["timestamp"])
                entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
                entry["first_seen"] = entry.get("first_seen") or seen_at
                entry["last_seen"] = seen_at
                source = str(document.get("source") or "unknown")
                sources = entry.setdefault("sources", {})
                sources[source] = int(sources.get(source, 0)) + 1
                entry.update(
                    {
                        "task_key": document.get("task_key", ""),
                        "task_name": document.get("task_name", ""),
                        "failure_kind": document.get("failure_kind", "unknown"),
                        "latest_incident_id": document["id"],
                        "latest_message": _truncate_text(document.get("message", "")),
                        "latest_context": _concise(document.get("context", {})),
                        "latest_evidence": _concise(document.get("evidence", [])),
                    }
                )
                _atomic_write_json(self.experience_path, experience)
        return Incident.from_dict(document, persisted_path=path)

    # A short name that reads naturally at call sites.
    save_incident = record_incident

    def record_repair_outcome(
        self,
        fingerprint: str | Incident,
        outcome: str | bool,
        *,
        summary: str = "",
        details: Any = None,
        timestamp: datetime | str | None = None,
    ) -> dict[str, Any]:
        fingerprint_text = (
            fingerprint.fingerprint if isinstance(fingerprint, Incident) else str(fingerprint)
        )
        if not fingerprint_text:
            raise ValueError("fingerprint must not be empty")
        if isinstance(outcome, bool):
            outcome_text = "success" if outcome else "failure"
        else:
            outcome_text = str(outcome).strip().casefold() or "unknown"
        occurred_at = _normalise_timestamp(timestamp) if timestamp else self._now()
        attempt = redact_secrets(
            {
                "id": self._id_factory(),
                "timestamp": occurred_at,
                "outcome": outcome_text,
                "summary": _truncate_text(summary),
                "details": _concise(details),
            }
        )
        with self._lock, _process_file_lock(self.lock_path):
            experience = self._load_experience()
            entries = experience["experiences"]
            entry = entries.setdefault(
                fingerprint_text, self._new_experience(fingerprint_text)
            )
            entry["repair_attempt_count"] = int(
                entry.get("repair_attempt_count", 0)
            ) + 1
            outcomes = entry.setdefault("repair_outcomes", {})
            outcomes[outcome_text] = int(outcomes.get(outcome_text, 0)) + 1
            attempts = entry.setdefault("repair_attempts", [])
            attempts.append(attempt)
            del attempts[:-MAX_REPAIR_HISTORY]
            entry["last_repair_at"] = occurred_at
            _atomic_write_json(self.experience_path, experience)
            return json.loads(json.dumps(entry, ensure_ascii=False))

    # Existing callers may describe the same operation as an attempt.
    record_repair_attempt = record_repair_outcome

    def prior_context(
        self, fingerprint: str | Incident, *, max_attempts: int = 5
    ) -> dict[str, Any] | None:
        fingerprint_text = (
            fingerprint.fingerprint if isinstance(fingerprint, Incident) else str(fingerprint)
        )
        with self._lock, _process_file_lock(self.lock_path):
            experience = self._load_experience()
            entry = experience["experiences"].get(fingerprint_text)
            if not isinstance(entry, dict):
                return None
            attempts = list(entry.get("repair_attempts", []))
            result = {
                key: value
                for key, value in entry.items()
                if key != "repair_attempts"
            }
            result["recent_repair_attempts"] = (
                attempts[-max(0, int(max_attempts)) :] if max_attempts else []
            )
            return _concise(result)

    get_prior_context = prior_context

    def discover_log_anomalies(
        self,
        log_paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        *,
        source: str = "log_monitor",
        include_existing: bool = False,
        base_context: Mapping[str, Any] | None = None,
    ) -> list[Incident]:
        return _discover_log_anomalies(
            log_paths,
            store=self,
            source=source,
            include_existing=include_existing,
            base_context=base_context,
        )


IncidentStore = IncidentLearningStore


_SEVERITY_RE = re.compile(r"\b(ERROR|CRITICAL)\b", re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"Traceback\s*\(most recent call last\)", re.IGNORECASE)
_EXPLICIT_FAILURE_RE = re.compile(
    r"(?:\bfailed\b|\bfailure\b|\bexception\b|\bfatal\b|\btimed?\s*out\b|"
    r"\bunable\s+to\b|\bcould\s+not\b|\bdid\s+not\b|\bnot\s+found\b|"
    r"\bunexpected\s+result\b|失败|异常|错误|超时|无法|未能|未找到|"
    r"未检测到|没有发生|未发生|不符合预期|预期结果.*(?:未|没有))",
    re.IGNORECASE,
)
_EXCEPTION_FINAL_RE = re.compile(
    r"^\s*(?:[A-Za-z_][\w.]*)(?:Error|Exception|Interrupt|Warning)\s*(?::|$)"
)
_LOG_ENTRY_START_RE = re.compile(
    r"^\s*(?:(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}[^\r\n]*?)?"
    r"\b(?:TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\b\s*(?:[|:\-])",
    re.IGNORECASE,
)
_STRUCTURED_SELF_HEALING_LOG_RE = re.compile(
    r"\|\s*(?:task_queue|debug_runner|dashboard_interface|self_healing|"
    r"codex_repair|incident_learning)\.",
    re.IGNORECASE,
)


def _path_identity(stat_result: os.stat_result) -> str:
    return f"{getattr(stat_result, 'st_dev', 0)}:{getattr(stat_result, 'st_ino', 0)}"


def _file_anchor(path: Path, offset: int, length: int = 64) -> str:
    if offset <= 0:
        return ""
    try:
        with path.open("rb") as stream:
            start = max(0, offset - length)
            stream.seek(start)
            payload = stream.read(offset - start)
    except OSError:
        return ""
    return hashlib.sha256(payload).hexdigest()


def _load_cursor(store: IncidentLearningStore) -> dict[str, Any]:
    value = _read_mapping(store.cursor_path)
    if not value or not isinstance(value.get("files"), dict):
        return {"version": 1, "files": {}}
    value.setdefault("version", 1)
    return value


def _log_paths(
    value: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
) -> list[Path]:
    if isinstance(value, (str, os.PathLike)):
        return [Path(value).resolve()]
    return [Path(item).resolve() for item in value]


def _read_increment(
    path: Path,
    previous: Mapping[str, Any] | None,
    *,
    include_existing: bool,
) -> tuple[str, dict[str, Any]]:
    try:
        stat_result = path.stat()
    except OSError:
        return "", {"missing": True, "offset": 0, "remainder": ""}

    size = int(stat_result.st_size)
    identity = _path_identity(stat_result)
    if previous is None and not include_existing:
        return "", {
            "identity": identity,
            "offset": size,
            "anchor": _file_anchor(path, size),
            "remainder": "",
        }

    previous = previous or {}
    offset = int(previous.get("offset", 0) or 0)
    remainder = str(previous.get("remainder", "") or "")
    reset = bool(previous.get("missing"))
    old_identity = str(previous.get("identity", ""))
    if old_identity and old_identity != identity:
        reset = True
    if size < offset:
        reset = True
    old_anchor = str(previous.get("anchor", ""))
    if offset and old_anchor and _file_anchor(path, offset) != old_anchor:
        reset = True
    if reset:
        offset = 0
        remainder = ""

    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            payload = stream.read(MAX_LOG_READ_BYTES)
    except OSError:
        return "", dict(previous)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoded = decoder.decode(payload, final=False)
    undecoded_bytes, _ = decoder.getstate()
    consumed = len(payload) - len(undecoded_bytes)
    new_offset = offset + consumed
    combined = remainder + decoded
    newline_at = max(combined.rfind("\n"), combined.rfind("\r"))
    if newline_at < 0:
        complete = ""
        new_remainder = combined
    else:
        complete = combined[: newline_at + 1]
        new_remainder = combined[newline_at + 1 :]
    return complete, {
        "identity": identity,
        "offset": new_offset,
        "anchor": _file_anchor(path, new_offset),
        "remainder": _redact_text(new_remainder[-MAX_LOG_REMAINDER_CHARS:]),
    }


def _traceback_end(lines: list[str], start: int) -> int:
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if end > start + 1 and _LOG_ENTRY_START_RE.search(line):
            break
        end += 1
        if _EXCEPTION_FINAL_RE.search(line):
            break
    return end


def _incident_candidates(
    text: str,
    *,
    path: Path,
    store: IncidentLearningStore,
    source: str,
    base_context: Mapping[str, Any] | None,
) -> list[Incident]:
    lines = text.splitlines()
    candidates: list[Incident] = []
    index = 0
    while index < len(lines) and len(candidates) < MAX_LOG_INCIDENTS_PER_SCAN:
        line = lines[index]
        if path.name.casefold() == "debug.log" and _STRUCTURED_SELF_HEALING_LOG_RE.search(
            line
        ):
            # These modules already submit a structured incident after resource
            # cleanup. Skip their complete entry, including the traceback.
            index += 1
            while index < len(lines) and not _LOG_ENTRY_START_RE.search(lines[index]):
                index += 1
            continue
        if _TRACEBACK_RE.search(line):
            end = _traceback_end(lines, index)
            block_lines = lines[index:end]
            block = "\n".join(block_lines)
            message = next(
                (item.strip() for item in reversed(block_lines) if item.strip()),
                "Traceback",
            )
            candidates.append(
                store.new_incident(
                    source=source,
                    task_key=path.name,
                    task_name=f"log:{path.name}",
                    failure_kind="traceback",
                    message=message,
                    observed=block,
                    traceback=block,
                    context={
                        **dict(base_context or {}),
                        "log_path": str(path),
                        "line_start": index + 1,
                    },
                    evidence=[{"kind": "log_excerpt", "value": block}],
                )
            )
            index = end
            continue

        severity = _SEVERITY_RE.search(line)
        explicit = (
            None
            if path.name.casefold() == "debug.log"
            else _EXPLICIT_FAILURE_RE.search(line)
        )
        if severity or explicit:
            if severity:
                entry_end = index + 1
                while entry_end < len(lines) and not _LOG_ENTRY_START_RE.search(
                    lines[entry_end]
                ):
                    entry_end += 1
                traceback_at = next(
                    (
                        candidate
                        for candidate in range(index + 1, entry_end)
                        if _TRACEBACK_RE.search(lines[candidate])
                    ),
                    None,
                )
                if traceback_at is not None:
                    index = traceback_at
                    continue
            failure_kind = (
                f"log_{severity.group(1).casefold()}"
                if severity
                else "explicit_failure"
            )
            candidates.append(
                store.new_incident(
                    source=source,
                    task_key=path.name,
                    task_name=f"log:{path.name}",
                    failure_kind=failure_kind,
                    message=line.strip(),
                    observed=line,
                    context={
                        **dict(base_context or {}),
                        "log_path": str(path),
                        "line_start": index + 1,
                    },
                    evidence=[{"kind": "log_line", "value": line}],
                )
            )
        index += 1
    return candidates


def _discover_log_anomalies(
    log_paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    store: IncidentLearningStore,
    source: str,
    include_existing: bool,
    base_context: Mapping[str, Any] | None,
) -> list[Incident]:
    discovered: list[Incident] = []
    with store._lock:
        cursor = _load_cursor(store)
        cursor_files = cursor["files"]
        for path in _log_paths(log_paths):
            key = os.path.normcase(str(path))
            previous = cursor_files.get(key)
            text, current = _read_increment(
                path,
                previous if isinstance(previous, Mapping) else None,
                include_existing=include_existing,
            )
            current["updated_at"] = store._now()
            cursor_files[key] = current
            unique: dict[str, Incident] = {}
            for incident in _incident_candidates(
                text,
                path=path,
                store=store,
                source=source,
                base_context=base_context,
            ):
                unique.setdefault(incident.fingerprint, incident)
            for incident in unique.values():
                discovered.append(store.record_incident(incident))
        _atomic_write_json(store.cursor_path, cursor)
    return discovered


def discover_log_anomalies(
    log_paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    storage_root: str | os.PathLike[str] | None = None,
    store: IncidentLearningStore | None = None,
    source: str = "log_monitor",
    include_existing: bool = False,
    base_context: Mapping[str, Any] | None = None,
) -> list[Incident]:
    """Persist unique anomalies found only in newly appended UTF-8 log text.

    On the first observation of an existing file, the default establishes a
    cursor at EOF so enabling the monitor does not replay historical failures.
    Pass ``include_existing=True`` for an intentional one-time backfill.
    """

    if store is not None and storage_root is not None:
        raise ValueError("pass either store or storage_root, not both")
    repository = store or IncidentLearningStore(storage_root)
    return _discover_log_anomalies(
        log_paths,
        store=repository,
        source=source,
        include_existing=include_existing,
        base_context=base_context,
    )


def incident_path(
    incident_id: str, *, storage_root: str | os.PathLike[str] | None = None
) -> Path:
    return IncidentLearningStore(storage_root).incident_path(incident_id)


def load_incident(
    path_or_id: str | os.PathLike[str],
    *,
    storage_root: str | os.PathLike[str] | None = None,
) -> Incident | None:
    return IncidentLearningStore(storage_root).load_incident(path_or_id)


def record_incident(
    incident: Incident | Mapping[str, Any] | None = None,
    *,
    storage_root: str | os.PathLike[str] | None = None,
    **values: Any,
) -> Incident:
    store = IncidentLearningStore(storage_root)
    if incident is not None and values:
        raise ValueError("pass either incident or incident fields, not both")
    target = incident if incident is not None else store.new_incident(**values)
    return store.record_incident(target)


def record_repair_outcome(
    fingerprint: str | Incident,
    outcome: str | bool,
    *,
    storage_root: str | os.PathLike[str] | None = None,
    summary: str = "",
    details: Any = None,
    timestamp: datetime | str | None = None,
) -> dict[str, Any]:
    return IncidentLearningStore(storage_root).record_repair_outcome(
        fingerprint,
        outcome,
        summary=summary,
        details=details,
        timestamp=timestamp,
    )


def prior_context(
    fingerprint: str | Incident,
    *,
    storage_root: str | os.PathLike[str] | None = None,
    max_attempts: int = 5,
) -> dict[str, Any] | None:
    return IncidentLearningStore(storage_root).prior_context(
        fingerprint, max_attempts=max_attempts
    )


__all__ = [
    "DEFAULT_STORAGE_ROOT",
    "Incident",
    "IncidentLearningStore",
    "IncidentStore",
    "discover_log_anomalies",
    "fingerprint_incident",
    "incident_fingerprint",
    "incident_path",
    "load_incident",
    "prior_context",
    "record_incident",
    "record_repair_outcome",
    "redact_secrets",
]
