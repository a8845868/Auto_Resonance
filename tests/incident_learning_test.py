import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from itertools import count

from core.services.incident_learning import (
    MAX_COLLECTION_ITEMS,
    MAX_LOG_INCIDENTS_PER_SCAN,
    MAX_LOG_READ_BYTES,
    MAX_TEXT_CHARS,
    REDACTED,
    Incident,
    IncidentLearningStore,
    discover_log_anomalies,
    incident_fingerprint,
    load_incident,
    prior_context,
    redact_secrets,
    record_incident,
    record_repair_outcome,
)


def _store(tmp_path):
    sequence = count(1)
    return IncidentLearningStore(
        tmp_path / "self_healing",
        clock=lambda: "2026-07-14T10:00:00Z",
        id_factory=lambda: f"generated-{next(sequence)}",
    )


def _process_report_incident(arguments):
    root, index = arguments
    store = IncidentLearningStore(root)
    return store.record_incident(
        Incident(
            id=f"process-{index}",
            source="worker",
            task_key="same-process-task",
            failure_kind="same-process-failure",
            message="same stable process failure",
        )
    ).fingerprint


def test_incident_round_trip_is_atomic_json_safe_and_recursively_redacted(tmp_path):
    store = _store(tmp_path)
    recursive = {"safe": "visible", "password": "plain-password"}
    recursive["self"] = recursive
    incident = Incident(
        id="incident-1",
        timestamp="2026-07-14T10:00:00Z",
        source="task_queue",
        task_key="weekly_trade",
        task_name="Weekly trade",
        failure_kind="unexpected_result",
        message="request failed token=message-secret",
        expected={"success": True},
        observed={"success": False, "apiKey": "observed-secret"},
        traceback="RuntimeError: password=trace-secret",
        context={
            "credentials": {"access_token": "nested-secret"},
            "cycle": recursive,
            "path": tmp_path / "screen.png",
        },
        evidence=[
            {"authorization": "Bearer raw-secret"},
            b"cookie=byte-secret",
            ValueError("secret=safe-redacted"),
            "Mirror CDK=cdk-secret",
        ],
    )

    recorded = store.record_incident(incident)

    assert recorded.persisted_path == store.incident_path("incident-1")
    assert recorded.persisted_path.is_file()
    assert not list(store.storage_root.rglob("*.tmp"))
    document_text = recorded.persisted_path.read_text(encoding="utf-8")
    document = json.loads(document_text)
    assert set(document) == {
        "context",
        "evidence",
        "expected",
        "failure_kind",
        "fingerprint",
        "id",
        "message",
        "observed",
        "source",
        "task_key",
        "task_name",
        "timestamp",
        "traceback",
    }
    for secret in (
        "plain-password",
        "message-secret",
        "observed-secret",
        "trace-secret",
        "nested-secret",
        "raw-secret",
        "byte-secret",
        "safe-redacted",
        "cdk-secret",
    ):
        assert secret not in document_text
    assert document["observed"]["apiKey"] == REDACTED
    assert document["context"]["credentials"]["access_token"] == REDACTED
    assert document["context"]["cycle"]["self"] == "<recursive>"
    assert document["fingerprint"] == recorded.fingerprint

    loaded_by_id = store.load_incident("incident-1")
    loaded_by_path = load_incident(
        recorded.persisted_path, storage_root=store.storage_root
    )
    assert loaded_by_id is not None
    assert loaded_by_path is not None
    assert loaded_by_id.to_dict() == loaded_by_path.to_dict() == document
    assert loaded_by_id.persisted_path == recorded.persisted_path.resolve()
    assert store.incident_path("../escape").parent == store.incidents_dir


def test_sanitiser_bounds_deep_large_and_wide_untrusted_values(tmp_path):
    deep = {"password": "deep-secret"}
    for _ in range(1_100):
        deep = {"child": deep}

    sanitised = redact_secrets(
        {
            "deep": deep,
            "huge": "token=huge-secret " + ("x" * 3_000_000),
            "huge_key": {"token_" + ("k" * 3_000_000): "key-secret"},
            "wide": list(range(MAX_COLLECTION_ITEMS + 500)),
        }
    )
    text = json.dumps(sanitised, ensure_ascii=False)

    assert "<max-depth>" in text
    assert "deep-secret" not in text
    assert "huge-secret" not in text
    assert "key-secret" not in text
    assert "<more-items-omitted>" in text
    assert len(sanitised["huge"]) <= MAX_TEXT_CHARS
    assert len(text) < 150_000

    store = _store(tmp_path)
    recorded = store.record_incident(
        Incident(
            id="bounded-incident",
            source="untrusted",
            failure_kind="oversized",
            message="token=persist-secret " + ("y" * 3_000_000),
            context={"deep": deep, "wide": list(range(5_000))},
        )
    )
    persisted = recorded.persisted_path.read_text(encoding="utf-8")
    assert "persist-secret" not in persisted
    assert len(persisted) < 150_000


