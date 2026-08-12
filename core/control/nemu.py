
import ctypes
import os
import threading
import time
from datetime import datetime
from typing import Optional

from loguru import logger
import numpy as np
from core.control.adb_port import EmulatorInfo, EmulatorType, resolve_mumu_launcher
from core.control.base_control import IADB
import cv2 as cv
from adb_shell.adb_device import AdbDeviceTcp

from core.control.nemu_dll.nemu_dll import init
from core.control.nemu_receipt import (
    DeliveryStatus,
    NativeCallStatus,
    NemuInputDispatchError,
    NemuTouchReceipt,
    NemuTouchReceiptBuilder,
    ReleaseStatus,
    map_capture_to_nemu,
)
from core.control.nemu_capture import NemuCaptureError
from core.model import app
from core.services.repair_safety import ensure_automation_allowed


class IPCUnavailableError(OSError):
    """Known environment failure indicating that NEMU IPC cannot be loaded."""


NEMU_CAPTURE_CHANNEL_COUNT = 4
NEMU_MAX_CAPTURE_DIMENSION = 16384
NEMU_MAX_CAPTURE_BUFFER_BYTES = 512 * 1024 * 1024


def _make_capture_buffer(
    width: int,
    height: int,
    channel_count: int = NEMU_CAPTURE_CHANNEL_COUNT,
):
    """Allocate a strongly owned byte buffer with the exact audited pointer type."""

    dimensions = (width, height, channel_count)
    if any(type(value) is not int for value in dimensions):
        raise ValueError("nemu_capture_dimensions_must_be_plain_integers")
    if width <= 0 or height <= 0 or channel_count <= 0:
        raise ValueError("nemu_capture_dimensions_must_be_positive")
    if channel_count != NEMU_CAPTURE_CHANNEL_COUNT:
        raise ValueError(f"nemu_capture_channel_count_unsupported:{channel_count}")
    if width > NEMU_MAX_CAPTURE_DIMENSION or height > NEMU_MAX_CAPTURE_DIMENSION:
        raise ValueError("nemu_capture_dimensions_exceed_limit")
    byte_length = width * height * channel_count
    if byte_length <= 0 or byte_length > NEMU_MAX_CAPTURE_BUFFER_BYTES:
        raise ValueError("nemu_capture_buffer_size_invalid")
    buffer_type = ctypes.c_ubyte * byte_length
    buffer = buffer_type()
    if len(buffer) != byte_length:
        raise RuntimeError("nemu_capture_buffer_length_mismatch")
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    if ctypes.addressof(buffer) != ctypes.addressof(pointer.contents):
        raise RuntimeError("nemu_capture_buffer_address_mismatch")
    return buffer, pointer, byte_length

def swipe_path(p0, p3, time):
    path = []
    p0 = np.array(p0)
    p3 = np.array(p3)
    p1 = 2/3 * p0 + 1/3 * p3
    p2 = 1/3 * p0 + 2/3 * p3

    time = int(time / 10)
    for i in range(time):
        t = i / (time - 1)

        point = (1 - t)**3 * p0 + \
            3 * (1 - t)**2 * t * p1 + \
            3 * (1 - t) * t**2 * p2 + \
            t**3 * p3
        point = point.astype(int).tolist()
        path.append(point)

    return path

