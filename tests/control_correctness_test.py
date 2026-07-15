import ctypes
from types import SimpleNamespace

import cv2 as cv
import numpy as np
import pytest

import core.control.control as control_module
from core.control.adb import ADB
from core.control.nemu import NEMU
from core.exception.exceptions import StopExecution
from core.image.utils import match_template


class FakeBackend:
    def __init__(self, name, events, result=True, error=None, kill_error=None):
        self.name = name
        self.events = events
        self.result = result
        self.error = error
        self.kill_error = kill_error

    def connect(self, _port=None):
        self.events.append(f"{self.name}.connect")
        if self.error is not None:
            raise self.error
        return self.result

    def kill(self):
        self.events.append(f"{self.name}.kill")
        if self.kill_error is not None:
            raise self.kill_error


@pytest.fixture(autouse=True)
def restore_control_state():
    previous_control = control_module.control
    previous_stop = control_module.STOP
    yield
    control_module.control = previous_control
    control_module.STOP = previous_stop


def _patch_mumu_backends(monkeypatch, nemu, adb, events):
    monkeypatch.setattr(
        control_module.app.Global, "device", SimpleNamespace(is_mumu=True)
    )

    def create_nemu(_device=None):
        events.append("nemu.init")
        if isinstance(nemu, Exception):
            raise nemu
        return nemu

    def create_adb():
        events.append("adb.init")
        return adb

    monkeypatch.setattr(control_module, "NEMU", create_nemu)
    monkeypatch.setattr(control_module, "ADB", create_adb)


def test_nemu_constructor_exception_falls_back_to_adb(monkeypatch):
    events = []
    adb = FakeBackend("adb", events)
    _patch_mumu_backends(monkeypatch, RuntimeError("DLL load failed"), adb, events)

    assert control_module.connect(16384) is True
    assert control_module.control is adb
    assert events == ["nemu.init", "adb.init", "adb.connect"]


def test_nemu_connect_exception_closes_before_adb(monkeypatch):
    events = []
    nemu = FakeBackend("nemu", events, error=RuntimeError("IPC failed"))
    adb = FakeBackend("adb", events)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    assert control_module.connect(16384) is True
    assert control_module.control is adb
    assert events == [
        "nemu.init",
        "nemu.connect",
        "nemu.kill",
        "adb.init",
        "adb.connect",
    ]


def test_nemu_false_result_closes_and_falls_back_to_adb(monkeypatch):
    events = []
    nemu = FakeBackend("nemu", events, result=False)
    adb = FakeBackend("adb", events)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    assert control_module.connect(16384) is True
    assert control_module.control is adb
    assert events == [
        "nemu.init",
        "nemu.connect",
        "nemu.kill",
        "adb.init",
        "adb.connect",
    ]


def test_nemu_success_does_not_create_adb(monkeypatch):
    events = []
    nemu = FakeBackend("nemu", events)
    adb = FakeBackend("adb", events)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    assert control_module.connect(16384) is True
    assert control_module.control is nemu
    assert events == ["nemu.init", "nemu.connect"]


def test_failed_adb_candidate_does_not_replace_current_backend(monkeypatch):
    events = []
    previous = FakeBackend("previous", events)
    adb = FakeBackend("adb", events, result=False)
    monkeypatch.setattr(
        control_module.app.Global, "device", SimpleNamespace(is_mumu=False)
    )
    monkeypatch.setattr(control_module, "control", previous)

    def create_adb():
        events.append("adb.init")
        return adb

    monkeypatch.setattr(control_module, "ADB", create_adb)

    assert control_module.connect(16384) is False
    assert control_module.control is previous
    assert events == ["adb.init", "adb.connect", "adb.kill"]


def test_nemu_and_adb_false_keep_previous_backend(monkeypatch):
    events = []
    previous = FakeBackend("previous", events)
    nemu = FakeBackend("nemu", events, result=False)
    adb = FakeBackend("adb", events, result=False)
    monkeypatch.setattr(control_module, "control", previous)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    assert control_module.connect(16384) is False
    assert control_module.control is previous
    assert events == [
        "nemu.init",
        "nemu.connect",
        "nemu.kill",
        "adb.init",
        "adb.connect",
        "adb.kill",
    ]


def test_nemu_failure_then_adb_exception_keeps_previous_backend(monkeypatch):
    events = []
    previous = FakeBackend("previous", events)
    nemu = FakeBackend("nemu", events, result=False)
    adb = FakeBackend("adb", events, error=RuntimeError("ADB failed"))
    monkeypatch.setattr(control_module, "control", previous)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    with pytest.raises(RuntimeError, match="ADB failed"):
        control_module.connect(16384)

    assert control_module.control is previous
    assert events == [
        "nemu.init",
        "nemu.connect",
        "nemu.kill",
        "adb.init",
        "adb.connect",
        "adb.kill",
    ]


