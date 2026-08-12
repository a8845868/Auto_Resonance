from __future__ import annotations

import ctypes
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import core.control.control as control_module
from auto.fatigue_recovery import _capture_blocked_fatigue_result
from core.control.nemu import NEMU, _make_capture_buffer
from core.control.nemu_capture import (
    CaptureSessionRecoveryResult,
    NemuCaptureError,
)
from core.services.city_navigation import (
    CityNavigationAdapter,
    KnownNavigationBlock,
)
from core.services.runtime_fault_telemetry import (
    RuntimeFaultEvent,
    record_runtime_fault,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _item(text: str, x: int, y: int) -> dict:
    return {
        "text": text,
        "position": [
            [x - 30, y - 12],
            [x + 30, y - 12],
            [x + 30, y + 12],
            [x - 30, y + 12],
        ],
    }


class _Frame:
    def __init__(self, *texts: str, pixel: int, capture_id: str):
        self.image = np.full((720, 1280, 3), pixel, dtype=np.uint8)
        self.raw_frame_hash = f"{pixel + 1:064x}"
        self.source_capture_id = capture_id
        self.captured_at = NOW
        self._items = [
            _item(text, 1100 if "访问城市" in text else 300 + index * 100,
                  480 if "访问城市" in text else 220)
            for index, text in enumerate(texts)
        ]
        if any("访问城市" in text for text in texts):
            self.image[440:443, 1000:1201] = 255
            self.image[518:521, 1000:1201] = 255
            self.image[440:521, 1000:1003] = 255
            self.image[440:521, 1198:1201] = 255

    def ocr(self):
        return list(self._items)


class _Clock:
    value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += float(seconds)


def _home(index: int):
    return _Frame(
        "访问城市", "作战终端", "启程",
        pixel=index,
        capture_id=f"home-{index}",
    )


def _city(index: int):
    return _Frame(
        "当前城市", "城市设施", "城市手册",
        pixel=index,
        capture_id=f"city-{index}",
    )


def _failure(stage="RUNTIME_CAPTURE", *, conflict="NO", code=2):
    return NemuCaptureError(
        native_return_code=code,
        instance_index=0,
        instance_handle=11,
        display_id=3,
        session_generation=7,
        connect_epoch="2026-08-07T12:00:00+00:00",
        capture_call_index=2,
        last_successful_capture_call_index=1,
        capture_width=1280,
        capture_height=720,
        thread_id=123,
        failure_stage=stage,
        session_lifecycle_conflict=conflict,
        health_capture_return_code=0,
        health_capture_session_generation=7,
        business_capture_return_code=code,
        business_capture_session_generation=7,
    )


def _adapter(sequence, *, recoverer=None, taps=None, telemetry=None):
    iterator = iter(sequence)
    clock = _Clock()

    def provide():
        item = next(iterator)
        if isinstance(item, BaseException):
            raise item
        return item

    return CityNavigationAdapter(
        frame_provider=provide,
        tap=lambda *_args, **_kwargs: (taps.append(True) if taps is not None else None) or True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: NOW,
        timeout=10,
        max_attempts=8,
        correlation_id="capture-fault-test",
        evidence_recorder=lambda _evidence: True,
        session_recoverer=recoverer,
        runtime_fault_recorder=(telemetry.append if telemetry is not None else None),
    )


def test_initial_capture_failure_blocks_before_dispatch():
    taps = []
    result = _adapter([_failure()], taps=taps).enter_city()

    assert result.status == "BLOCKED_SAFETY"
    assert result.reason == "initial_capture_failed"
    assert result.capture_failure_stage == "INITIAL_CAPTURE"
    assert result.dispatch_count == result.physical_input_count == 0
    assert result.session_recovery_count == 0
    assert taps == []


def test_initial_capture_failure_recovers_with_a_new_session_generation():
    taps = []
    result = _adapter(
        [_failure(), _home(2), _home(3), _city(4)],
        recoverer=lambda: CaptureSessionRecoveryResult(
            True, "replacement_session_ready", 7, 8, "NO"
        ),
        taps=taps,
    ).enter_city()

    assert result.status == "PASS"
    assert result.session_recovery_count == 1
    assert result.session_generation_before_recovery == 7
    assert result.session_generation_after_recovery == 8
    assert result.same_action_retry == 0
    assert len(taps) == 1


def test_fresh_capture_failure_without_recoverer_blocks_before_dispatch():
    taps = []
    result = _adapter([_home(1), _failure()], taps=taps).enter_city()

    assert result.status == "BLOCKED_SAFETY"
    assert result.reason == "fresh_capture_failed"
    assert result.capture_failure_stage == "FRESH_CONFIRMATION_CAPTURE"
    assert result.dispatch_count == result.physical_input_count == 0
    assert taps == []


def test_fresh_capture_recovery_restarts_the_full_two_frame_precondition():
    taps = []
    recoveries = []

    def recover():
        recoveries.append(True)
        return CaptureSessionRecoveryResult(
            True, "replacement_session_ready", 7, 8, "NO"
        )

    result = _adapter(
        [_home(1), _failure(), _home(3), _home(4), _city(5)],
        recoverer=recover,
        taps=taps,
    ).enter_city()

    assert result.status == "PASS"
    assert result.session_recovery_count == 1
    assert result.session_generation_before_recovery == 7
    assert result.session_generation_after_recovery == 8
    assert result.dispatch_count == result.physical_input_count == 1
    assert result.same_action_retry == 0
    assert len(taps) == 1
    assert recoveries == [True]


def test_second_predispatch_failure_exhausts_only_the_session_recovery():
    taps = []
    result = _adapter(
        [_failure(), _failure()],
        recoverer=lambda: CaptureSessionRecoveryResult(
            True, "replacement_session_ready", 7, 8, "NO"
        ),
        taps=taps,
    ).enter_city()

    assert result.status == "BLOCKED_SAFETY"
    assert result.session_recovery_count == 1
    assert result.retry_exhausted is True
    assert result.dispatch_count == 0
    assert taps == []


def test_lifecycle_conflict_forbids_session_recovery():
    recoveries = []
    result = _adapter(
        [_failure(conflict="YES")],
        recoverer=lambda: recoveries.append(True),
    ).enter_city()

    assert result.status == "BLOCKED_SAFETY"
    assert result.session_lifecycle_conflict == "YES"
    assert result.session_recovery_count == 0
    assert recoveries == []


def test_post_dispatch_capture_failure_is_unobservable_and_never_retries():
    taps = []
    result = _adapter(
        [_home(1), _home(2), _failure()],
        recoverer=lambda: pytest.fail("post-dispatch recovery is forbidden"),
        taps=taps,
    ).enter_city()

    assert result.status == "POSTCONDITION_UNOBSERVABLE"
    assert result.reason == "post_dispatch_capture_failed"
    assert result.capture_failure_stage == "POST_DISPATCH_CAPTURE"
    assert result.dispatch_count == result.physical_input_count == 1
    assert result.postcondition_observation_failed is True
    assert result.same_action_retry == 0
    assert len(taps) == 1


def test_unexpected_runtime_error_remains_fatal_to_the_caller():
    with pytest.raises(RuntimeError, match="programming defect"):
        _adapter([RuntimeError("programming defect")]).enter_city()


def test_nemu_rejection_raises_typed_error_and_never_creates_a_frame():
    backend = object.__new__(NEMU)
    backend.nemu = SimpleNamespace(
        nemu_capture_display=lambda *_args: 2,
    )
    backend.connect_id = 11
    backend.display_id = 3
    backend.device = SimpleNamespace(index=0)
    backend.width = 4
    backend.height = 2
    backend.width_ptr = ctypes.pointer(ctypes.c_int(4))
    backend.height_ptr = ctypes.pointer(ctypes.c_int(2))
    backend.pixels_array, backend.pixels_pointer, backend.length = (
        _make_capture_buffer(4, 2, 4)
    )
    backend.session_generation = 7
    backend.connect_epoch = "epoch"
    backend.health_capture_return_code = 0
    backend.health_capture_session_generation = 7
    backend._health_capture_instance_handle = 11
    backend._health_capture_display_id = 3
    backend._health_capture_disconnect_call_count = 0
    backend.disconnect_call_count = 0

    with pytest.raises(NemuCaptureError) as caught:
        backend.screenshot(failure_stage="INITIAL_CAPTURE")

    error = caught.value
    assert error.native_return_code == 2
    assert error.failure_stage == "INITIAL_CAPTURE"
    assert error.capture_native_status == "FAILED"
    assert error.frame_created is False
    assert error.frame_sha256 is None
    assert error.session_lifecycle_conflict == "NO"


def test_nemu_lifecycle_change_after_health_capture_is_reported_as_conflict():
    backend = object.__new__(NEMU)
    backend.connect_id = 12
    backend.display_id = 3
    backend.session_generation = 7
    backend.health_capture_return_code = 0
    backend.health_capture_session_generation = 7
    backend._health_capture_instance_handle = 11
    backend._health_capture_display_id = 3
    backend._health_capture_disconnect_call_count = 0
    backend.disconnect_call_count = 1

    assert backend._capture_lifecycle_conflict() == "YES"


def test_session_recovery_closes_failed_session_once_and_replaces_generation(
    monkeypatch,
):
    disconnects = []
    previous = object.__new__(NEMU)
    previous.connect_id = 11
    previous.display_id = 3
    previous.session_generation = 7
    previous.health_capture_return_code = 0
    previous.health_capture_session_generation = 7
    previous._health_capture_instance_handle = 11
    previous._health_capture_display_id = 3
    previous._health_capture_disconnect_call_count = 0
    previous.disconnect_call_count = 0
    previous.kill_call_count = 0
    previous._health_adb = None
    previous.nemu = SimpleNamespace(
        nemu_disconnect=lambda handle: disconnects.append(handle)
    )
    device = SimpleNamespace(
        port=16384,
        index=0,
        type="MUMUV5",
        path=r"C:\Program Files\NetEase\MuMu",
    )
    replacement = SimpleNamespace(
        device=device,
        path=r"C:\Program Files\NetEase\MuMu",
        session_generation=8,
        health_capture_return_code=0,
        health_capture_session_generation=8,
        session_quarantined=False,
        connect_id=12,
        display_id=0,
        connect=lambda _port: True,
        kill=lambda: None,
    )
    monkeypatch.setattr(control_module, "control", previous)
    monkeypatch.setattr(control_module, "get_runtime_device", lambda: device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: replacement)
    monkeypatch.setattr(
        control_module,
        "resolve_mumu_launcher",
        lambda _device: SimpleNamespace(
            install_root=r"C:\Program Files\NetEase\MuMu"
        ),
    )
    monkeypatch.setattr(
        control_module,
        "connect",
        lambda *_args, **_kwargs: pytest.fail(
            "capture recovery must not call general connect()"
        ),
    )
    monkeypatch.setattr(
        control_module,
        "ADB",
        lambda: pytest.fail("capture recovery must not construct ADB"),
    )
    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is True
    assert recovered.previous_session_generation == 7
    assert recovered.current_session_generation == 8
    assert control_module.control is replacement
    assert disconnects == [11]
    assert previous.kill_call_count == 1
    assert previous.disconnect_call_count == 1


def _recovery_previous_backend():
    previous = object.__new__(NEMU)
    previous.connect_id = 11
    previous.display_id = 3
    previous.session_generation = 7
    previous.health_capture_return_code = 0
    previous.health_capture_session_generation = 7
    previous._health_capture_instance_handle = 11
    previous._health_capture_display_id = 3
    previous._health_capture_disconnect_call_count = 0
    previous.disconnect_call_count = 0
    previous.kill_call_count = 0
    previous._health_adb = None
    previous.nemu = SimpleNamespace(nemu_disconnect=lambda _handle: None)
    return previous


def _recovery_device(*, index=0):
    return SimpleNamespace(
        port=16384,
        index=index,
        type="MUMUV5",
        path=r"C:\Program Files\NetEase\MuMu",
    )


def _healthy_recovery_candidate(device, *, generation=8):
    kills = []
    candidate = SimpleNamespace(
        device=device,
        path=r"C:\Program Files\NetEase\MuMu",
        session_generation=generation,
        health_capture_return_code=0,
        health_capture_session_generation=generation,
        session_quarantined=False,
        connect_id=12,
        display_id=0,
        connect=lambda _port: True,
        kill=lambda: kills.append("kill"),
    )
    candidate.kills = kills
    return candidate


def _patch_recovery_environment(monkeypatch, previous, device):
    monkeypatch.setattr(control_module, "control", previous)
    monkeypatch.setattr(control_module, "get_runtime_device", lambda: device)
    monkeypatch.setattr(
        control_module,
        "resolve_mumu_launcher",
        lambda _device: SimpleNamespace(
            install_root=r"C:\Program Files\NetEase\MuMu"
        ),
    )
    monkeypatch.setattr(
        control_module,
        "connect",
        lambda *_args, **_kwargs: pytest.fail(
            "capture recovery must not call general connect()"
        ),
    )
    monkeypatch.setattr(
        control_module,
        "ADB",
        lambda: pytest.fail("capture recovery must not construct ADB"),
    )


def test_session_recovery_unavailable_nemu_never_publishes_adb(monkeypatch):
    previous = _recovery_previous_backend()
    device = _recovery_device()
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(
        control_module,
        "NEMU",
        lambda _device: (_ for _ in ()).throw(FileNotFoundError("missing DLL")),
    )

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_nemu_unavailable"
    assert control_module.control is previous
    assert previous.connect_id is None


def test_session_recovery_connect_false_keeps_closed_nemu_backend(monkeypatch):
    previous = _recovery_previous_backend()
    device = _recovery_device()
    candidate = _healthy_recovery_candidate(device)
    candidate.connect = lambda _port: False
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: candidate)

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_session_connect_rejected"
    assert control_module.control is previous
    assert candidate.kills == ["kill"]


