"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-03-20 22:24:35
LastEditTime: 2025-02-11 16:56:45
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

import hashlib
import random
import secrets
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import cv2 as cv
import numpy as np
from loguru import logger

from core.control.adb import ADB
from core.control.adb_port import EmulatorInfo, EmulatorType
from core.control.base_control import IADB
from core.control.nemu import IPCUnavailableError, NEMU
from core.exception.exceptions import StopExecution
from core.image.image import Image
from core.model import app
from core.services.repair_safety import ensure_automation_allowed

EXCURSIONX = [-10, 10]
EXCURSIONY = [-10, 10]
STOP = False
MAX_SWIPE_SEGMENTS = 100

control: IADB = ADB()
_runtime_device: EmulatorInfo | None = None
_runtime_auto_start_emulator: bool | None = None
_BACKEND_LOCK = threading.RLock()
_BACKEND_GENERATION = 1
_BACKEND_CONNECTED_AT = datetime.now().astimezone()
_CAPTURE_SESSION_GENERATION = _BACKEND_GENERATION
_CAPTURE_SEQUENCE = 0
_ACTION_POLICY_LOCK = threading.RLock()
_ACTION_POLICY_OWNERS: dict[str, object] = {}
_ACTION_POLICY_ORDER: list[str] = []
_READ_ONLY_FAIL_CLOSED = False


@dataclass(frozen=True)
class _ProductionSessionProvenance:
    session_id: str
    backend_object_identity: str
    backend_generation: int
    instance_id: str
    adb_serial: str
    policy_revision: str


_PRODUCTION_SESSION_REGISTRY: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_PRODUCTION_SESSION_REGISTRY_LOCK = threading.RLock()


@dataclass(frozen=True)
class CaptureEnvelope:
    frame: cv.typing.MatLike
    backend_capture_id: str
    backend_monotonic_sequence: int
    captured_at: datetime
    raw_frame_hash: str
    backend_generation: int
    backend_object_identity: str
    instance_id: str
    adb_serial: str
    geometry_revision: str


class _BoundControlInputExecutor:
    """The only production bridge from a read-only guard to device input."""

    def __init__(self, backend: IADB):
        self.__backend = backend

    def tap(self, physical_point: tuple[int, int]):
        self.__backend.input_tap(int(physical_point[0]), int(physical_point[1]))

    def swipe(
        self,
        physical_trajectory: tuple[tuple[int, int], ...],
        duration_ms: int,
    ):
        start, end = physical_trajectory[0], physical_trajectory[-1]
        self.__backend.input_swipe(
            int(start[0]), int(start[1]), int(end[0]), int(end[1]), int(duration_ms)
        )


def _create_bound_input_executor(backend: IADB):
    """Internal/test factory; the returned bridge is never stored on a session."""

    return _BoundControlInputExecutor(backend)


def _register_production_session(policy, identity, policy_revision: str) -> None:
    with _PRODUCTION_SESSION_REGISTRY_LOCK:
        _PRODUCTION_SESSION_REGISTRY[policy] = _ProductionSessionProvenance(
            session_id=secrets.token_urlsafe(24),
            backend_object_identity=identity.backend_object_identity,
            backend_generation=int(identity.backend_generation),
            instance_id=str(identity.instance_id),
            adb_serial=str(identity.adb_serial),
            policy_revision=str(policy_revision),
        )


def _revoke_production_session(policy) -> None:
    with _PRODUCTION_SESSION_REGISTRY_LOCK:
        _PRODUCTION_SESSION_REGISTRY.pop(policy, None)


def _validate_production_session(policy) -> _ProductionSessionProvenance:
    from core.services.read_only_policy import (
        PRODUCTION_POLICY_REVISION,
        ProductionReadOnlySafetySession,
    )

    if not isinstance(policy, ProductionReadOnlySafetySession):
        raise TypeError("production session provenance required")
    with _PRODUCTION_SESSION_REGISTRY_LOCK:
        provenance = _PRODUCTION_SESSION_REGISTRY.get(policy)
    if provenance is None or policy.closed:
        raise PermissionError("production session provenance is not registered")
    identity = current_bound_device_identity()
    if (
        provenance.backend_object_identity != identity.backend_object_identity
        or provenance.backend_generation != identity.backend_generation
        or provenance.instance_id != identity.instance_id
        or provenance.adb_serial != identity.adb_serial
        or provenance.policy_revision != PRODUCTION_POLICY_REVISION
    ):
        _revoke_production_session(policy)
        raise PermissionError("production session provenance changed")
    return provenance