class NEMU(IADB):
    _generation_counter = 0

    def __init__(self, device: EmulatorInfo | None = None) -> None:
        ensure_automation_allowed("加载 NEMU 控制接口")
        self.connect_id = None
        self.device = device or app.Global.device
        if self.device.type not in {EmulatorType.MUMUV5, EmulatorType.MUMUV4}:
            raise Exception(f"不支持的模拟器类型: {self.device.type}")
        # GUI/runtime discovery may persist the installation root, nx_main/
        # shell launcher directory, or an explicit launcher.  NEMU IPC needs
        # the canonical installation root for both DLL resolution and
        # nemu_connect; never append nx_device to the launcher directory.
        launcher = resolve_mumu_launcher(self.device)
        self.path = str(launcher.install_root)
        if self.device.type == EmulatorType.MUMUV5:
            path = os.path.join(self.path, "./nx_device/12.0/shell/sdk/external_renderer_ipc.dll")
        else:
            path = os.path.join(self.path, "./shell/sdk/external_renderer_ipc.dll")
        self.nemu = init(path)
        self.session_generation = 0
        self.session_quarantined = False
        self.last_touch_receipt: NemuTouchReceipt | None = None
        self._health_adb = None
        self.connect_epoch = ""
        self.native_capture_call_count = 0
        self.last_successful_capture_call_index = 0
        self.health_capture_return_code = None
        self.health_capture_session_generation = None
        self.business_capture_return_code = None
        self.business_capture_session_generation = None
        self.kill_call_count = 0
        self.disconnect_call_count = 0
        self.last_kill_timestamp = ""
        self.last_disconnect_timestamp = ""
        self._health_capture_instance_handle = None
        self._health_capture_display_id = None
        self._health_capture_disconnect_call_count = 0

    def connect(self, adb_port: Optional[int] = None) -> bool:
        ensure_automation_allowed("建立 NEMU 连接")
        logger.info("使用NEMUIPC连接")
        self._health_adb = getattr(self, "_health_adb", None)
        self.session_quarantined = False
        self.last_touch_receipt = None
        self.connect_epoch = datetime.now().astimezone().isoformat(
            timespec="microseconds"
        )
        try:
            self.connect_id = self.nemu.nemu_connect(self.path, self.device.index)
            self.display_id = self.nemu.nemu_get_display_id(
                self.connect_id, b"com.hermes.goda", 0
            )
            if int(self.connect_id) <= 0:
                raise RuntimeError("nemu_connect_rejected")
            if int(self.display_id) < 0:
                raise RuntimeError("nemu_display_id_invalid")

            # 获取尺寸
            self.width_ptr = ctypes.pointer(ctypes.c_int(0))
            self.height_ptr = ctypes.pointer(ctypes.c_int(0))
            nullptr = ctypes.POINTER(ctypes.c_ubyte)()
            size_result = int(self.nemu.nemu_capture_display(
                self.connect_id,
                self.display_id,
                0,
                self.width_ptr,
                self.height_ptr,
                nullptr,
            ))
            if size_result != 0:
                raise RuntimeError(f"nemu_capture_size_query_failed:{size_result}")

            self.width = self.width_ptr.contents.value
            self.height = self.height_ptr.contents.value
            if self.width <= 0 or self.height <= 0:
                raise RuntimeError("nemu_capture_size_invalid")
            type(self)._generation_counter += 1
            self.session_generation = type(self)._generation_counter

            self.capture_channel_count = NEMU_CAPTURE_CHANNEL_COUNT
            (
                self.pixels_array,
                self.pixels_pointer,
                self.length,
            ) = _make_capture_buffer(
                self.width,
                self.height,
                self.capture_channel_count,
            )
            self.capture_buffer_array_type = type(self.pixels_array).__name__
            self.capture_buffer_pointer_type = "POINTER(c_ubyte)"
            self.capture_buffer_address = ctypes.addressof(self.pixels_array)
            self.capture_pointer_address = ctypes.addressof(
                self.pixels_pointer.contents
            )
            self.capture_buffer_address_match = (
                self.capture_buffer_address == self.capture_pointer_address
            )
            self.last_capture_return_code = None
            self.last_capture_native_status = "NOT_CALLED"
            self.native_capture_call_count = 0
            self.last_successful_capture_call_index = 0
            self.health_capture_return_code = None
            self.health_capture_session_generation = None
            self.business_capture_return_code = None
            self.business_capture_session_generation = None
            if not self.check_resolution_ratio(self.width, self.height):
                self.kill()
                return False

            # Match ADB.connect(): do not publish a backend that cannot capture.
            # Preserve the public zero-argument screenshot contract used by
            # test doubles and other backends while tagging this native call.
            self._capture_failure_stage_context = "CONNECT_HEALTH_CAPTURE"
            try:
                screenshot = self.screenshot()
            finally:
                self._capture_failure_stage_context = ""
            expected_shape = (self.height, self.width, 3)
            if not isinstance(screenshot, np.ndarray) or screenshot.shape != expected_shape:
                actual_shape = getattr(screenshot, "shape", None)
                raise RuntimeError(
                    f"NEMUIPC截图无效: 期望 {expected_shape}, 实际 {actual_shape}"
                )
            # ADB is diagnostic-only here: it proves process/foreground state
            # and may capture one cross-check frame, but is never an input
            # fallback for a NEMU receipt failure.
            if self.device.port is not None:
                try:
                    health_adb = AdbDeviceTcp(
                        str(getattr(self.device, "adb_host", "127.0.0.1") or "127.0.0.1"),
                        port=int(self.device.port),
                    )
                    if health_adb.connect():
                        self._health_adb = health_adb
                except Exception as error:
                    logger.warning(f"NEMU只读健康ADB不可用: {type(error).__name__}")
            return True
        except Exception:
            try:
                self.kill()
            except Exception:
                logger.exception("NEMUIPC连接失败后的资源清理也失败")
            raise

    def input_swipe(
        self, x1: int, y1: int, x2: int, y2: int, millisecond: int = 100
    ) -> NemuTouchReceipt:
        ensure_automation_allowed("通过 NEMU 滑动游戏界面")
        if self.session_quarantined:
            raise RuntimeError("nemu_session_quarantined")
        if self.connect_id is None or self.session_generation <= 0:
            raise RuntimeError("nemu_session_not_connected")
        if self.display_id is None or int(self.display_id) < 0:
            raise RuntimeError("nemu_display_id_invalid")

        capture_points = tuple(
            (int(point[0]), int(point[1]))
            for point in swipe_path((x1, y1), (x2, y2), millisecond)
        )
        if not capture_points:
            raise RuntimeError("nemu_swipe_path_empty")
        mapped_points = tuple(
            map_capture_to_nemu(
                point,
                capture_size=(self.width, self.height),
                display_size=(self.width, self.height),
                rotation=0,
            )
            for point in capture_points
        )
        builder = NemuTouchReceiptBuilder(
            instance_id=str(self.device.index),
            display_id=int(self.display_id),
            session_generation=self.session_generation,
            capture_size=(self.width, self.height),
            display_size=(self.width, self.height),
            rotation=0,
            capture_point=capture_points[0],
            mapped_nemu_point=mapped_points[0],
        )
        healthy, health_reasons = self.input_health()
        if not healthy:
            builder.delivery = DeliveryStatus.REJECTED_BEFORE_DELIVERY
            builder.reasons.extend(health_reasons)
            receipt = builder.finish(python_call_returned=True)
            self.last_touch_receipt = receipt
            raise NemuInputDispatchError(receipt)

        accepted_down = False
        try:
            for point_index, point in enumerate(mapped_points):
                builder.down_called = True
                builder.down_code = int(self.nemu.nemu_input_event_touch_down(
                    self.connect_id, self.display_id, *point
                ))
                builder.down_status = self._native_status(builder.down_code)
                if builder.down_status is NativeCallStatus.REJECTED:
                    builder.delivery = (
                        DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH
                        if accepted_down
                        else DeliveryStatus.REJECTED_BEFORE_DELIVERY
                    )
                    builder.release = (
                        ReleaseStatus.UNKNOWN if accepted_down else ReleaseStatus.NOT_REQUIRED
                    )
                    builder.reasons.append(
                        f"swipe_touch_down_native_rejected_at_{point_index}"
                    )
                    if accepted_down:
                        self._best_effort_swipe_release(builder)
                        self.session_quarantined = True
                    receipt = builder.finish(python_call_returned=True)
                    self.last_touch_receipt = receipt
                    raise NemuInputDispatchError(receipt)
                if builder.down_status is NativeCallStatus.UNKNOWN:
                    builder.delivery = DeliveryStatus.UNKNOWN_AFTER_EXCEPTION
                    builder.release = ReleaseStatus.UNKNOWN
                    builder.reasons.append(
                        f"swipe_touch_down_return_code_unknown_at_{point_index}"
                    )
                    self._best_effort_swipe_release(builder)
                    self.session_quarantined = True
                    receipt = builder.finish(python_call_returned=True)
                    self.last_touch_receipt = receipt
                    raise NemuInputDispatchError(receipt)
                accepted_down = True
                time.sleep(0.01)

            builder.up_called = True
            builder.up_code = int(self.nemu.nemu_input_event_touch_up(
                self.connect_id, self.display_id
            ))
            builder.up_status = self._native_status(builder.up_code)
            if builder.up_status is not NativeCallStatus.ACCEPTED:
                builder.delivery = DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH
                builder.release = ReleaseStatus.UNKNOWN
                builder.reasons.append("swipe_touch_up_not_accepted")
                self.session_quarantined = True
                receipt = builder.finish(python_call_returned=True)
                self.last_touch_receipt = receipt
                raise NemuInputDispatchError(receipt)

            builder.delivery = DeliveryStatus.NATIVE_ACCEPTED
            builder.release = ReleaseStatus.CONFIRMED
            receipt = builder.finish(python_call_returned=True)
            self.last_touch_receipt = receipt
        except NemuInputDispatchError:
            raise
        except Exception:
            builder.delivery = DeliveryStatus.UNKNOWN_AFTER_EXCEPTION
            if builder.down_called:
                self._best_effort_swipe_release(builder)
            else:
                builder.release = ReleaseStatus.NOT_REQUIRED
            builder.reasons.append("swipe_python_exception_after_native_call")
            self.session_quarantined = True
            receipt = builder.finish(python_call_returned=False)
            self.last_touch_receipt = receipt
            raise NemuInputDispatchError(receipt)
        time.sleep(0.05)
        return receipt

    def _best_effort_swipe_release(
        self, builder: NemuTouchReceiptBuilder
    ) -> None:
        """Attempt one native release without masking the dispatch failure."""
        if builder.up_called:
            return
        builder.up_called = True
        try:
            builder.up_code = int(
                self.nemu.nemu_input_event_touch_up(
                    self.connect_id, self.display_id
                )
            )
            builder.up_status = self._native_status(builder.up_code)
            if builder.up_status is NativeCallStatus.ACCEPTED:
                builder.release = ReleaseStatus.CONFIRMED
                builder.reasons.append("swipe_best_effort_touch_up_accepted")
            else:
                builder.release = ReleaseStatus.UNKNOWN
                builder.reasons.append("swipe_best_effort_touch_up_not_accepted")
        except Exception as error:
            builder.up_status = NativeCallStatus.UNKNOWN
            builder.release = ReleaseStatus.UNKNOWN
            builder.reasons.append(
                f"swipe_best_effort_touch_up_exception:{type(error).__name__}"
            )


    @staticmethod
    def _native_status(return_code: int) -> NativeCallStatus:
        if return_code == 0:
            return NativeCallStatus.ACCEPTED
        if return_code > 0:
            return NativeCallStatus.REJECTED
        return NativeCallStatus.UNKNOWN

    def input_health(self) -> tuple[bool, tuple[str, ...]]:
        reasons = []
        if self.device.index < 0:
            reasons.append("nemu_instance_mismatch")
        if self.display_id is None or int(self.display_id) < 0:
            reasons.append("nemu_display_id_invalid")
        if self.width <= 0 or self.height <= 0:
            reasons.append("nemu_capture_size_invalid")
        if self.session_generation <= 0 or self.connect_id is None:
            reasons.append("nemu_session_invalid")
        adb = self._health_adb
        if adb is None:
            reasons.extend(("game_process_liveness_unproven", "foreground_package_unproven"))
        else:
            try:
                if not str(adb.shell("pidof com.hermes.goda") or "").strip():
                    reasons.append("game_process_not_alive")
            except Exception:
                reasons.append("game_process_liveness_unproven")
            try:
                foreground = str(adb.shell("dumpsys window windows") or "")
                if "com.hermes.goda" not in foreground:
                    reasons.append("foreground_package_mismatch")
            except Exception:
                reasons.append("foreground_package_unproven")
        return not reasons, tuple(reasons)

    def input_tap(self, x: int, y: int) -> NemuTouchReceipt:
        ensure_automation_allowed("通过 NEMU 点击游戏界面")
        if self.session_quarantined:
            raise RuntimeError("nemu_session_quarantined")
        if self.connect_id is None or self.session_generation <= 0:
            raise RuntimeError("nemu_session_not_connected")
        capture_point = (int(x), int(y))
        mapped = map_capture_to_nemu(
            capture_point,
            capture_size=(self.width, self.height),
            display_size=(self.width, self.height),
            rotation=0,
        )
        builder = NemuTouchReceiptBuilder(
            instance_id=str(self.device.index),
            display_id=int(self.display_id),
            session_generation=self.session_generation,
            capture_size=(self.width, self.height),
            display_size=(self.width, self.height),
            rotation=0,
            capture_point=capture_point,
            mapped_nemu_point=mapped,
        )
        healthy, health_reasons = self.input_health()
        if not healthy:
            builder.delivery = DeliveryStatus.REJECTED_BEFORE_DELIVERY
            builder.reasons.extend(health_reasons)
            receipt = builder.finish(python_call_returned=True)
            self.last_touch_receipt = receipt
            raise NemuInputDispatchError(receipt)
        try:
            builder.down_called = True
            builder.down_code = int(self.nemu.nemu_input_event_touch_down(
                self.connect_id, self.display_id, *mapped
            ))
            builder.down_status = self._native_status(builder.down_code)
            if builder.down_status is NativeCallStatus.REJECTED:
                builder.delivery = DeliveryStatus.REJECTED_BEFORE_DELIVERY
                builder.reasons.append("touch_down_native_rejected")
                receipt = builder.finish(python_call_returned=True)
                self.last_touch_receipt = receipt
                raise NemuInputDispatchError(receipt)
            if builder.down_status is NativeCallStatus.UNKNOWN:
                builder.delivery = DeliveryStatus.UNKNOWN_AFTER_EXCEPTION
                builder.release = ReleaseStatus.UNKNOWN
                builder.reasons.append("touch_down_return_code_unknown")
                self.session_quarantined = True
                receipt = builder.finish(python_call_returned=True)
                self.last_touch_receipt = receipt
                raise NemuInputDispatchError(receipt)
            builder.up_called = True
            builder.up_code = int(self.nemu.nemu_input_event_touch_up(
                self.connect_id, self.display_id
            ))
            builder.up_status = self._native_status(builder.up_code)
            if builder.up_status is not NativeCallStatus.ACCEPTED:
                builder.delivery = DeliveryStatus.UNKNOWN_AFTER_PARTIAL_DISPATCH
                builder.release = ReleaseStatus.UNKNOWN
                builder.reasons.append("touch_up_not_accepted")
                self.session_quarantined = True
                receipt = builder.finish(python_call_returned=True)
                self.last_touch_receipt = receipt
                raise NemuInputDispatchError(receipt)
            builder.delivery = DeliveryStatus.NATIVE_ACCEPTED
            builder.release = ReleaseStatus.CONFIRMED
            receipt = builder.finish(python_call_returned=True)
            self.last_touch_receipt = receipt
        except NemuInputDispatchError:
            raise
        except Exception:
            builder.delivery = DeliveryStatus.UNKNOWN_AFTER_EXCEPTION
            builder.release = (
                ReleaseStatus.UNKNOWN if builder.down_called else ReleaseStatus.NOT_REQUIRED
            )
            builder.reasons.append("python_exception_after_native_call")
            self.session_quarantined = True
            receipt = builder.finish(python_call_returned=False)
            self.last_touch_receipt = receipt
            raise NemuInputDispatchError(receipt)
        time.sleep(0.5)
        return receipt

    def _capture_lifecycle_conflict(self) -> str:
        if (
            getattr(self, "health_capture_return_code", None) != 0
            or getattr(self, "health_capture_session_generation", None)
            != getattr(self, "session_generation", 0)
        ):
            return "NOT_PROVEN"
        changed = any(
            (
                getattr(self, "connect_id", None)
                != getattr(self, "_health_capture_instance_handle", None),
                getattr(self, "display_id", None)
                != getattr(self, "_health_capture_display_id", None),
                int(getattr(self, "disconnect_call_count", 0))
                != int(getattr(self, "_health_capture_disconnect_call_count", 0)),
            )
        )
        return "YES" if changed else "NO"

    def screenshot(
        self,
        *,
        failure_stage: str | None = None,
    ) -> cv.typing.MatLike:
        ensure_automation_allowed("通过 NEMU 读取游戏画面")
        failure_stage = str(
            failure_stage
            or getattr(self, "_capture_failure_stage_context", "")
            or "RUNTIME_CAPTURE"
        )
        result = int(self.nemu.nemu_capture_display(self.connect_id, self.display_id, self.length, self.width_ptr, self.height_ptr, self.pixels_pointer))
        self.native_capture_call_count = int(
            getattr(self, "native_capture_call_count", 0)
        ) + 1
        call_index = self.native_capture_call_count
        self.last_capture_return_code = result
        self.last_capture_native_status = "ACCEPTED" if result == 0 else "REJECTED"
        if failure_stage == "CONNECT_HEALTH_CAPTURE":
            self.health_capture_return_code = result
            self.health_capture_session_generation = int(
                getattr(self, "session_generation", 0)
            )
        else:
            self.business_capture_return_code = result
            self.business_capture_session_generation = int(
                getattr(self, "session_generation", 0)
            )
        if result != 0:
            raise NemuCaptureError(
                native_return_code=result,
                instance_index=getattr(getattr(self, "device", None), "index", None),
                instance_handle=getattr(self, "connect_id", None),
                display_id=getattr(self, "display_id", None),
                session_generation=getattr(self, "session_generation", 0),
                connect_epoch=getattr(self, "connect_epoch", ""),
                capture_call_index=call_index,
                last_successful_capture_call_index=getattr(
                    self, "last_successful_capture_call_index", 0
                ),
                capture_width=getattr(self, "width", 0),
                capture_height=getattr(self, "height", 0),
                thread_id=threading.get_ident(),
                failure_stage=failure_stage,
                session_lifecycle_conflict=self._capture_lifecycle_conflict(),
                health_capture_return_code=getattr(
                    self, "health_capture_return_code", None
                ),
                health_capture_session_generation=getattr(
                    self, "health_capture_session_generation", None
                ),
                business_capture_return_code=getattr(
                    self, "business_capture_return_code", None
                ),
                business_capture_session_generation=getattr(
                    self, "business_capture_session_generation", None
                ),
                disconnect_call_count=getattr(self, "disconnect_call_count", 0),
                kill_call_count=getattr(self, "kill_call_count", 0),
                last_disconnect_timestamp=getattr(
                    self, "last_disconnect_timestamp", ""
                ),
                last_kill_timestamp=getattr(self, "last_kill_timestamp", ""),
            )
        image = np.frombuffer(self.pixels_array, dtype=np.uint8).reshape((self.height, self.width, 4))

        image = cv.cvtColor(image, cv.COLOR_BGRA2BGR)
        image = cv.flip(image, 0)
        self.last_successful_capture_call_index = call_index
        if failure_stage == "CONNECT_HEALTH_CAPTURE":
            self._health_capture_instance_handle = getattr(self, "connect_id", None)
            self._health_capture_display_id = getattr(self, "display_id", None)
            self._health_capture_disconnect_call_count = int(
                getattr(self, "disconnect_call_count", 0)
            )
        return image
    
    def kill(self):
        ensure_automation_allowed("关闭 NEMU 连接")
        connect_id = getattr(self, "connect_id", None)
        if connect_id is None:
            return
        self.kill_call_count = int(getattr(self, "kill_call_count", 0)) + 1
        self.last_kill_timestamp = datetime.now().astimezone().isoformat(
            timespec="microseconds"
        )
        self.connect_id = None
        health_adb, self._health_adb = getattr(self, "_health_adb", None), None
        if health_adb is not None:
            try:
                health_adb.close()
            except Exception:
                pass
        self.nemu.nemu_disconnect(connect_id)
        self.disconnect_call_count = int(
            getattr(self, "disconnect_call_count", 0)
        ) + 1
        self.last_disconnect_timestamp = datetime.now().astimezone().isoformat(
            timespec="microseconds"
        )