def test_fingerprint_normalises_volatile_values_but_keeps_failure_identity():
    first = Incident(
        id="75df7973-b14c-4d89-9c26-118ec9c62d0b",
        timestamp="2026-07-14T10:00:00+08:00",
        source="DEBUG_RUNNER",
        task_key="probe",
        task_name="Probe",
        failure_kind="RuntimeError",
        message=(
            "failed at 2026-07-14 10:00:00 pid=1234 request 12345678 "
            "token=first-secret"
        ),
        traceback='File "C:\\worktree-a\\runner.py", line 81\nRuntimeError: bad',
        context={
            "created_at": "2026-07-14T10:00:00Z",
            "line_number": 81,
            "trace_id": "abc",
            "api_key": "first",
        },
    )
    second = Incident(
        id="8762e19c-cb3e-4a11-929e-c8cf047f4021",
        timestamp="2026-07-14T11:12:13Z",
        source="debug_runner",
        task_key="probe",
        task_name="probe",
        failure_kind="runtimeerror",
        message=(
            "failed at 2026-07-15 22:33:44 pid 9999 request 87654321 "
            "token=second-secret"
        ),
        traceback='File "D:\\other-root\\runner.py", line 999\nRuntimeError: bad',
        context={
            "created_at": "2027-01-01T00:00:00Z",
            "line_number": 999,
            "trace_id": "different",
            "api_key": "second",
        },
    )

    assert incident_fingerprint(first) == incident_fingerprint(second)

    different = Incident.from_dict(second.to_dict())
    different.failure_kind = "ValueError"
    assert incident_fingerprint(first) != incident_fingerprint(different)


def test_fingerprint_ignores_evidence_churn_but_tracks_code_revision():
    first = Incident(
        source="task_queue",
        task_key="daily",
        failure_kind="unexpected_result",
        message="expected result did not happen",
        context={"git_revision": "abc123", "recent_debug_log": "first tail"},
        evidence=[{"value": "first screenshot"}],
    )
    second = Incident(
        source="task_queue",
        task_key="daily",
        failure_kind="unexpected_result",
        message="expected result did not happen",
        context={"git_revision": "abc123", "recent_debug_log": "changed tail"},
        evidence=[{"value": "different screenshot"}],
    )

    assert incident_fingerprint(first) == incident_fingerprint(second)

    second.context["git_revision"] = "def456"
    assert incident_fingerprint(first) != incident_fingerprint(second)


def test_unexpected_result_fingerprint_keeps_distinct_observed_failures():
    first = Incident(
        source="task_queue",
        task_key="daily",
        failure_kind="unexpected_result",
        message="任务未返回明确成功结果",
        observed={"success": False, "error": "not found"},
    )
    second = Incident.from_dict(first.to_dict())
    second.observed = {"success": False, "error": "timeout"}

    assert incident_fingerprint(first) != incident_fingerprint(second)


def test_experience_accumulates_occurrences_repairs_and_concise_prior_context(
    tmp_path,
):
    store = _store(tmp_path)
    long_message = "failed " + ("x" * 900)
    first = Incident(
        id="first",
        timestamp="2026-07-14T10:00:00Z",
        source="runner",
        task_key="trade",
        failure_kind="timeout",
        message=long_message,
        context={"password": "one", "phase": "buy"},
    )
    second = Incident(
        id="second",
        timestamp="2026-07-14T10:05:00Z",
        source="runner",
        task_key="trade",
        failure_kind="timeout",
        message=long_message,
        context={"password": "two", "phase": "buy"},
    )

    recorded_first = store.record_incident(first)
    store.record_incident(second)
    store.record_incident(first)  # Re-saving one incident id is idempotent.
    store.record_repair_outcome(
        recorded_first.fingerprint,
        True,
        summary="retry succeeded",
        details={"token": "repair-secret", "tests": 3},
        timestamp="2026-07-14T10:10:00Z",
    )
    store.record_repair_attempt(
        recorded_first.fingerprint,
        "failure",
        summary="regression test failed",
        timestamp="2026-07-14T10:11:00Z",
    )

    context = store.prior_context(recorded_first.fingerprint, max_attempts=1)
    assert context is not None
    assert context["occurrence_count"] == 2
    assert context["first_seen"] == "2026-07-14T10:00:00Z"
    assert context["last_seen"] == "2026-07-14T10:05:00Z"
    assert context["repair_attempt_count"] == 2
    assert context["repair_outcomes"] == {"failure": 1, "success": 1}
    assert len(context["recent_repair_attempts"]) == 1
    assert context["recent_repair_attempts"][0]["outcome"] == "failure"
    assert "<truncated>" in context["latest_message"]
    assert "repair-secret" not in store.experience_path.read_text(encoding="utf-8")
    assert not list(store.storage_root.rglob("*.tmp"))