def activate_action_policy(policy) -> str:
    """Install an owner-token policy without previous/restore races."""

    _validate_production_session(policy)
    token = secrets.token_urlsafe(24)
    global _READ_ONLY_FAIL_CLOSED
    with _BACKEND_LOCK:
        with _ACTION_POLICY_LOCK:
            _ACTION_POLICY_OWNERS[token] = policy
            _ACTION_POLICY_ORDER.append(token)
            _READ_ONLY_FAIL_CLOSED = True
    return token


def remove_action_policy(token: str, *, close_session: bool = True) -> bool:
    """Remove only the matching owner; out-of-order exits preserve newer owners."""

    global _READ_ONLY_FAIL_CLOSED
    with _BACKEND_LOCK:
        with _ACTION_POLICY_LOCK:
            policy = _ACTION_POLICY_OWNERS.pop(str(token), None)
            if policy is None:
                return False
            _ACTION_POLICY_ORDER[:] = [item for item in _ACTION_POLICY_ORDER if item != token]
            if close_session:
                policy.close()
            if not _ACTION_POLICY_OWNERS and close_session:
                _READ_ONLY_FAIL_CLOSED = False
            return True


def current_action_policy():
    with _ACTION_POLICY_LOCK:
        while _ACTION_POLICY_ORDER and _ACTION_POLICY_ORDER[-1] not in _ACTION_POLICY_OWNERS:
            _ACTION_POLICY_ORDER.pop()
        return _ACTION_POLICY_OWNERS.get(_ACTION_POLICY_ORDER[-1]) if _ACTION_POLICY_ORDER else None


def close_orphaned_action_policy(policy) -> None:
    """Explicitly close a fail-closed session after owner metadata was lost."""

    global _READ_ONLY_FAIL_CLOSED
    policy.close()
    with _ACTION_POLICY_LOCK:
        if not _ACTION_POLICY_OWNERS:
            _READ_ONLY_FAIL_CLOSED = False


def current_display_geometry():
    """Return the deterministic logical-to-device geometry for guarded input."""

    from core.services.read_only_policy import DisplayGeometry

    with _BACKEND_LOCK:
        return DisplayGeometry.from_ratio(float(getattr(control, "ratio", 1.0)))


def current_bound_device_identity():
    from core.services.read_only_policy import BoundDeviceIdentity

    with _BACKEND_LOCK:
        device = get_runtime_device()
        geometry = current_display_geometry()
        instance_id = str(getattr(device, "index", "0"))
        port = getattr(device, "port", None)
        adb_serial = f"127.0.0.1:{port}" if port is not None else "backend-local"
        return BoundDeviceIdentity(
            emulator_backend=type(control).__name__,
            instance_id=instance_id,
            adb_serial=adb_serial,
            backend_generation=_BACKEND_GENERATION,
            backend_object_identity=f"{type(control).__name__}:{id(control):x}",
            display_geometry_revision=geometry.geometry_revision,
            connected_at=_BACKEND_CONNECTED_AT,
        )


