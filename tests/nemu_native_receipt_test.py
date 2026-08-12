import ctypes
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.control.nemu import NEMU, _make_capture_buffer
from core.control.adb_port import EmulatorInfo, EmulatorType
from core.control.nemu_dll.nemu_dll import (
    NemuNativeSignatureError,
    audit_native_signature,
    bind_exports,
)
from core.control.nemu_receipt import (
    NemuInputDispatchError,
    map_capture_to_nemu,
)


class _Function:
    def __init__(self, result=0, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class _SequenceFunction(_Function):
    def __init__(self, results):
        super().__init__()
        self.results = iter(results)

    def __call__(self, *args):
        self.calls.append(args)
        return next(self.results)


def _library(down=0, up=0, *, down_error=None, up_error=None):
    names = (
        "nemu_connect", "nemu_disconnect", "nemu_get_display_id",
        "nemu_capture_display", "nemu_input_text",
        "nemu_input_event_touch_down", "nemu_input_event_touch_up",
        "nemu_input_event_finger_touch_down", "nemu_input_event_finger_touch_up",
        "nemu_input_event_key_down", "nemu_input_event_key_up",
    )
    value = SimpleNamespace(**{name: _Function() for name in names})
    value.nemu_input_event_touch_down = _Function(down, down_error)
    value.nemu_input_event_touch_up = _Function(up, up_error)
    return value


def _backend(library):
    backend = object.__new__(NEMU)
    backend.nemu = library
    backend.connect_id = 11
    backend.display_id = 3
    backend.device = SimpleNamespace(index=0)
    backend.width = 1280
    backend.height = 720
    backend.session_generation = 7
    backend.session_quarantined = False
    backend.last_touch_receipt = None
    backend._health_adb = SimpleNamespace(
        shell=lambda command: (
            "1234" if command.startswith("pidof") else "mCurrentFocus com.hermes.goda/.Main"
        )
    )
    return backend


def test_every_used_ctypes_export_has_explicit_signature():
    library = bind_exports(_library())
    assert library.nemu_connect.argtypes == [ctypes.c_wchar_p, ctypes.c_int]
    assert library.nemu_connect.restype is ctypes.c_int
    assert library.nemu_disconnect.argtypes == [ctypes.c_int]
    assert library.nemu_disconnect.restype is None
    assert library.nemu_input_event_touch_down.argtypes == [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    assert library.nemu_input_event_touch_up.restype is ctypes.c_int


def test_nemu_resolves_install_root_from_root_nx_main_or_explicit_launcher(
    tmp_path, monkeypatch,
):
    root = tmp_path / "MuMu"
    nx_main = root / "nx_main"
    nx_main.mkdir(parents=True)
    manager = nx_main / "MuMuManager.exe"
    cli = nx_main / "mumu-cli.exe"
    manager.touch()
    cli.touch()
    loaded = []
    monkeypatch.setattr("core.control.nemu.init", lambda path: loaded.append(path) or object())

    for configured_path in (root, nx_main, manager, cli):
        device = EmulatorInfo(
            name="MuMu instance 0",
            port=16384,
            path=str(configured_path),
            type=EmulatorType.MUMUV5,
            index=0,
        )
        backend = NEMU(device)
        assert Path(backend.path) == root
        assert Path(loaded.pop()).resolve() == (
            root / "nx_device" / "12.0" / "shell" / "sdk" /
            "external_renderer_ipc.dll"
        ).resolve()


def test_capture_buffer_uses_real_byte_pointer_and_retains_writes():
    byte_length = 32
    buffer, pointer, actual_length = _make_capture_buffer(4, 2, 4)
    assert actual_length == byte_length == len(buffer)
    assert isinstance(pointer, ctypes.POINTER(ctypes.c_ubyte))
    assert ctypes.addressof(buffer) == ctypes.addressof(pointer.contents)

    capture_type = ctypes.CFUNCTYPE(
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ubyte),
    )

    @capture_type
    def capture(_instance, _display, length, _width, _height, pixels):
        pixels[0] = 0x11
        pixels[length // 2] = 0x22
        pixels[length - 1] = 0x33
        return 0

    width = ctypes.pointer(ctypes.c_int(4))
    height = ctypes.pointer(ctypes.c_int(2))
    assert capture(1, 0, byte_length, width, height, pointer) == 0
    assert (buffer[0], buffer[byte_length // 2], buffer[-1]) == (0x11, 0x22, 0x33)
    with pytest.raises(ctypes.ArgumentError):
        capture(1, 0, byte_length, width, height, ctypes.pointer(buffer))


@pytest.mark.parametrize(
    "width,height,channels,reason",
    (
        (True, 720, 4, "plain_integers"),
        (1280, 0, 4, "positive"),
        (1280, 720, 3, "unsupported"),
        (20000, 720, 4, "exceed_limit"),
    ),
)
def test_capture_buffer_rejects_invalid_dimensions(width, height, channels, reason):
    with pytest.raises(ValueError, match=reason):
        _make_capture_buffer(width, height, channels)


@pytest.mark.parametrize("native_result", (0, 5))
def test_screenshot_preserves_native_return_code_contract(native_result):
    backend = object.__new__(NEMU)
    backend.nemu = _library()
    backend.nemu.nemu_capture_display = _Function(native_result)
    backend.connect_id = 11
    backend.display_id = 3
    backend.width = 4
    backend.height = 2
    backend.width_ptr = ctypes.pointer(ctypes.c_int(4))
    backend.height_ptr = ctypes.pointer(ctypes.c_int(2))
    backend.pixels_array, backend.pixels_pointer, backend.length = (
        _make_capture_buffer(4, 2, 4)
    )
    backend.native_capture_call_count = 0
    backend.last_capture_return_code = None
    backend.last_capture_native_status = "NOT_CALLED"
    if native_result == 0:
        image = backend.screenshot()
        assert image.shape == (2, 4, 3)
        assert backend.last_capture_native_status == "ACCEPTED"
    else:
        with pytest.raises(RuntimeError, match="nemu_capture_failed:5"):
            backend.screenshot()
        assert backend.last_capture_native_status == "REJECTED"
    assert backend.last_capture_return_code == native_result
    assert backend.native_capture_call_count == 1


def test_untrusted_32_bit_calling_convention_is_blocked(tmp_path):
    path = tmp_path / "x86.dll"
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, 0x14C)
    struct.pack_into("<H", payload, 0x98, 0x10B)
    path.write_bytes(payload)
    with pytest.raises(NemuNativeSignatureError, match="untrusted_architecture"):
        audit_native_signature(path)


def test_touch_down_rejection_does_not_call_up():
    library = _library(down=5)
    backend = _backend(library)
    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_tap(100, 200)
    receipt = caught.value.receipt
    assert receipt.delivery_status == "REJECTED_BEFORE_DELIVERY"
    assert receipt.touch_down_status == "REJECTED"
    assert receipt.touch_up_called is False
    assert library.nemu_input_event_touch_up.calls == []
    assert backend.session_quarantined is False


def test_touch_up_rejection_is_partial_and_quarantines_without_retry():
    library = _library(down=0, up=9)
    backend = _backend(library)
    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_tap(100, 200)
    receipt = caught.value.receipt
    assert receipt.delivery_status == "UNKNOWN_AFTER_PARTIAL_DISPATCH"
    assert receipt.release_status == "UNKNOWN"
    assert len(library.nemu_input_event_touch_down.calls) == 1
    assert len(library.nemu_input_event_touch_up.calls) == 1
    assert backend.session_quarantined is True
    with pytest.raises(RuntimeError, match="session_quarantined"):
        backend.input_tap(100, 200)
    assert len(library.nemu_input_event_touch_down.calls) == 1


def test_python_exception_after_down_call_is_unknown_and_no_adb_fallback():
    library = _library(down_error=OSError("native fault"))
    backend = _backend(library)
    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_tap(100, 200)
    assert caught.value.receipt.delivery_status == "UNKNOWN_AFTER_EXCEPTION"
    assert caught.value.receipt.touch_up_called is False
    assert backend.session_quarantined is True


def test_down_and_up_accepted_produce_native_receipt(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    backend = _backend(_library())
    receipt = backend.input_tap(100, 200)
    assert receipt.delivery_status == "NATIVE_ACCEPTED"
    assert receipt.release_status == "CONFIRMED"
    assert receipt.python_call_returned is True
    assert receipt.capture_point == receipt.mapped_nemu_point == (100, 200)


def test_swipe_quarantined_session_makes_zero_native_calls():
    library = _library()
    backend = _backend(library)
    backend.session_quarantined = True

    with pytest.raises(RuntimeError, match="session_quarantined"):
        backend.input_swipe(100, 200, 300, 200, 30)

    assert library.nemu_input_event_touch_down.calls == []
    assert library.nemu_input_event_touch_up.calls == []


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    (
        ("connect_id", None, "session_not_connected"),
        ("session_generation", 0, "session_not_connected"),
        ("display_id", None, "display_id_invalid"),
    ),
)
def test_swipe_invalid_session_identity_makes_zero_native_calls(
    attribute, value, reason
):
    library = _library()
    backend = _backend(library)
    setattr(backend, attribute, value)

    with pytest.raises(RuntimeError, match=reason):
        backend.input_swipe(100, 200, 300, 200, 30)

    assert library.nemu_input_event_touch_down.calls == []
    assert library.nemu_input_event_touch_up.calls == []


def test_swipe_unhealthy_session_is_rejected_before_native_delivery():
    library = _library()
    backend = _backend(library)
    backend._health_adb = None

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    assert caught.value.receipt.delivery_status == "REJECTED_BEFORE_DELIVERY"
    assert library.nemu_input_event_touch_down.calls == []
    assert library.nemu_input_event_touch_up.calls == []


def test_swipe_rejected_first_down_stops_without_release_or_quarantine(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library(down=5)
    backend = _backend(library)

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    receipt = caught.value.receipt
    assert receipt.delivery_status == "REJECTED_BEFORE_DELIVERY"
    assert receipt.touch_up_called is False
    assert len(library.nemu_input_event_touch_down.calls) == 1
    assert library.nemu_input_event_touch_up.calls == []
    assert backend.session_quarantined is False


def test_swipe_rejected_later_down_stops_and_quarantines(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library()
    library.nemu_input_event_touch_down = _SequenceFunction((0, 7, 0))
    backend = _backend(library)

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    receipt = caught.value.receipt
    assert receipt.delivery_status == "UNKNOWN_AFTER_PARTIAL_DISPATCH"
    assert receipt.release_status == "CONFIRMED"
    assert receipt.touch_up_called is True
    assert receipt.touch_up_return_code == 0
    assert receipt.touch_up_status == "ACCEPTED"
    assert len(library.nemu_input_event_touch_down.calls) == 2
    assert len(library.nemu_input_event_touch_up.calls) == 1
    assert backend.session_quarantined is True


def test_swipe_unknown_down_stops_and_quarantines(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library(down=-2)
    backend = _backend(library)

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    receipt = caught.value.receipt
    assert receipt.delivery_status == "UNKNOWN_AFTER_EXCEPTION"
    assert receipt.release_status == "CONFIRMED"
    assert receipt.touch_up_called is True
    assert receipt.touch_up_status == "ACCEPTED"
    assert len(library.nemu_input_event_touch_down.calls) == 1
    assert len(library.nemu_input_event_touch_up.calls) == 1
    assert backend.session_quarantined is True


@pytest.mark.parametrize("up_code", (9, -2))
def test_swipe_nonaccepted_up_is_partial_and_quarantines(monkeypatch, up_code):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library(down=0, up=up_code)
    backend = _backend(library)

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    assert caught.value.receipt.delivery_status == "UNKNOWN_AFTER_PARTIAL_DISPATCH"
    assert caught.value.receipt.release_status == "UNKNOWN"
    assert backend.session_quarantined is True


def test_swipe_python_exception_best_effort_release_failure_is_recorded(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library(
        down_error=OSError("native swipe fault"),
        up_error=OSError("native release fault"),
    )
    backend = _backend(library)

    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_swipe(100, 200, 300, 200, 30)

    receipt = caught.value.receipt
    assert receipt.delivery_status == "UNKNOWN_AFTER_EXCEPTION"
    assert receipt.release_status == "UNKNOWN"
    assert receipt.touch_up_called is True
    assert receipt.touch_up_return_code is None
    assert receipt.touch_up_status == "UNKNOWN"
    assert "swipe_best_effort_touch_up_exception:OSError" in receipt.reason_codes
    assert backend.session_quarantined is True
    assert len(library.nemu_input_event_touch_up.calls) == 1


def test_swipe_all_native_calls_accepted_produce_receipt(monkeypatch):
    monkeypatch.setattr("core.control.nemu.time.sleep", lambda _seconds: None)
    library = _library()
    backend = _backend(library)

    receipt = backend.input_swipe(100, 200, 300, 200, 30)

    assert receipt.delivery_status == "NATIVE_ACCEPTED"
    assert receipt.release_status == "CONFIRMED"
    assert len(library.nemu_input_event_touch_down.calls) == 3
    assert len(library.nemu_input_event_touch_up.calls) == 1
    assert backend.session_quarantined is False


def test_unproven_process_and_foreground_block_before_native_delivery():
    library = _library()
    backend = _backend(library)
    backend._health_adb = None
    with pytest.raises(NemuInputDispatchError) as caught:
        backend.input_tap(100, 200)
    assert caught.value.receipt.delivery_status == "REJECTED_BEFORE_DELIVERY"
    assert "game_process_liveness_unproven" in caught.value.receipt.reason_codes
    assert library.nemu_input_event_touch_down.calls == []


def test_capture_to_display_transform_checks_bounds_rotation_and_scale():
    assert map_capture_to_nemu(
        (640, 360), capture_size=(1280, 720), display_size=(1920, 1080)
    ) == (960, 540)
    assert map_capture_to_nemu(
        (0, 0), capture_size=(1280, 720), display_size=(1280, 720), rotation=90
    ) == (719, 0)
    with pytest.raises(ValueError, match="out_of_bounds"):
        map_capture_to_nemu(
            (1280, 0), capture_size=(1280, 720), display_size=(1280, 720)
        )
    with pytest.raises(ValueError, match="rotation_unsupported"):
        map_capture_to_nemu(
            (0, 0), capture_size=(1280, 720), display_size=(1280, 720), rotation=45
        )