def test_concurrent_reporters_do_not_lose_experience_updates(tmp_path):
    root = tmp_path / "shared"

    def report(index):
        # Separate instances exercise the process-wide per-root lock.
        store = IncidentLearningStore(root)
        return store.record_incident(
            Incident(
                id=f"concurrent-{index}",
                source="worker",
                task_key="same-task",
                failure_kind="same-failure",
                message="same stable failure",
                context={"pid": 1000 + index},
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        incidents = list(executor.map(report, range(32)))

    assert len({item.fingerprint for item in incidents}) == 1
    store = IncidentLearningStore(root)
    context = store.prior_context(incidents[0].fingerprint)
    assert context["occurrence_count"] == 32
    assert len(list(store.incidents_dir.glob("*.json"))) == 32
    assert not list(root.rglob("*.tmp"))


def test_cross_process_reporters_do_not_lose_experience_updates(tmp_path):
    root = tmp_path / "multiprocess"
    context = multiprocessing.get_context("spawn")
    arguments = [(str(root), index) for index in range(8)]

    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        fingerprints = list(executor.map(_process_report_incident, arguments))

    assert len(set(fingerprints)) == 1
    store = IncidentLearningStore(root)
    prior = store.prior_context(fingerprints[0])
    assert prior["occurrence_count"] == 8


def test_log_discovery_starts_at_eof_then_reads_only_complete_new_utf8_lines(
    tmp_path,
):
    log_path = tmp_path / "debug.log"
    log_path.write_text(
        "09:00:00 - ERROR | old.failure:1 - old historical failure\n",
        encoding="utf-8",
    )
    store = _store(tmp_path)

    assert store.discover_log_anomalies(log_path) == []
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("10:00:00 - INFO | worker.run:1 - 正常\n")
        stream.write(
            "10:01:00 - ERROR | worker.run:2 - operation failed request 12345678\n"
        )
        stream.write(
            "10:02:00 - ERROR | worker.run:2 - operation failed request 87654321\n"
        )
        stream.write("Traceback (most recent call last):\n")
        stream.write('  File "C:\\first\\worker.py", line 91, in run\n')
        stream.write("ValueError: 中文异常 token=log-secret\n")

    found = store.discover_log_anomalies(log_path)
    assert [item.failure_kind for item in found] == ["log_error", "traceback"]
    assert all(item.persisted_path and item.persisted_path.is_file() for item in found)
    assert store.discover_log_anomalies(log_path) == []

    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("10:03:00 - CRITICAL | worker.run:3 - 中文失败")
    assert store.discover_log_anomalies(log_path) == []
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(" password=tail-secret\n")
    final = store.discover_log_anomalies(log_path)
    assert len(final) == 1
    assert final[0].failure_kind == "log_critical"
    persisted = final[0].persisted_path.read_text(encoding="utf-8")
    assert "tail-secret" not in persisted
    assert "log-secret" not in "".join(
        item.persisted_path.read_text(encoding="utf-8") for item in found
    )
    cursor = json.loads(store.cursor_path.read_text(encoding="utf-8"))
    assert len(cursor["files"]) == 1


def test_log_discovery_reads_bounded_chunks_and_caps_incidents_per_scan(tmp_path):
    log_path = tmp_path / "service.log"
    normal_line = b"INFO normal\n"
    prefix = normal_line * (MAX_LOG_READ_BYTES // len(normal_line) + 20)
    log_path.write_bytes(prefix + b"ERROR late bounded failure\n")
    store = IncidentLearningStore(tmp_path / "bounded-log")

    assert store.discover_log_anomalies(log_path, include_existing=True) == []
    cursor = json.loads(store.cursor_path.read_text(encoding="utf-8"))
    state = next(iter(cursor["files"].values()))
    assert state["offset"] <= MAX_LOG_READ_BYTES

    second = store.discover_log_anomalies(log_path, include_existing=True)
    assert len(second) == 1
    assert second[0].message == "ERROR late bounded failure"

    many_path = tmp_path / "many.log"
    many_path.write_text(
        "".join(f"ERROR unique-code-{index:03d}\n" for index in range(150)),
        encoding="utf-8",
    )
    many = IncidentLearningStore(tmp_path / "many-log").discover_log_anomalies(
        many_path, include_existing=True
    )
    assert len(many) == MAX_LOG_INCIDENTS_PER_SCAN


def test_log_discovery_can_backfill_and_detects_rewrite_truncation_and_rotation(
    tmp_path,
):
    log_path = tmp_path / "service.log"
    log_path.write_text("ERROR historical failure\n", encoding="utf-8")
    backfill_store = IncidentLearningStore(tmp_path / "backfill")
    assert len(
        discover_log_anomalies(
            log_path, store=backfill_store, include_existing=True
        )
    ) == 1

    store = IncidentLearningStore(tmp_path / "monitor")
    assert store.discover_log_anomalies(log_path) == []
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write("ERROR appended failure\n")
    assert len(store.discover_log_anomalies(log_path)) == 1

    # Rewrite to the same size: the saved anchor, not just size, detects it.
    old_size = log_path.stat().st_size
    replacement = "CRITICAL rewritten failure\n".ljust(old_size, "x")
    log_path.write_text(replacement, encoding="utf-8")
    rewritten = store.discover_log_anomalies(log_path)
    assert len(rewritten) == 1
    assert rewritten[0].failure_kind == "log_critical"

    # Truncation clears any pending partial line and resumes at byte zero.
    log_path.write_text("任务预期结果没有发生\n", encoding="utf-8")
    truncated = store.discover_log_anomalies(log_path)
    assert len(truncated) == 1
    assert truncated[0].failure_kind == "explicit_failure"

    # Replacing the file changes its identity and starts the rotated file at zero.
    rotated = tmp_path / "service.log.1"
    log_path.replace(rotated)
    log_path.write_text("ERROR rotation failure\n", encoding="utf-8")
    rotation = store.discover_log_anomalies(log_path)
    assert len(rotation) == 1
    assert rotation[0].message == "ERROR rotation failure"


def test_debug_log_ignores_structured_self_healing_and_warning_text(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text(
        "10:00:00 - WARNING | dashboard_interface._queueFinished:1 - "
        "检测到任务异常，自动调度已暂停\n"
        "10:00:01 - ERROR | task_queue.run:2 - 任务执行失败\n"
        "Traceback (most recent call last):\n"
        '  File "C:\\repo\\task_queue.py", line 99, in run\n'
        "RuntimeError: structured failure\n",
        encoding="utf-8",
    )
    store = IncidentLearningStore(tmp_path / "store")

    assert store.discover_log_anomalies(log_path, include_existing=True) == []


def test_debug_log_keeps_one_unstructured_traceback_incident(tmp_path):
    log_path = tmp_path / "debug.log"
    log_path.write_text(
        "10:00:01 - ERROR | unknown_worker.run:2 - operation failed\n"
        "Traceback (most recent call last):\n"
        '  File "C:\\repo\\worker.py", line 8, in run\n'
        "ValueError: unexpected state\n",
        encoding="utf-8",
    )
    store = IncidentLearningStore(tmp_path / "store")

    incidents = store.discover_log_anomalies(log_path, include_existing=True)

    assert len(incidents) == 1
    assert incidents[0].failure_kind == "traceback"


def test_public_helpers_return_persisted_path_and_prior_context(tmp_path):
    root = tmp_path / "public-api"
    recorded = record_incident(
        storage_root=root,
        source="public",
        task_key="probe",
        task_name="Probe",
        failure_kind="unexpected_result",
        message="expected result did not happen",
        expected=True,
        observed=False,
    )

    assert recorded.persisted_path is not None
    assert recorded.persisted_path.is_file()
    assert load_incident(recorded.persisted_path, storage_root=root).id == recorded.id
    record_repair_outcome(
        recorded,
        "success",
        storage_root=root,
        summary="verified by focused test",
    )
    context = prior_context(recorded.fingerprint, storage_root=root)
    assert context["occurrence_count"] == 1
    assert context["repair_outcomes"] == {"success": 1}