def capture_envelope() -> CaptureEnvelope:
    """Perform one real backend capture and mint freshness at the boundary."""

    global _CAPTURE_SESSION_GENERATION, _CAPTURE_SEQUENCE
    ensure_automation_allowed("读取游戏画面")
    with _BACKEND_LOCK:
        if STOP:
            raise StopExecution()
        if _CAPTURE_SESSION_GENERATION != _BACKEND_GENERATION:
            _CAPTURE_SESSION_GENERATION = _BACKEND_GENERATION
            _CAPTURE_SEQUENCE = 0
        raw = control.screenshot()
        frame = cv.resize(raw, control.dsize, interpolation=cv.INTER_AREA)
        _CAPTURE_SEQUENCE += 1
        sequence = _CAPTURE_SEQUENCE
        captured_at = datetime.now().astimezone()
        raw_hash = hashlib.sha256(raw.tobytes()).hexdigest()
        identity = current_bound_device_identity()
        capture_id = hashlib.sha256(
            "|".join((
                identity.backend_object_identity,
                str(identity.backend_generation),
                str(sequence),
                captured_at.isoformat(timespec="microseconds"),
                raw_hash,
            )).encode("utf-8")
        ).hexdigest()
        return CaptureEnvelope(
            frame=frame,
            backend_capture_id=capture_id,
            backend_monotonic_sequence=sequence,
            captured_at=captured_at,
            raw_frame_hash=raw_hash,
            backend_generation=identity.backend_generation,
            backend_object_identity=identity.backend_object_identity,
            instance_id=identity.instance_id,
            adb_serial=identity.adb_serial,
            geometry_revision=identity.display_geometry_revision,
        )


def _create_production_read_only_safety_session(observer, *, resolver=None, now=None):
    """Production factory that seals the exact backend and identity snapshot."""

    from core.services.read_only_policy import (
        AnchorResolver,
        PRODUCTION_POLICY_REVISION,
        PRODUCTION_POLICY_SPECS,
        ProductionReadOnlySafetySession,
        ReadOnlyPermitIssuer,
    )

    with _BACKEND_LOCK:
        backend = control
        identity = current_bound_device_identity()
        issuer = ReadOnlyPermitIssuer(
            observer,
            resolver or AnchorResolver(),
            policies=PRODUCTION_POLICY_SPECS,
            now=now or (lambda: datetime.now().astimezone()),
            bound_device_identity=identity,
            identity_provider=current_bound_device_identity,
        )
        session = ProductionReadOnlySafetySession(
            _create_bound_input_executor(backend),
            permit_issuer=issuer,
            now=now or (lambda: datetime.now().astimezone()),
            device_action_lock=_BACKEND_LOCK,
        )
        _register_production_session(session, identity, PRODUCTION_POLICY_REVISION)
        return session


def set_runtime_device(device: EmulatorInfo | None) -> None:
    """Freeze the target used by an active queue independently of GUI config."""

    global _runtime_device
    _runtime_device = (
        EmulatorInfo.from_dict(device.to_dict()) if device is not None else None
    )


def clear_runtime_device() -> None:
    global _runtime_auto_start_emulator
    set_runtime_device(None)
    _runtime_auto_start_emulator = None


def has_runtime_device() -> bool:
    return _runtime_device is not None


def set_runtime_auto_start_emulator(enabled: bool) -> None:
    global _runtime_auto_start_emulator
    _runtime_auto_start_emulator = bool(enabled)


def get_runtime_auto_start_emulator() -> bool:
    if _runtime_auto_start_emulator is None:
        return True
    return _runtime_auto_start_emulator


def get_runtime_device() -> EmulatorInfo:
    device = _runtime_device or app.Global.device
    # Tests and legacy callers may provide a lightweight device-like object.
    # Preserve it instead of requiring the newer serialisation API.
    if not hasattr(device, "to_dict"):
        return device
    return EmulatorInfo.from_dict(device.to_dict())


def _close_backend(candidate: IADB, name: str) -> None:
    try:
        candidate.kill()
    except Exception:
        logger.exception(f"{name}连接资源清理失败")


def _activate_backend(candidate: IADB) -> None:
    global control, _BACKEND_GENERATION, _BACKEND_CONNECTED_AT
    with _BACKEND_LOCK:
        previous = control
        control = candidate
        if previous is not candidate:
            _BACKEND_GENERATION += 1
            _BACKEND_CONNECTED_AT = datetime.now().astimezone()
            with _PRODUCTION_SESSION_REGISTRY_LOCK:
                _PRODUCTION_SESSION_REGISTRY.clear()
            _close_backend(previous, "旧控制后端")


