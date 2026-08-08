from __future__ import annotations

import re
import time
import json
from collections import Counter
from dataclasses import replace

import cv2 as cv
import numpy as np

from loguru import logger

from core.control.control import (
    connect,
    current_display_geometry,
    input_swipe,
    input_tap,
    screenshot,
)
from core.exception.exceptions import StopExecution
from core.preset import go_home
from core.preset.control import blurry_ocr_click
from core.services.screen_state import (
    is_inventory_item_detail,
    is_inventory_screen,
    is_train_in_transit as _is_train_in_transit,
)
from core.utils.utils import RESOURCES_PATH
from core.services.inventory_assets import (
    Asset,
    classify_asset,
    merge_assets,
    parse_amount,
    parse_ocr_assets,
)
from core.services.navigation_evidence import (
    CoordinateChain,
    NavigationAttemptEvidence,
    frame_sha256,
    record_navigation_attempt,
)
from core.services.read_only_policy import ActionIntent
from core.services.dispatch_outcome import physical_input_count_from_dispatch_error
from core.services.runtime_errors import BlockedBySafetyError
from core.services.home_backpack_cube import (
    HomeAssetsBalanceDisplay,
    HomeBackpackCubeCandidate,
    confirm_fresh_home_backpack_cube,
    find_home_backpack_cube_candidates,
    resolve_home_backpack_cube_control,
)
from core.services.runtime_navigation_kernel import UiState, normalize_legacy_state
from core.services.inventory_page_observer import InventoryPageState, observe_inventory_page


def _center(item: dict) -> tuple[float, float]:
    position = item["position"]
    return ((position[0][0] + position[2][0]) / 2, (position[0][1] + position[2][1]) / 2)


STATION_PRIMARY_CURRENCIES = {
    "武林源": "交子",
}
HOME_ASSETS_BALANCE_SEMANTICS = HomeAssetsBalanceDisplay()


def _home_primary_currency(items: list[dict]) -> list[Asset]:
    """Map the home-screen `资产` balance to the current station's currency."""
    label = next((item for item in items if item["text"].strip() == "资产"), None)
    if not label:
        return []
    screen_text = " ".join(str(item.get("text", "")).replace(" ", "") for item in items)
    currency_name = next(
        (
            currency
            for station, currency in STATION_PRIMARY_CURRENCIES.items()
            if station in screen_text
        ),
        "铁盟币",
    )
    x, y = _center(label)
    candidates = []
    for item in items:
        amount = parse_amount(item["text"])
        if amount is None or not item["text"].replace(",", "").isdigit():
            continue
        nx, ny = _center(item)
        if 0 <= nx - x <= 180 and abs(ny - y) <= 35:
            candidates.append((abs(nx - x) + abs(ny - y), amount))
    return [Asset(currency_name, min(candidates)[1], "货币")] if candidates else []


def _home_iron_currency(items: list[dict]) -> list[Asset]:
    """Backward-compatible wrapper for callers/tests using the old name."""
    return _home_primary_currency(items)


def _read_unicode_image(path, flags=cv.IMREAD_COLOR) -> cv.typing.MatLike | None:
    try:
        return cv.imdecode(np.fromfile(str(path), dtype=np.uint8), flags)
    except OSError:
        return None


INVENTORY_ICON_MIN_SCORE = 0.78
INVENTORY_ICON_AUTO_SCORE = 0.90
INVENTORY_ICON_MARGIN = 0.04
INVENTORY_ICON_CLUSTER_DISTANCE = 68


def _inventory_icon_manifest() -> dict[str, str]:
    path = RESOURCES_PATH / "currency" / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(name): str(filename)
        for name, filename in data.items()
        if isinstance(name, str) and isinstance(filename, str)
    }