def test_session_recovery_capture_failure_never_publishes_candidate(monkeypatch):
    previous = _recovery_previous_backend()
    device = _recovery_device()
    candidate = _healthy_recovery_candidate(device)
    failure = NemuCaptureError(
        native_return_code=2,
        session_generation=8,
        failure_stage="CONNECT_HEALTH_CAPTURE",
    )

    def fail_capture(_port):
        raise failure

    candidate.connect = fail_capture
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: candidate)

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_session_capture_failed"
    assert recovered.capture_failure is failure
    assert control_module.control is previous
    assert candidate.kills == ["kill"]


def test_session_recovery_target_mismatch_is_closed_without_publication(monkeypatch):
    previous = _recovery_previous_backend()
    device = _recovery_device(index=0)
    candidate = _healthy_recovery_candidate(_recovery_device(index=6))
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: candidate)

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_session_target_mismatch"
    assert control_module.control is previous
    assert candidate.kills == ["kill"]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("session_generation", "invalid"),
        ("health_capture_return_code", 2),
        ("health_capture_session_generation", 7),
        ("session_quarantined", True),
        ("connect_id", None),
        ("display_id", None),
    ],
)
def test_session_recovery_unproven_health_is_closed_without_publication(
    monkeypatch, attribute, value
):
    previous = _recovery_previous_backend()
    device = _recovery_device()
    candidate = _healthy_recovery_candidate(device)
    setattr(candidate, attribute, value)
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: candidate)

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_session_health_unproven"
    assert control_module.control is previous
    assert candidate.kills == ["kill"]