def _known_nemu_unavailable(error: BaseException) -> bool:
    if isinstance(error, (FileNotFoundError, IPCUnavailableError)):
        return True
    if not isinstance(error, OSError):
        return False
    winerror = getattr(error, "winerror", None)
    if winerror in {126, 127, 193}:
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "dll",
            "entry point",
            "specified module could not be found",
            "找不到指定的模块",
            "找不到指定的程序",
        )
    )


def connect(adb_port: Optional[int] = None):
    """
    连接ADB

    :param order: ADB端口
    """
    ensure_automation_allowed("连接 ADB/NEMU")
    global control
    device = get_runtime_device()
    if device.is_mumu:
        nemu_candidate = None
        try:
            nemu_candidate = NEMU(device)
            status = nemu_candidate.connect(adb_port)
        except Exception as error:
            if _known_nemu_unavailable(error):
                logger.warning(
                    "MUMUIPC当前不可用，按预期降级到ADB："
                    f"{type(error).__name__}: {error}"
                )
                status = False
            else:
                if nemu_candidate is not None:
                    _close_backend(nemu_candidate, "NEMUIPC")
                logger.exception("MUMUIPC发生非预期编程错误，禁止静默降级")
                raise
        if status:
            _activate_backend(nemu_candidate)
            return True
        if nemu_candidate is not None:
            _close_backend(nemu_candidate, "NEMUIPC")
        logger.warning("MUMUIPC连接失败，尝试使用ADB连接")

    adb_candidate = ADB()
    try:
        status = adb_candidate.connect(adb_port if adb_port is not None else device.port)
    except Exception:
        _close_backend(adb_candidate, "ADB")
        raise
    if status:
        _activate_backend(adb_candidate)
    else:
        _close_backend(adb_candidate, "ADB")
    return status


def connect_adb(adb_port: Optional[int] = None):
    """Force the TCP ADB transport for workflows that require shell evidence."""
    ensure_automation_allowed("连接 ADB")
    device = get_runtime_device()
    candidate = ADB()
    status = candidate.connect(adb_port if adb_port is not None else device.port)
    if status:
        _activate_backend(candidate)
    else:
        _close_backend(candidate, "ADB")
    return status


def stop():
    global STOP
    STOP = True


def reset_stop():
    """Clear a previous global stop request before a new queue starts."""
    global STOP
    STOP = False


def is_stopped() -> bool:
    return STOP


def kill():
    """
    关闭连接
    """
    control.kill()