def test_nemu_disconnect_exception_does_not_block_adb_fallback(monkeypatch):
    events = []
    nemu = FakeBackend(
        "nemu", events, result=False, kill_error=RuntimeError("disconnect failed")
    )
    adb = FakeBackend("adb", events)
    _patch_mumu_backends(monkeypatch, nemu, adb, events)

    assert control_module.connect(16384) is True
    assert control_module.control is adb
    assert events == [
        "nemu.init",
        "nemu.connect",
        "nemu.kill",
        "adb.init",
        "adb.connect",
    ]


def test_successful_reconnect_closes_previous_backend(monkeypatch):
    events = []
    previous = FakeBackend("previous", events)
    adb = FakeBackend("adb", events)
    monkeypatch.setattr(
        control_module.app.Global, "device", SimpleNamespace(is_mumu=False)
    )
    monkeypatch.setattr(control_module, "control", previous)
    monkeypatch.setattr(
        control_module,
        "ADB",
        lambda: events.append("adb.init") or adb,
    )

    assert control_module.connect(16384) is True
    assert control_module.control is adb
    assert events == ["adb.init", "adb.connect", "previous.kill"]


class ScreenshotFailureDLL:
    def __init__(self):
        self.capture_calls = 0
        self.disconnected = []

    def nemu_connect(self, _path, _index):
        return 42

    def nemu_get_display_id(self, _connect_id, _package, _app_index):
        return 7

    def nemu_capture_display(
        self, _connect_id, _display_id, _length, width_ptr, height_ptr, _pixels
    ):
        self.capture_calls += 1
        if self.capture_calls == 1:
            width_ptr.contents.value = 16
            height_ptr.contents.value = 9
            return 0
        raise RuntimeError("capture failed")

    def nemu_disconnect(self, connect_id):
        self.disconnected.append(connect_id)


class ScreenshotValueDLL(ScreenshotFailureDLL):
    def nemu_capture_display(
        self, _connect_id, _display_id, _length, width_ptr, height_ptr, _pixels
    ):
        width_ptr.contents.value = 16
        height_ptr.contents.value = 9
        return 0


def test_nemu_connect_validates_screenshot_and_disconnects_on_failure():
    dll = ScreenshotFailureDLL()
    nemu = NEMU.__new__(NEMU)
    nemu.device = SimpleNamespace(index=0)
    nemu.path = "C:/MuMu"
    nemu.nemu = dll

    with pytest.raises(RuntimeError, match="capture failed"):
        nemu.connect()

    assert dll.disconnected == [42]


@pytest.mark.parametrize(
    "invalid_screenshot",
    [
        None,
        np.zeros((0, 0, 3), dtype=np.uint8),
        np.zeros((9, 16), dtype=np.uint8),
        np.zeros((9, 16, 4), dtype=np.uint8),
        np.zeros((8, 16, 3), dtype=np.uint8),
    ],
    ids=["none", "empty", "two-dimensional", "four-channel", "wrong-size"],
)
def test_nemu_connect_rejects_invalid_screenshot(invalid_screenshot):
    dll = ScreenshotValueDLL()
    nemu = NEMU.__new__(NEMU)
    nemu.device = SimpleNamespace(index=0)
    nemu.path = "C:/MuMu"
    nemu.nemu = dll
    nemu.screenshot = lambda: invalid_screenshot

    with pytest.raises(RuntimeError, match="截图无效"):
        nemu.connect()

    assert dll.disconnected == [42]


def test_nemu_kill_is_idempotent_after_disconnect_exception():
    class FailingDisconnectDLL:
        def __init__(self):
            self.calls = []

        def nemu_disconnect(self, connect_id):
            self.calls.append(connect_id)
            raise RuntimeError("disconnect failed")

    dll = FailingDisconnectDLL()
    nemu = NEMU.__new__(NEMU)
    nemu.nemu = dll
    nemu.connect_id = 42

    with pytest.raises(RuntimeError, match="disconnect failed"):
        nemu.kill()
    nemu.kill()

    assert nemu.connect_id is None
    assert dll.calls == [42]