def test_session_recovery_target_resolution_error_closes_candidate(monkeypatch):
    previous = _recovery_previous_backend()
    device = _recovery_device()
    candidate = _healthy_recovery_candidate(device)
    _patch_recovery_environment(monkeypatch, previous, device)
    monkeypatch.setattr(control_module, "NEMU", lambda _device: candidate)
    monkeypatch.setattr(
        control_module,
        "resolve_mumu_launcher",
        lambda _device: (_ for _ in ()).throw(RuntimeError("resolver failed")),
    )

    recovered = control_module.recover_nemu_capture_session()

    assert recovered.success is False
    assert recovered.reason == "replacement_session_target_mismatch"
    assert control_module.control is previous
    assert candidate.kills == ["kill"]


def test_concurrent_connect_waits_for_atomic_nemu_recovery(monkeypatch):
    device = SimpleNamespace(
        is_mumu=True,
        port=16384,
        index=0,
        type="MUMUV5",
        path=r"C:\Program Files\NetEase\MuMu",
    )
    recovery_connect_entered = threading.Event()
    concurrent_connect_started = threading.Event()
    release_recovery = threading.Event()
    created = []

    class RecoveryBackend:
        def __init__(self, target, generation, *, connected=True):
            self.device = target
            self.path = r"C:\Program Files\NetEase\MuMu"
            self.session_generation = generation
            self.health_capture_return_code = 0
            self.health_capture_session_generation = generation
            self.session_quarantined = False
            self.connect_id = 11 if connected else None
            self.display_id = 0
            self.kill_call_count = 0

        def _capture_lifecycle_conflict(self):
            return "NO"

        def connect(self, _port):
            recovery_connect_entered.set()
            assert concurrent_connect_started.wait(timeout=2)
            assert release_recovery.wait(timeout=2)
            self.connect_id = 12
            return True

        def kill(self):
            if self.connect_id is None:
                return
            self.kill_call_count += 1
            self.connect_id = None

    previous = RecoveryBackend(device, 7)

    def create_nemu(target):
        candidate = RecoveryBackend(target, 8, connected=False)
        created.append(candidate)
        return candidate

    monkeypatch.setattr(control_module, "control", previous)
    monkeypatch.setattr(control_module, "get_runtime_device", lambda: device)
    monkeypatch.setattr(control_module, "NEMU", create_nemu)
    monkeypatch.setattr(control_module, "_NEMU_BACKEND_TYPE", RecoveryBackend)
    monkeypatch.setattr(
        control_module,
        "resolve_mumu_launcher",
        lambda _device: SimpleNamespace(
            install_root=r"C:\Program Files\NetEase\MuMu"
        ),
    )
    monkeypatch.setattr(
        control_module,
        "ADB",
        lambda: pytest.fail("concurrent recovery must never construct ADB"),
    )
    recovery_results = []
    connect_results = []

    recovery_thread = threading.Thread(
        target=lambda: recovery_results.append(
            control_module.recover_nemu_capture_session()
        )
    )

    def concurrent_connect():
        concurrent_connect_started.set()
        connect_results.append(control_module.connect(16384))

    connect_thread = threading.Thread(target=concurrent_connect)
    recovery_thread.start()
    assert recovery_connect_entered.wait(timeout=2)
    connect_thread.start()
    assert concurrent_connect_started.wait(timeout=2)
    release_recovery.set()
    recovery_thread.join(timeout=2)
    connect_thread.join(timeout=2)

    assert not recovery_thread.is_alive()
    assert not connect_thread.is_alive()
    assert [result.success for result in recovery_results] == [True]
    assert connect_results == [True]
    assert len(created) == 1
    assert control_module.control is created[0]
    assert previous.kill_call_count == 1