def _legacy_input_swipe(
    pos1=(919, 617),
    pos2=(919, 908),
    swipe_time: int = 100,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    """
    滑动屏幕(可超出屏幕)

    :param pos1: 坐标1
    :param pos2: 坐标2
    :param time: 操作时间(毫秒)
    """
    ensure_automation_allowed("滑动游戏界面")
    if STOP:
        raise StopExecution()
    # 添加随机值
    pos_x1 = control.ratio * pos1[0] + random.randint(*EXCURSIONX)
    pos_y1 = control.ratio * pos1[1] + random.randint(*EXCURSIONY)
    pos_x2 = control.ratio * pos2[0] + random.randint(*EXCURSIONX)
    pos_y2 = control.ratio * pos2[1] + random.randint(*EXCURSIONY)

    logger.debug(f"滑动 ({pos_x1}, {pos_y1}) -> ({pos_x2}, {pos_y2})")
    remaining_x = pos_x2 - pos_x1
    remaining_y = pos_y2 - pos_y1
    safe_x1, safe_y1, safe_x2, safe_y2 = control.safe_area
    preferred_start = (
        max(safe_x1, min(pos_x1, safe_x2)),
        max(safe_y1, min(pos_y1, safe_y2)),
    )

    for segment in range(MAX_SWIPE_SEGMENTS):
        if abs(remaining_x) <= 10 and abs(remaining_y) <= 10:
            return
        if STOP:
            raise StopExecution()
        if segment >= 1:
            time.sleep(0.5)
            if STOP:
                raise StopExecution()

        if segment == 0:
            start_x, start_y = preferred_start
        else:
            start_x = (
                safe_x1
                if remaining_x > 0
                else safe_x2
                if remaining_x < 0
                else preferred_start[0]
            )
            start_y = (
                safe_y1
                if remaining_y > 0
                else safe_y2
                if remaining_y < 0
                else preferred_start[1]
            )

        def progress_ratio(x, y):
            ratios = [1.0]
            if remaining_x > 0:
                ratios.append((safe_x2 - x) / remaining_x)
            elif remaining_x < 0:
                ratios.append((x - safe_x1) / -remaining_x)
            if remaining_y > 0:
                ratios.append((safe_y2 - y) / remaining_y)
            elif remaining_y < 0:
                ratios.append((y - safe_y1) / -remaining_y)
            return max(0.0, min(ratios))

        ratio = progress_ratio(start_x, start_y)
        limit_pos_x1 = int(round(start_x))
        limit_pos_y1 = int(round(start_y))
        limit_pos_x2 = int(round(start_x + remaining_x * ratio))
        limit_pos_y2 = int(round(start_y + remaining_y * ratio))
        actual_x = limit_pos_x2 - limit_pos_x1
        actual_y = limit_pos_y2 - limit_pos_y1

        if actual_x == 0 and actual_y == 0 and segment == 0:
            start_x = (
                safe_x1
                if remaining_x > 0
                else safe_x2
                if remaining_x < 0
                else preferred_start[0]
            )
            start_y = (
                safe_y1
                if remaining_y > 0
                else safe_y2
                if remaining_y < 0
                else preferred_start[1]
            )
            ratio = progress_ratio(start_x, start_y)
            limit_pos_x1 = int(round(start_x))
            limit_pos_y1 = int(round(start_y))
            limit_pos_x2 = int(round(start_x + remaining_x * ratio))
            limit_pos_y2 = int(round(start_y + remaining_y * ratio))
            actual_x = limit_pos_x2 - limit_pos_x1
            actual_y = limit_pos_y2 - limit_pos_y1

        if actual_x == 0 and actual_y == 0:
            raise RuntimeError(
                f"滑动无法推进：剩余位移 ({remaining_x}, {remaining_y})，"
                f"安全区域 {control.safe_area}"
            )

        logger.debug(
            f"多次滑动 ({limit_pos_x1}, {limit_pos_y1}) -> ({limit_pos_x2}, {limit_pos_y2})"
        )

        control.input_swipe(
            limit_pos_x1, limit_pos_y1, limit_pos_x2, limit_pos_y2, swipe_time
        )

        previous_distance = abs(remaining_x) + abs(remaining_y)
        remaining_x -= actual_x
        remaining_y -= actual_y
        if abs(remaining_x) + abs(remaining_y) >= previous_distance:
            raise RuntimeError(
                f"滑动无法推进：分段位移 ({actual_x}, {actual_y}) 未减少剩余距离"
            )
        if abs(remaining_x) <= 10 and abs(remaining_y) <= 10:
            return

    raise RuntimeError(
        f"滑动超过最大分段次数 {MAX_SWIPE_SEGMENTS}，"
        f"仍剩余位移 ({remaining_x}, {remaining_y})"
    )


def _legacy_input_tap(
    pos: Tuple[int, int] = (880, 362),
    random_offset: bool = True,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    """
    点击坐标

    :param pos: 坐标
    """
    ensure_automation_allowed("点击游戏界面")
    if STOP:
        raise StopExecution()
    offset_x = random.randint(*EXCURSIONX) if random_offset else 0
    offset_y = random.randint(*EXCURSIONY) if random_offset else 0
    control.input_tap(
        int(control.ratio * pos[0] + offset_x),
        int(control.ratio * pos[1] + offset_y),
    )
    return True


def _linear_trajectory(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    maximum_step: int = 25,
) -> tuple[tuple[int, int], ...]:
    distance = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    steps = max(1, int((distance + maximum_step - 1) // maximum_step))
    return tuple(
        (
            int(round(start[0] + (end[0] - start[0]) * index / steps)),
            int(round(start[1] + (end[1] - start[1]) * index / steps)),
        )
        for index in range(steps + 1)
    )


def _input_swipe_locked(
    pos1=(919, 617),
    pos2=(919, 908),
    swipe_time: int = 100,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    action_policy = current_action_policy()
    if action_policy is None:
        if _READ_ONLY_FAIL_CLOSED:
            raise PermissionError("read_only_session_policy_unavailable")
        return _legacy_input_swipe(
            pos1, pos2, swipe_time, intent=intent, permit=permit,
            page_id=page_id, page_fingerprint=page_fingerprint,
            anchor_key=anchor_key,
        )
    _validate_production_session(action_policy)
    ensure_automation_allowed("滑动游戏界面")
    if STOP:
        raise StopExecution()
    # Read-only authorization is exclusively logical. The guard owns the
    # trusted executor and submits the exact transformed trajectory itself.
    start = (int(pos1[0]), int(pos1[1]))
    end = (int(pos2[0]), int(pos2[1]))
    trajectory = _linear_trajectory(start, end)

    if intent is None:
        return False
    if not action_policy.request_swipe(
        intent, trajectory, swipe_time, geometry=current_display_geometry()
    ):
        return False
    return True


def input_swipe(
    pos1=(919, 617),
    pos2=(919, 908),
    swipe_time: int = 100,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    with _BACKEND_LOCK:
        return _input_swipe_locked(
            pos1, pos2, swipe_time, intent=intent, permit=permit,
            page_id=page_id, page_fingerprint=page_fingerprint,
            anchor_key=anchor_key,
        )


def _input_tap_locked(
    pos: Tuple[int, int] = (880, 362),
    random_offset: bool = True,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    action_policy = current_action_policy()
    if action_policy is None:
        if _READ_ONLY_FAIL_CLOSED:
            raise PermissionError("read_only_session_policy_unavailable")
        return _legacy_input_tap(
            pos, random_offset=random_offset, intent=intent, permit=permit,
            page_id=page_id, page_fingerprint=page_fingerprint,
            anchor_key=anchor_key,
        )
    _validate_production_session(action_policy)
    ensure_automation_allowed("点击游戏界面")
    if STOP:
        raise StopExecution()
    logical = (int(pos[0]), int(pos[1]))

    if intent is None:
        return False
    if not action_policy.request_tap(
        intent, logical, geometry=current_display_geometry()
    ):
        return False
    return True


def input_tap(
    pos: Tuple[int, int] = (880, 362),
    random_offset: bool = True,
    *,
    intent=None,
    permit=None,
    page_id: str = "",
    page_fingerprint: str = "",
    anchor_key: str = "",
):
    with _BACKEND_LOCK:
        return _input_tap_locked(
            pos, random_offset=random_offset, intent=intent, permit=permit,
            page_id=page_id, page_fingerprint=page_fingerprint,
            anchor_key=anchor_key,
        )


def screenshot() -> Image:
    """
    截图
    """
    if STOP:
        raise StopExecution()

    screenshot = screenshot_image()

    return Image(screenshot)


def screenshot_image() -> cv.typing.MatLike:
    """
    截图并返回图片对象
    """
    return capture_envelope().frame

def wait_stopped(threshold=7100000, timeout=15.0):
    """
    等待画面静止
    参数:
        :param threshold: 参数阈值
    """
    logger.info("等待图像静止")
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        gray1 = cv.cvtColor(screenshot_image(), cv.COLOR_BGR2GRAY)
        # 等待画面变动，并再次截图
        time.sleep(0.5)
        gray2 = cv.cvtColor(screenshot_image(), cv.COLOR_BGR2GRAY)

        # 计算两帧之间的绝对差异
        diff = cv.absdiff(gray1, gray2)

        # 计算差异值
        diff_sum: int = np.sum(diff)  # type: ignore
        logger.debug(f"画面差异 {diff_sum}")

        if diff_sum < threshold:
            return True
        time.sleep(1)
    logger.warning(f"等待图像静止超时（{timeout}秒），继续后续识别")
    return False