def test_adb_and_nemu_screenshots_share_bgr_pixel_contract():
    expected_bgr = np.array([10, 20, 30], dtype=np.uint8)
    source = expected_bgr.reshape(1, 1, 3)
    encoded, png = cv.imencode(".png", source)
    assert encoded

    adb = ADB()
    adb.device = SimpleNamespace(shell=lambda *_args, **_kwargs: png.tobytes())

    nemu = NEMU.__new__(NEMU)
    nemu.connect_id = 1
    nemu.display_id = 2
    nemu.width = 1
    nemu.height = 1
    nemu.length = 4
    nemu.width_ptr = ctypes.pointer(ctypes.c_int(1))
    nemu.height_ptr = ctypes.pointer(ctypes.c_int(1))
    nemu.pixels_array = (ctypes.c_ubyte * 4)(10, 20, 30, 255)
    nemu.pixels_pointer = ctypes.pointer(nemu.pixels_array)
    nemu.nemu = SimpleNamespace(nemu_capture_display=lambda *_args: 0)

    adb_image = adb.screenshot()
    nemu_image = nemu.screenshot()
    np.testing.assert_array_equal(adb_image[0, 0], expected_bgr)
    np.testing.assert_array_equal(nemu_image[0, 0], expected_bgr)

    assert match_template(adb_image, source, threshold=0.999)
    assert match_template(nemu_image, source, threshold=0.999)


class FakeSwipeControl:
    def __init__(self, safe_area, ratio=1, max_calls=5, on_swipe=None):
        self.safe_area = safe_area
        self.ratio = ratio
        self.max_calls = max_calls
        self.on_swipe = on_swipe
        self.swipes = []

    def input_swipe(self, x1, y1, x2, y2, duration):
        self.swipes.append((x1, y1, x2, y2, duration))
        if self.on_swipe is not None:
            self.on_swipe(len(self.swipes))
        if self.max_calls is not None and len(self.swipes) > self.max_calls:
            raise AssertionError("swipe segmentation did not make progress")


def _patch_swipe_runtime(monkeypatch, fake_control):
    monkeypatch.setattr(control_module, "control", fake_control)
    monkeypatch.setattr(control_module.random, "randint", lambda *_args: 0)
    monkeypatch.setattr(control_module.time, "sleep", lambda _seconds: None)
    control_module.reset_stop()


def test_swipe_beyond_safe_area_finishes_with_accumulated_displacement(monkeypatch):
    fake = FakeSwipeControl((0, 0, 200, 700))
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe((100, 100), (100, 900), swipe_time=120)

    assert len(fake.swipes) == 2
    assert sum(swipe[2] - swipe[0] for swipe in fake.swipes) == 0
    assert sum(swipe[3] - swipe[1] for swipe in fake.swipes) == 800


def test_swipe_raises_when_safe_area_cannot_make_progress(monkeypatch):
    fake = FakeSwipeControl((100, 100, 100, 100))
    _patch_swipe_runtime(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="无法推进"):
        control_module.input_swipe((100, 100), (100, 200))

    assert fake.swipes == []


def test_swipe_inside_safe_area_remains_one_gesture(monkeypatch):
    fake = FakeSwipeControl((0, 0, 1280, 720))
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe((100, 100), (300, 250), swipe_time=80)

    assert fake.swipes == [(100, 100, 300, 250, 80)]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ((20, 100), (180, 100), [(20, 100, 180, 100, 90)]),
        ((100, 20), (100, 180), [(100, 20, 100, 180, 90)]),
        (
            (100, 100),
            (300, 100),
            [(100, 100, 200, 100, 90), (0, 100, 100, 100, 90)],
        ),
        (
            (100, 100),
            (-100, 100),
            [(100, 100, 0, 100, 90), (200, 100, 100, 100, 90)],
        ),
        (
            (100, 100),
            (100, 300),
            [(100, 100, 100, 200, 90), (100, 0, 100, 100, 90)],
        ),
        (
            (100, 100),
            (100, -100),
            [(100, 100, 100, 0, 90), (100, 200, 100, 100, 90)],
        ),
        (
            (100, 100),
            (300, 300),
            [(100, 100, 200, 200, 90), (0, 0, 100, 100, 90)],
        ),
        ((200, 100), (300, 100), [(0, 100, 100, 100, 90)]),
        ((250, 100), (350, 100), [(0, 100, 100, 100, 90)]),
    ],
    ids=[
        "horizontal-inside",
        "vertical-inside",
        "right-overflow",
        "left-overflow",
        "bottom-overflow",
        "top-overflow",
        "diagonal-overflow",
        "boundary-start",
        "outside-start",
    ],
)
def test_swipe_emits_expected_coordinate_sequence(monkeypatch, start, end, expected):
    fake = FakeSwipeControl((0, 0, 200, 200))
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe(start, end, swipe_time=90)

    assert fake.swipes == expected
    target_dx = end[0] - start[0]
    target_dy = end[1] - start[1]
    assert sum(x2 - x1 for x1, _y1, x2, _y2, _ in fake.swipes) == target_dx
    assert sum(y2 - y1 for _x1, y1, _x2, y2, _ in fake.swipes) == target_dy
    assert all((x2 - x1) * target_dx >= 0 for x1, _y1, x2, _y2, _ in fake.swipes)
    assert all((y2 - y1) * target_dy >= 0 for _x1, y1, _x2, y2, _ in fake.swipes)
    assert all((x1, y1) != (x2, y2) for x1, y1, x2, y2, _ in fake.swipes)