def test_runtime_fault_telemetry_contains_only_allowlisted_metadata(tmp_path):
    path = tmp_path / "runtime_faults.jsonl"
    event = RuntimeFaultEvent(
        task_id="fatigue",
        attempt_id="attempt",
        backend="NEMU",
        session_generation=7,
        native_return_code=2,
        failure_stage="INITIAL_CAPTURE",
        recovery_attempted=True,
        recovery_result="replacement_session_ready",
    )
    assert record_runtime_fault(event, path=path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert "frame" not in serialized.lower()
    assert "ocr" not in serialized.lower()
    assert "account" not in serialized.lower()
    assert payload["native_return_code"] == 2


def test_fatigue_known_navigation_block_is_safe_and_has_zero_business_actions():
    navigation = SimpleNamespace(
        reason="initial_capture_failed",
        capture_failure=_failure("INITIAL_CAPTURE"),
        capture_failure_stage="INITIAL_CAPTURE",
        session_recovery_count=1,
        retry_exhausted=True,
        runtime_fault_recorded=True,
        physical_input_count=0,
    )
    result = _capture_blocked_fatigue_result(KnownNavigationBlock(navigation))

    assert result["task_outcome"] == "BLOCKED_SAFETY"
    assert result["queue_retry_count"] == 0
    assert result["incident_eligible"] is False
    assert result["halt_eligible"] is False
    assert result["physical_input_count"] == 0
    assert result["buy_actions"] == 0
    assert result["sell_actions"] == 0
    assert result["depart_actions"] == 0
    assert result["fatigue_consume_actions"] == 0