def _match_inventory_template(image, template) -> tuple[tuple[int, int] | None, float]:
    """Return the best visible-grid match for one transparent or learned template."""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or template is None:
        return None, 0.0
    height, width = image.shape[:2]
    x1, x2 = int(width * 0.29), int(width * 0.85)
    y1, y2 = int(height * 0.10), int(height * 0.99)
    roi = image[y1:y2, x1:x2]
    has_alpha = template.ndim == 3 and template.shape[2] == 4
    if has_alpha:
        alpha = template[:, :, 3]
        ys, xs = np.where(alpha >= 32)
        if not len(xs):
            return None, 0.0
        source = template[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1, :3]
        source_mask = alpha[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    else:
        source = template[:, :, :3]
        source_mask = None

    best_score = 0.0
    best_center = None
    # The control layer normalizes inventory frames to 1280x720 and both the
    # shipped and learned icons are 96x96. A narrow scale band keeps a full
    # manifest scan practical while still tolerating UI animation/rounding.
    for scale_percent in range(90, 111, 5):
        scale = scale_percent / 100
        resized = cv.resize(source, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
        th, tw = resized.shape[:2]
        if th >= roi.shape[0] or tw >= roi.shape[1]:
            continue
        if source_mask is not None:
            mask = cv.resize(source_mask, (tw, th), interpolation=cv.INTER_NEAREST)
            scores = cv.matchTemplate(roi, resized, cv.TM_CCORR_NORMED, mask=mask)
        else:
            scores = cv.matchTemplate(roi, resized, cv.TM_CCOEFF_NORMED)
        scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
        _, score, _, location = cv.minMaxLoc(scores)
        if score > best_score:
            best_score = float(score)
            best_center = (x1 + location[0] + tw // 2, y1 + location[1] + th // 2)
    return best_center, best_score


def _match_inventory_icons(image) -> list[dict]:
    """Match every icon registered in the resource manifest on the current page."""
    icon_root = RESOURCES_PATH / "currency"
    matches = []
    for name, filename in _inventory_icon_manifest().items():
        template = _read_unicode_image(icon_root / filename, cv.IMREAD_UNCHANGED)
        location, score = _match_inventory_template(image, template)
        if location is not None and score >= INVENTORY_ICON_MIN_SCORE:
            matches.append({"name": name, "location": location, "score": score})
    return matches


def _cluster_inventory_icon_matches(matches: list[dict]) -> list[list[dict]]:
    """Group competing template names that point at the same inventory slot."""
    clusters: list[list[dict]] = []
    for match in sorted(matches, key=lambda item: item["score"], reverse=True):
        x, y = match["location"]
        cluster = next(
            (
                group
                for group in clusters
                if min(
                    (x - item["location"][0]) ** 2 + (y - item["location"][1]) ** 2
                    for item in group
                )
                <= INVENTORY_ICON_CLUSTER_DISTANCE**2
            ),
            None,
        )
        if cluster is None:
            clusters.append([match])
        else:
            cluster.append(match)
    return clusters


def _inventory_grid_numbers(items: list[dict], width=1280, height=720) -> list[dict]:
    """Locate quantity-bearing cells; unmatched cells become unknown-item probes."""
    numbers = []
    for item in items:
        raw = str(item.get("text", "")).replace(",", "").strip()
        if not re.fullmatch(r"(?:x|×)?\s*\d{1,8}", raw, re.IGNORECASE):
            continue
        x, y = _center(item)
        if width * 0.29 <= x <= width * 0.85 and height * 0.40 <= y <= height * 0.98:
            numbers.append({"location": (int(x), int(y - height * 0.055)), "count": _parse_count(raw), "raw": raw})
    return numbers


def _number_near_inventory_icon(numbers: list[dict], location: tuple[int, int]) -> dict | None:
    x, y = location
    candidates = [
        (abs(item["location"][0] - x) + abs(item["location"][1] - y), item)
        for item in numbers
        if abs(item["location"][0] - x) <= 75 and abs(item["location"][1] - y) <= 85
    ]
    return min(candidates, default=(0, None))[1]


def _index_inventory_icon_page(image, items: list[dict]) -> tuple[list[Asset], list[dict]]:
    """Resolve unambiguous icon/count pairs and return only uncertain cells for probing."""
    numbers = _inventory_grid_numbers(items, *image.shape[1::-1])
    assets = []
    probes = []
    claimed_numbers: set[int] = set()
    for cluster in _cluster_inventory_icon_matches(_match_inventory_icons(image)):
        ranked = sorted(cluster, key=lambda item: item["score"], reverse=True)
        best = ranked[0]
        number = _number_near_inventory_icon(numbers, best["location"])
        if number is not None:
            claimed_numbers.add(id(number))
        margin = best["score"] - ranked[1]["score"] if len(ranked) > 1 else 1.0
        if (
            best["score"] >= INVENTORY_ICON_AUTO_SCORE
            and margin >= INVENTORY_ICON_MARGIN
            and number is not None
            and number["count"] is not None
        ):
            assets.append(Asset(best["name"], number["count"], classify_asset(best["name"])))
            continue
        probes.append(
            {
                "location": best["location"],
                "candidates": tuple(item["name"] for item in ranked),
                "reason": "missing_count" if number is None else "template_conflict",
            }
        )
    for number in numbers:
        if id(number) not in claimed_numbers:
            probes.append(
                {
                    "location": number["location"],
                    "candidates": (),
                    "reason": "unknown_icon",
                }
            )
    return assets, probes


def _parse_primary_currency_grid(image, ocr_items: list[dict]) -> list[Asset]:
    """Read the stable currency slots on the first Assets page."""
    height, width = image.shape[:2]
    numbers = []
    for item in ocr_items:
        raw = item["text"].replace(",", "").strip()
        if raw.isdigit():
            numbers.append((*_center(item), int(raw)))
    # Count label centers observed on the normalized 1280x720 game canvas.
    slots = {
        "桦石": (0.495, 0.290),
        "交子": (0.570, 0.290),
        "铁盟币": (0.684, 0.290),
        "绝命奖章": (0.804, 0.290),
        "赴命奖章": (0.390, 0.480),
    }
    found = []
    for name, (x_ratio, y_ratio) in slots.items():
        tx, ty = width * x_ratio, height * y_ratio
        candidates = [
            (abs(x - tx) + abs(y - ty), amount)
            for x, y, amount in numbers
            if abs(x - tx) <= width * 0.045 and abs(y - ty) <= height * 0.035
        ]
        if candidates:
            amount = min(candidates)[1]
            logger.info(f"资产槽位识别：{name} {amount}")
            found.append(Asset(name, amount, "货币"))
    return found


AssetsEntryCandidate = HomeBackpackCubeCandidate


def _find_assets_entry_candidates(image) -> list[AssetsEntryCandidate]:
    """Backward-compatible name for the HOME backpack cube resolver."""

    return find_home_backpack_cube_candidates(image)


def _find_assets_entry(image) -> tuple[tuple[int, int] | None, float]:
    """Backward-compatible best-candidate view used by older callers/tests."""

    candidates = _find_assets_entry_candidates(image)
    if not candidates:
        return None, 0.0
    return candidates[0].point, candidates[0].score


def _find_assets_text_entry(items: list[dict], width: int, height: int):
    """Find the bottom-left balance label only as a station-home guard.

    The label itself is not an entry.  Older code clicked it and then scanned
    the unchanged station screen as though it were the backpack, producing
    false icon matches.
    """
    for item in items:
        if str(item.get("text", "")).replace(" ", "").strip() != "资产":
            continue
        x, y = _center(item)
        if x <= width * 0.35 and y >= height * 0.78:
            return int(x), int(y)
    return None


def _is_assets_inventory_screen(items: list[dict]) -> bool:
    """Require the backpack's right-hand category rail before scanning it."""
    return is_inventory_screen(items)


def _wait_for_assets_inventory(timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_assets_inventory_screen(screenshot().ocr()):
            return True
        time.sleep(0.4)
    return False


def _assets_runtime_state(frame) -> tuple[UiState, tuple[str, ...]]:
    frame_hash = frame_sha256(frame)
    capture_id = str(getattr(frame, "source_capture_id", "") or "")
    try:
        items = frame.ocr()
    except Exception:  # noqa: BLE001 - detector errors must be distinguished
        return (
            normalize_legacy_state(
                "DETECTOR_ERROR",
                phase="OPEN_INVENTORY",
                confidence="UNKNOWN",
                frame_hash=frame_hash,
                capture_id=capture_id,
            ),
            ("ocr_detector_error",),
        )
    height, width = frame.image.shape[:2]
    inventory_observation = observe_inventory_page(frame)
    if inventory_observation.state is InventoryPageState.INVENTORY_PAGE_VISIBLE:
        return (
            normalize_legacy_state(
                "INVENTORY_PAGE_VISIBLE",
                phase="OPEN_INVENTORY",
                evidence=("inventory_category_rail",),
                frame_hash=frame_hash,
                capture_id=capture_id,
            ),
            (),
        )
    if _find_assets_text_entry(items, width, height):
        home = normalize_legacy_state(
            "HOME_READY",
            phase="OPEN_INVENTORY",
            evidence=("home_iron_coin_balance_display",),
            frame_hash=frame_hash,
            capture_id=capture_id,
        )
        return (
            replace(home, capabilities=frozenset()),
            ("inventory_category_rail_absent",),
        )
    if items:
        return (
            normalize_legacy_state(
                "UNEXPECTED_PAGE",
                phase="OPEN_INVENTORY",
                confidence="UNKNOWN",
                frame_hash=frame_hash,
                capture_id=capture_id,
            ),
            ("inventory_category_rail_absent",),
        )
    return (
        normalize_legacy_state(
            "UNKNOWN",
            phase="OPEN_INVENTORY",
            confidence="UNKNOWN",
            frame_hash=frame_hash,
            capture_id=capture_id,
        ),
        ("no_page_cues",),
    )


def _assets_post_state(frame) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    state, negative = _assets_runtime_state(frame)
    return state.base_page, state.evidence, negative


def _open_assets_entry(
    *,
    frame_provider=None,
    dispatcher=None,
    candidate_resolver=None,
    geometry_provider=None,
    evidence_recorder=None,
    timeout: float = 6.0,
    poll_interval: float = 0.4,
    monotonic=None,
    sleep=None,
) -> bool:
    frame_provider = frame_provider or screenshot
    dispatcher = dispatcher or input_tap
    candidate_resolver = candidate_resolver or _find_assets_entry_candidates
    geometry_provider = geometry_provider or current_display_geometry
    evidence_recorder = evidence_recorder or record_navigation_attempt
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep

    image = frame_provider()
    height, width = image.image.shape[:2]
    items = image.ocr()
    if _is_assets_inventory_screen(items):
        return True
    # `资产` at bottom-left proves this is the station home, but is only a
    # balance display.  The actual backpack entry is the white cube in the
    # top-right toolbar.
    if not _find_assets_text_entry(items, width, height):
        logger.warning("当前画面未识别到站点主界面的资产余额，拒绝盲点背包入口")
        return False
    candidates = list(candidate_resolver(image.image))
    initial_control = resolve_home_backpack_cube_control(
        image,
        candidate_resolver=lambda _pixels: candidates,
    )
    if not initial_control.resolved:
        if initial_control.candidate_count <= 1:
            logger.error(
                "未解析出唯一 HOME 背包魔方控件；资产余额显示不提供点击权限"
            )
            return False
        candidate = candidates[0]
        geometry = geometry_provider()
        chain = CoordinateChain.from_capture_point(
            candidate.point,
            capture_size=(width, height),
            render_client_size=(int(geometry.physical_width), int(geometry.physical_height)),
            device_size=(int(geometry.physical_width), int(geometry.physical_height)),
            source_coordinate_space="CAPTURE_PIXELS",
        )
        blocked = NavigationAttemptEvidence(
            task_name="inventory_scan",
            entry_name="home_backpack_cube",
            pre_state="HOME_READY",
            pre_frame_sha256=frame_sha256(image),
            coordinate_chain=chain,
            candidate_type="home_backpack_cube_control",
            candidate_bbox=None,
            candidate_score=candidate.score,
            candidate_count=initial_control.candidate_count,
            dispatch_backend="device_control",
        )
        blocked.mark_dispatch(requested=False, acknowledged=False, result="blocked_ambiguous_candidates")
        blocked.add_post_observation(
            frame=image,
            state="HOME_READY",
            negative_cues=("multiple_backpack_cube_candidates",),
            reason_codes=("backpack_cube_ambiguous",),
            postcondition_result="FAIL",
        )
        evidence_recorder(blocked)
        logger.error(
            f"HOME 背包魔方候选不唯一（{initial_control.candidate_count}），拒绝点击"
        )
        return False

    if poll_interval > 0:
        sleep(min(poll_interval, 0.25))
    fresh_frame = frame_provider()
    fresh_height, fresh_width = fresh_frame.image.shape[:2]
    fresh_items = fresh_frame.ocr()
    if not _find_assets_text_entry(fresh_items, fresh_width, fresh_height):
        logger.warning("背包魔方 fresh HOME_READY 前置条件不再成立")
        return False
    fresh_candidates = list(candidate_resolver(fresh_frame.image))
    fresh_control = resolve_home_backpack_cube_control(
        fresh_frame,
        candidate_resolver=lambda _pixels: fresh_candidates,
    )
    confirmed_control = confirm_fresh_home_backpack_cube(
        initial_control,
        fresh_control,
    )
    if confirmed_control is None or confirmed_control.safe_hit_point is None:
        logger.error("背包魔方控件在两张 fresh HOME_READY 帧之间不稳定，输入为 0")
        return False
    point = confirmed_control.safe_hit_point
    image = fresh_frame
    height, width = fresh_height, fresh_width
    geometry = geometry_provider()
    chain = CoordinateChain.from_capture_point(
        point,
        capture_size=(width, height),
        render_client_size=(int(geometry.physical_width), int(geometry.physical_height)),
        device_size=(int(geometry.physical_width), int(geometry.physical_height)),
        source_coordinate_space="CAPTURE_PIXELS",
    )
    evidence = NavigationAttemptEvidence(
        task_name="inventory_scan",
        entry_name="home_backpack_cube",
        pre_state="HOME_READY",
        pre_frame_sha256=frame_sha256(image),
        coordinate_chain=chain,
        candidate_type="home_backpack_cube_control",
        candidate_bbox=confirmed_control.cube_bbox,
        candidate_score=confirmed_control.candidate_score,
        candidate_count=confirmed_control.candidate_count,
        dispatch_backend="device_control",
    )

    logger.info(
        f"背包魔方 attempt={evidence.attempt_id} capture_point={point} "
        f"device_point={chain.device_point} bbox={confirmed_control.cube_bbox} "
        f"score={confirmed_control.candidate_score:.3f}"
    )
    try:
        dispatch_result = dispatcher(
            point,
            random_offset=False,
            intent=ActionIntent(
                "open_inventory", "home_backpack_cube", evidence.attempt_id
            ),
        )
    except Exception as error:  # noqa: BLE001 - preserve evidence before fail-closed
        physical_input_count = physical_input_count_from_dispatch_error(error)
        evidence.mark_dispatch(
            requested=True,
            acknowledged=False,
            result=f"dispatch_exception:{type(error).__name__}",
        )
        evidence_recorder(evidence)
        logger.exception(
            f"背包魔方 dispatch 失败；physical_input_count={physical_input_count}"
        )
        return False
    acknowledged = bool(dispatch_result)
    evidence.mark_dispatch(
        requested=True,
        acknowledged=acknowledged,
        result="call_returned" if acknowledged else "dispatch_rejected",
    )
    if not acknowledged:
        evidence_recorder(evidence)
        logger.error("背包魔方 dispatch 未确认，停止且不重试")
        return False

    deadline = monotonic() + max(0.0, float(timeout))
    final_state = "UNKNOWN"
    while monotonic() < deadline:
        if poll_interval > 0:
            sleep(poll_interval)
        post_frame = frame_provider()
        state, positive, negative = _assets_post_state(post_frame)
        final_state = state
        passed = state == "INVENTORY_PAGE_VISIBLE"
        evidence.add_post_observation(
            frame=post_frame,
            state=state,
            positive_cues=positive,
            negative_cues=negative,
            reason_codes=(
                "backpack_visible"
                if passed
                else "unexpected_page"
                if state == "UNEXPECTED_PAGE"
                else "postcondition_pending"
            ,),
            postcondition_result="PASS" if passed else "PENDING",
        )
        if passed:
            evidence_recorder(evidence)
            logger.info("已确认进入背包（识别到右侧道具/材料分类栏）")
            return True
        if state == "UNEXPECTED_PAGE":
            break

    reason = (
        "postcondition_detector_error"
        if final_state == "DETECTOR_ERROR"
        else "home_unchanged"
        if final_state == "HOME_READY"
        else "unexpected_page"
        if final_state == "UNEXPECTED_PAGE"
        else "postcondition_unknown"
    )
    evidence.add_post_observation(
        frame=post_frame if "post_frame" in locals() else image,
        state=final_state,
        negative_cues=("inventory_category_rail_absent",),
        reason_codes=(reason,),
        postcondition_result="FAIL",
    )
    evidence_recorder(evidence)
    logger.error(f"背包魔方后置条件失败：{reason}；停止且不重试")
    return False


def _inventory_detail_asset(items: list[dict]) -> Asset | None:
    """Read a complete item name and owned count from a verified detail overlay."""
    if not is_inventory_item_detail(items):
        return None
    owned = next(
        (
            _parse_count(str(item.get("text", "")))
            for item in items
            if "拥有" in str(item.get("text", ""))
        ),
        None,
    )
    if owned is None:
        return None

    manifest_names = _inventory_icon_manifest()
    normalized = {
        name.replace(" ", ""): name
        for name in manifest_names
    }
    for item in items:
        compact = str(item.get("text", "")).replace(" ", "")
        if compact in normalized:
            name = normalized[compact]
            return Asset(name, owned, classify_asset(name))

    candidates = []
    for item in items:
        raw = str(item.get("text", "")).strip()
        compact = raw.replace(" ", "")
        if not compact or compact.isascii() or any(character.isdigit() for character in compact):
            continue
        x, y = _center(item)
        if 430 <= x <= 900 and 185 <= y <= 255 and not any(
            marker in compact
            for marker in ("拥有", "获取途径", "触碰空白区域退出", "使用", "出售")
        ):
            candidates.append((abs(y - 228), -len(compact), compact))
    if not candidates:
        return None
    name = min(candidates)[2]
    return Asset(name, owned, classify_asset(name))


def _confirm_inventory_detail_asset(initial_items: list[dict], frames=3):
    """Accept a detail result only when the same name/count appears twice."""
    readings = []
    detail_seen = False
    items = initial_items
    for frame in range(frames):
        detail_seen = detail_seen or is_inventory_item_detail(items)
        asset = _inventory_detail_asset(items)
        if asset is not None:
            readings.append(asset)
        if frame + 1 < frames:
            time.sleep(0.25)
            items = screenshot().ocr()
    if not readings:
        return None, detail_seen
    key, confirmations = Counter((item.name, item.count) for item in readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"背包物品详情多帧 OCR 不一致: {[(a.name, a.count) for a in readings]}")
        return None, detail_seen
    name, count = key
    return Asset(name, count, classify_asset(name)), detail_seen


def _safe_inventory_icon_filename(name: str, manifest: dict[str, str]) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "unknown-item"
    used = {filename.casefold() for filename in manifest.values()}
    filename = f"{stem}.png"
    suffix = 2
    while filename.casefold() in used:
        filename = f"{stem}-{suffix}.png"
        suffix += 1
    return filename


def _learn_inventory_icon(name: str, page_image, location: tuple[int, int]) -> bool:
    """Persist a confirmed unknown slot crop and atomically register it in the manifest."""
    manifest = _inventory_icon_manifest()
    if name in manifest or not isinstance(page_image, np.ndarray):
        return False
    x, y = location
    height, width = page_image.shape[:2]
    half = 48
    x1, x2 = max(0, x - half), min(width, x + half)
    y1, y2 = max(0, y - half), min(height, y + half)
    crop = page_image[y1:y2, x1:x2]
    if crop.shape[:2] != (96, 96):
        return False
    icon_root = RESOURCES_PATH / "currency"
    manifest_path = icon_root / "manifest.json"
    filename = _safe_inventory_icon_filename(name, manifest)
    stem = filename[:-4]
    suffix = 2
    while (icon_root / filename).exists():
        filename = f"{stem}-{suffix}.png"
        suffix += 1
    icon_path = icon_root / filename
    temp_manifest = manifest_path.with_suffix(".json.tmp")
    try:
        # Learned crops contain the live count near their bottom edge. Store an
        # alpha mask that excludes that band so a future quantity change does
        # not invalidate the icon template.
        learned = cv.cvtColor(crop, cv.COLOR_BGR2BGRA)
        learned[:, :, 3] = 0
        learned[:72, :, 3] = 255
        encoded, buffer = cv.imencode(".png", learned)
        if not encoded:
            return False
        buffer.tofile(str(icon_path))
        manifest[name] = filename
        temp_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_manifest.replace(manifest_path)
    except OSError:
        logger.exception(f"写入未知背包物品资源失败: {name}")
        try:
            if icon_path.exists() and name not in _inventory_icon_manifest():
                icon_path.unlink()
        except OSError:
            pass
        return False
    logger.info(f"已学习背包物品图标: {name} -> {filename}")
    return True


def _probe_inventory_item(page_image, probe: dict) -> Asset | None:
    """Open one uncertain slot, verify its detail, and learn unknown artwork."""
    location = probe["location"]
    input_tap(location)
    time.sleep(0.6)
    initial_items = screenshot().ocr()
    asset, detail_seen = _confirm_inventory_detail_asset(initial_items)
    if asset is not None and asset.name not in _inventory_icon_manifest():
        _learn_inventory_icon(asset.name, page_image, location)
    if detail_seen:
        input_tap((640, 600))
        time.sleep(0.4)
    return asset


def _deduplicate_inventory_probes(probes: list[dict]) -> list[dict]:
    unique = []
    for probe in sorted(probes, key=lambda item: (item["location"][1], item["location"][0])):
        x, y = probe["location"]
        if any((x - old["location"][0]) ** 2 + (y - old["location"][1]) ** 2 <= 55**2 for old in unique):
            continue
        unique.append(probe)
    return unique


def scan_inventory_assets(max_pages: int = 10) -> list[Asset]:
    """Build a full-page icon index, probing only ambiguous or unknown cells."""
    if not connect():
        raise BlockedBySafetyError("ADB 连接失败，请先在“ADB信息”中确认模拟器连接")
    initial_items = screenshot().ocr()
    if _is_train_in_transit(initial_items):
        raise BlockedBySafetyError("列车正在行驶，当前无法打开资产页面；请到站后重新扫描")
    should_restore_home = False
    try:
        if not go_home():
            raise BlockedBySafetyError("无法返回站点主画面；请确认列车已到站后重试")
        # The home screen contains many unrelated numbers; only retain known currency-like rows.
        home_items = screenshot().ocr()
        currencies = _home_primary_currency(home_items)
        currencies.extend(asset for asset in parse_ocr_assets(home_items) if asset.category == "货币")
        snapshots = [currencies]
        if not _open_assets_entry():
            raise BlockedBySafetyError(
                "未找到游戏主界面的资产入口图标；请停留在主界面后重试"
            )
        should_restore_home = True
        time.sleep(1.2)
        first_frame = screenshot()
        first_ocr = first_frame.ocr()
        snapshots.append(_parse_primary_currency_grid(first_frame.image, first_ocr))
        previous_signature = None
        unchanged_pages = 0
        for page in range(max_pages):
            page_frame = first_frame if page == 0 else screenshot()
            page_ocr = first_ocr if page == 0 else page_frame.ocr()
            signature = _inventory_page_signature(page_ocr)
            icon_assets, probes = _index_inventory_icon_page(page_frame.image, page_ocr)
            snapshots.append(icon_assets)
            current = parse_ocr_assets(page_ocr)
            snapshots.append(current)
            probes = _deduplicate_inventory_probes(probes)
            logger.info(
                f"扫描背包第 {page + 1}/{max_pages} 页："
                f"模板确认 {len(icon_assets)} 项，待详情复核 {len(probes)} 项"
            )
            for probe in probes:
                asset = _probe_inventory_item(page_frame.image, probe)
                if asset is not None:
                    snapshots.append([asset])
            if signature and signature == previous_signature:
                unchanged_pages += 1
            else:
                unchanged_pages = 0
            previous_signature = signature
            if unchanged_pages >= 2:
                logger.info("背包内容连续三次未变化，已到列表末页")
                break
            if page < max_pages - 1:
                input_swipe((930, 640), (930, 285), swipe_time=600)
                time.sleep(0.9)
        assets = merge_assets(*snapshots)
        logger.info(f"资产扫描完成：识别到 {len(assets)} 类物品")
        return assets
    finally:
        if should_restore_home:
            try:
                go_home()
            except Exception:
                logger.warning("资产扫描结束后返回主界面失败")


def _parse_count(text: str) -> int | None:
    match = re.search(r"(?:x|×)?\s*(\d{1,8})", text.replace(",", ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _is_restock_book_name(text: str) -> bool:
    compact = str(text).replace(" ", "")
    return "进货" in compact and "书" in compact and any(
        marker in compact for marker in ("采买", "采购")
    )


def _restock_book_item(items: list[dict]) -> dict | None:
    return next(
        (item for item in items if _is_restock_book_name(item.get("text", ""))),
        None,
    )


def _find_restock_book_icon(image) -> tuple[tuple[int, int] | None, float]:
    """Locate the restock-book artwork in the item grid.

    Item names are hidden until an icon is opened, so OCR-only scanning can
    never discover the book from the grid.  The shipped transparent icon is
    matched with its alpha mask across the visible item area instead.
    """
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None, 0.0
    template = _read_unicode_image(
        RESOURCES_PATH / "currency" / "进货采买书.png", cv.IMREAD_UNCHANGED
    )
    if template is None or template.ndim != 3 or template.shape[2] != 4:
        return None, 0.0

    alpha = template[:, :, 3]
    ys, xs = np.where(alpha >= 32)
    if not len(xs):
        return None, 0.0
    template_bgr = template[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1, :3]
    template_mask = alpha[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]

    height, width = image.shape[:2]
    x1, x2 = int(width * 0.29), int(width * 0.85)
    y1, y2 = int(height * 0.10), int(height * 0.99)
    roi = image[y1:y2, x1:x2]
    best_score = 0.0
    best_center = None
    for scale_percent in range(55, 131, 5):
        scale = scale_percent / 100
        resized = cv.resize(template_bgr, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
        mask = cv.resize(template_mask, (resized.shape[1], resized.shape[0]), interpolation=cv.INTER_NEAREST)
        th, tw = resized.shape[:2]
        if th >= roi.shape[0] or tw >= roi.shape[1]:
            continue
        scores = cv.matchTemplate(roi, resized, cv.TM_CCORR_NORMED, mask=mask)
        scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
        _, score, _, location = cv.minMaxLoc(scores)
        if score > best_score:
            best_score = score
            best_center = (x1 + location[0] + tw // 2, y1 + location[1] + th // 2)
    return (best_center if best_score >= 0.78 else None), best_score


def _restock_book_count_near_icon(
    items: list[dict], location: tuple[int, int]
) -> tuple[int, str] | None:
    """Pair an icon only with the count directly beneath its own grid cell."""
    x, y = location
    candidates = []
    for item in items:
        raw = str(item.get("text", "")).strip()
        count = _parse_count(raw)
        if count is None or not re.fullmatch(r"(?:x|×)?\s*\d{1,5}", raw, re.IGNORECASE):
            continue
        nx, ny = _center(item)
        if abs(nx - x) <= 70 and 15 <= ny - y <= 105:
            candidates.append((abs(nx - x) + abs(ny - y) * 0.5, count, raw))
    if not candidates:
        return None
    _, count, raw = min(candidates)
    return count, raw


def _confirm_restock_book_icon_count(initial_items, location, frames=3):
    readings = []
    raw_values = []
    items = initial_items
    for frame in range(frames):
        result = _restock_book_count_near_icon(items, location)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            items = screenshot().ocr()
    if not readings:
        return None
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书图标下方数量多帧 OCR 不一致: {readings}")
        return None
    return count, ", ".join(raw_values)


def _restock_book_count_from_items(items: list[dict]) -> tuple[int, str] | None:
    """Pair the restock-book label only with a nearby/inline quantity."""
    for asset in parse_ocr_assets(items):
        if _is_restock_book_name(asset.name):
            return asset.count, asset.name

    book = _restock_book_item(items)
    if not book:
        return None
    # The opened detail card renders the canonical name on the left and
    # `拥有：N` on the upper-right, much farther away than a normal grid pair.
    for item in items:
        raw = str(item.get("text", ""))
        if "拥有" in raw and (count := _parse_count(raw)) is not None:
            return count, raw
    x, y = _center(book)
    candidates = []
    for item in items:
        count = _parse_count(str(item.get("text", "")))
        if count is None:
            continue
        nx, ny = _center(item)
        if abs(nx - x) <= 150 and -35 <= ny - y <= 170:
            candidates.append((abs(nx - x) + abs(ny - y) * 0.7, count, item["text"]))
    if not candidates:
        return None
    _, count, raw = min(candidates)
    return count, raw


def _confirm_restock_book_count(initial_items: list[dict], frames=3):
    readings = []
    raw_values = []
    items = initial_items
    for frame in range(frames):
        result = _restock_book_count_from_items(items)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            page_frame = screenshot()
            items = page_frame.ocr()
    if not readings:
        return None
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书数量多帧 OCR 不一致: {readings}")
        return None
    return count, ", ".join(raw_values)


def _restock_book_detail_count(items: list[dict]) -> tuple[int, str] | None:
    """Read only the canonical restock-book name and its owned count from a detail card."""
    if not is_inventory_item_detail(items) or not _restock_book_item(items):
        return None
    for item in items:
        raw = str(item.get("text", ""))
        if "拥有" in raw and (count := _parse_count(raw)) is not None:
            return count, raw
    return None


def _confirm_restock_book_detail_count(initial_items: list[dict], frames=3):
    """Require the exact detail identity and owned count in at least two frames."""
    readings = []
    raw_values = []
    detail_seen = False
    items = initial_items
    for frame in range(frames):
        detail_seen = detail_seen or is_inventory_item_detail(items)
        result = _restock_book_detail_count(items)
        if result:
            count, raw = result
            readings.append(count)
            raw_values.append(str(raw))
        if frame + 1 < frames:
            time.sleep(0.25)
            items = screenshot().ocr()
    if not readings:
        return None, detail_seen
    count, confirmations = Counter(readings).most_common(1)[0]
    if confirmations < 2:
        logger.warning(f"进货书详情数量多帧 OCR 不一致: {readings}")
        return None, detail_seen
    return (count, ", ".join(raw_values)), detail_seen


def _inventory_page_signature(items: list[dict]) -> tuple[str, ...]:
    """Stable text signature used to detect the bottom of the scroll list."""
    return tuple(sorted(str(item.get("text", "")).replace(" ", "") for item in items))


def read_restock_book_count(max_pages: int = 10) -> int | None:
    """Best-effort inventory read; return None instead of blocking trading."""
    stopped = False
    should_restore_home = False
    try:
        if not connect():
            logger.warning("ADB 连接失败，无法读取背包进货书")
            return None
        if _is_train_in_transit(screenshot().ocr()):
            logger.info("列车正在行驶，跳过进货书背包扫描，交给跑商恢复流程等待到站")
            return None
        if not go_home():
            return None
        should_restore_home = True
        if not _open_assets_entry():
            logger.warning("未找到背包入口，将使用界面填写的进货书库存")
            return None
        time.sleep(1.2)
        previous_signature = None
        unchanged_pages = 0
        for page in range(1, max_pages + 1):
            page_frame = screenshot()
            items = page_frame.ocr()
            signature = _inventory_page_signature(items)
            logger.info(f"扫描背包第 {page}/{max_pages} 页")

            book = _restock_book_item(items)
            if book:
                logger.info(f"在背包第 {page} 页识别到进货采买书，开始多帧核对数量")
                confirmed = _confirm_restock_book_count(items)
                if not confirmed:
                    x, y = _center(book)
                    input_tap((int(x), int(y)))
                    time.sleep(0.6)
                    confirmed = _confirm_restock_book_count(screenshot().ocr())
                if confirmed:
                    count, raw = confirmed
                    logger.info(f"背包进货书数量: {count}（多帧 OCR: {raw}）")
                    return count
                logger.warning("已找到进货采买书，但多帧数量核对失败，继续扫描")
            else:
                icon, score = _find_restock_book_icon(getattr(page_frame, "image", None))
                if icon:
                    logger.info(
                        f"在背包第 {page} 页识别到进货采买书图标 {icon}"
                        f"（匹配度 {score:.3f}），开始多帧核对数量"
                    )
                    grid_confirmed = _confirm_restock_book_icon_count(items, icon)
                    # A high-confidence icon may still have no OCR-readable grid
                    # count. Open its detail card so the exact item name and
                    # `拥有：N` can provide an independent multi-frame result.
                    if grid_confirmed or score >= 0.90:
                        input_tap(icon)
                        time.sleep(0.6)
                        detail_items = screenshot().ocr()
                        detail, detail_seen = _confirm_restock_book_detail_count(detail_items)
                        if detail and (
                            grid_confirmed is None or detail[0] == grid_confirmed[0]
                        ):
                            count, detail_raw = detail
                            grid_raw = grid_confirmed[1] if grid_confirmed else "未识别"
                            logger.info(
                                f"背包进货书数量: {count}（格位多帧 OCR: {grid_raw}；"
                                f"详情多帧 OCR: {detail_raw}）"
                            )
                            return count
                        logger.warning(
                            "疑似进货书图标的详情名称/拥有数量未通过多帧确认，继续扫描"
                        )
                        if detail_seen:
                            input_tap((640, 600))
                            time.sleep(0.4)
                    else:
                        logger.warning(
                            f"疑似进货书图标匹配度 {score:.3f}，但下方数量未通过多帧核对"
                        )

            if signature and signature == previous_signature:
                unchanged_pages += 1
            else:
                unchanged_pages = 0
            previous_signature = signature
            if unchanged_pages >= 2:
                logger.info("背包内容连续三次未变化，已到列表末页")
                break
            if page < max_pages:
                input_swipe((930, 640), (930, 285), swipe_time=600)
                time.sleep(0.9)

        logger.warning(f"已翻查背包 {min(page, max_pages)} 页，仍未确认进货采买书数量")
        return None
    except StopExecution:
        stopped = True
        raise
    except Exception:
        logger.exception("读取背包进货书失败，将使用界面填写值")
        return None
    finally:
        if should_restore_home and not stopped:
            try:
                go_home()
            except StopExecution:
                raise
            except Exception:
                pass