def test_swipe_equal_points_emit_no_gesture(monkeypatch):
    fake = FakeSwipeControl((0, 0, 200, 200))
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe((100, 100), (100, 100))

    assert fake.swipes == []


@pytest.mark.parametrize(
    ("safe_area", "start", "end"),
    [
        ((100, 0, 100, 200), (100, 100), (200, 100)),
        ((0, 100, 200, 100), (100, 100), (100, 200)),
    ],
    ids=["zero-width", "zero-height"],
)
def test_swipe_rejects_safe_area_without_required_progress(
    monkeypatch, safe_area, start, end
):
    fake = FakeSwipeControl(safe_area)
    _patch_swipe_runtime(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="无法推进"):
        control_module.input_swipe(start, end)

    assert fake.swipes == []


def test_swipe_max_segments_has_bounded_coordinate_sequence(monkeypatch):
    fake = FakeSwipeControl((0, 0, 1, 100), max_calls=None)
    _patch_swipe_runtime(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="最大分段次数 100"):
        control_module.input_swipe((0, 50), (111, 50), swipe_time=70)

    assert fake.swipes == [(0, 50, 1, 50, 70)] * 100
    assert sum(x2 - x1 for x1, _y1, x2, _y2, _ in fake.swipes) == 100


def test_swipe_hundredth_segment_can_finish_within_tolerance(monkeypatch):
    fake = FakeSwipeControl((0, 0, 1, 100), max_calls=None)
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe((0, 50), (110, 50), swipe_time=70)

    assert fake.swipes == [(0, 50, 1, 50, 70)] * 100
    assert sum(x2 - x1 for x1, _y1, x2, _y2, _ in fake.swipes) == 100


def test_swipe_first_segment_reanchors_after_rounding_to_zero(monkeypatch):
    fake = FakeSwipeControl((30, 105, 1800, 1050), ratio=1.5)
    _patch_swipe_runtime(monkeypatch, fake)
    random_values = iter((-10, 0, 0, 0))
    monkeypatch.setattr(
        control_module.random, "randint", lambda *_args: next(random_values)
    )

    control_module.input_swipe((27, 100), (-100, 100))

    assert fake.swipes == [(1800, 150, 1620, 150, 100)]
    initial_x = fake.ratio * 27 - 10
    target_x = fake.ratio * -100
    actual_x = sum(x2 - x1 for x1, _y1, x2, _y2, _ in fake.swipes)
    assert abs((target_x - initial_x) - actual_x) <= 10
    assert all((x1, y1) != (x2, y2) for x1, y1, x2, y2, _ in fake.swipes)


def test_swipe_stop_between_segments_prevents_next_gesture(monkeypatch):
    fake = FakeSwipeControl((0, 0, 200, 200))
    _patch_swipe_runtime(monkeypatch, fake)
    sleep_calls = []

    def stop_during_sleep(seconds):
        sleep_calls.append(seconds)
        control_module.stop()

    monkeypatch.setattr(control_module.time, "sleep", stop_during_sleep)

    with pytest.raises(StopExecution):
        control_module.input_swipe((100, 100), (300, 100), swipe_time=90)

    assert sleep_calls == [0.5]
    assert fake.swipes == [(100, 100, 200, 100, 90)]


def test_swipe_scaled_rounding_has_bounded_residual(monkeypatch):
    fake = FakeSwipeControl((0, 0, 200, 200), ratio=0.125)
    _patch_swipe_runtime(monkeypatch, fake)

    control_module.input_swipe((0, 0), (100, 0), swipe_time=60)

    assert fake.swipes == [(0, 0, 12, 0, 60)]
    logical_dx = fake.ratio * 100
    actual_dx = sum(x2 - x1 for x1, _y1, x2, _y2, _ in fake.swipes)
    assert 0 <= logical_dx - actual_dx <= 10
