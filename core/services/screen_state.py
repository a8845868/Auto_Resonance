"""Small, side-effect-free classifiers for top-level game screen state."""

from enum import Enum


# The control layer normalizes every frame to 1280x720.  This point is the
# middle of the wide blue confirmation button on the pre-login resource-pack
# prompt, not merely the OCR text bounding box.
RESOURCE_DOWNLOAD_CONFIRM_TAP = (640, 506)
# Ordinary navigation keeps its existing 45-attempt limit.  Only after this
# prompt is observed do callers grant roughly five minutes for downloading.
RESOURCE_DOWNLOAD_WAIT_ATTEMPTS = 150
CLARITY_REPLENISH_CANCEL_TAP = (350, 509)


def _texts(items: list[dict]) -> list[str]:
    return [str(item.get("text", "")).replace(" ", "") for item in items]


def _center(item: dict) -> tuple[int, int] | None:
    points = item.get("position") or []
    if len(points) < 3:
        return None
    return (
        int((points[0][0] + points[2][0]) / 2),
        int((points[0][1] + points[2][1]) / 2),
    )


class ResidentHomeState(str, Enum):
    HOME_READY = "HOME_READY"
    ANNOUNCEMENT_OVERLAY = "ANNOUNCEMENT_OVERLAY"
    CHECKIN_OVERLAY = "CHECKIN_OVERLAY"
    UNKNOWN_OVERLAY = "UNKNOWN_OVERLAY"


def resident_home_state(items: list[dict]) -> ResidentHomeState | None:
    """Classify resident-activity home/overlay state without clicking it."""

    if is_inventory_item_detail(items):
        return None
    texts = _texts(items)
    joined = "|".join(texts)
    if any(marker in joined for marker in ("访问城市", "作战终端", "启程")):
        return ResidentHomeState.HOME_READY
    if any(marker in joined for marker in ("公告", "资讯")):
        return ResidentHomeState.ANNOUNCEMENT_OVERLAY
    if any(marker in joined for marker in ("每日签到奖励", "签到奖励", "签到")):
        return ResidentHomeState.CHECKIN_OVERLAY
    if any("触碰空白区域退出" in text for text in texts):
        return ResidentHomeState.UNKNOWN_OVERLAY
    return None


def clarity_replenish_cancel_position(
    items: list[dict],
) -> tuple[int, int] | None:
    """Return a guarded cancel position for the clarity replenish prompt."""
    texts = _texts(items)
    if not any(
        "澄明度不足" in text or "是否补充澄明度" in text for text in texts
    ):
        return None
    for item, text in zip(items, texts):
        if text == "取消":
            return _center(item) or CLARITY_REPLENISH_CANCEL_TAP
    # Normalized 1280x720 fallback, permitted only after the prompt guard.
    return CLARITY_REPLENISH_CANCEL_TAP


def is_train_in_transit(items: list[dict]) -> bool:
    """Detect the driving HUD, where station-only menus are unavailable."""
    texts = _texts(items)
    if any(marker in text for marker in ("自动巡航", "剩余行程") for text in texts):
        return True
    has_destination = any("目的地" in text for text in texts)
    has_carriage = any(
        marker in text for text in texts for marker in ("车厢内", "副官室")
    )
    return has_destination and has_carriage


def is_top_level_hud(items: list[dict]) -> bool:
    """Recognize the train/station HUD without one version-specific pixel."""
    texts = _texts(items)
    markers = ("资产", "车厢内", "副官室")
    matched = sum(any(marker in text for text in texts) for marker in markers)
    return matched >= 2


def is_inventory_screen(items: list[dict]) -> bool:
    """Recognize the backpack list by its right-hand category rail."""
    texts = _texts(items)
    categories = (
        "道具",
        "材料",
        "装备",
        "载货",
        "冰箱",
        "武装",
        "凭钥柜",
        "信物",
        "私人仓库",
    )
    matched = sum(any(category in text for text in texts) for category in categories)
    return any("道具" in text for text in texts) and matched >= 3


def is_inventory_item_detail(items: list[dict]) -> bool:
    """Separate an item detail overlay from the visually similar startup overlay."""
    texts = _texts(items)
    has_blank_exit = any("触碰空白区域退出" in text for text in texts)
    has_item_metadata = any(
        marker in text
        for text in texts
        for marker in ("拥有:", "拥有：", "获取途径")
    )
    return has_blank_exit and has_item_metadata


def startup_screen_action(items: list[dict]) -> str | None:
    """Return the only safe action for a game startup/login screen.

    The login page's top-left resource-repair button overlaps the normal
    in-game back-button area.  Callers must handle these states before they
    attempt ordinary ``go_home`` navigation.
    """
    texts = _texts(items)
    # Item details use the same "touch blank area to exit" wording as one
    # startup overlay.  Treating a backpack detail as startup makes go_home()
    # enter a sticky wait loop after the detail is closed.
    if is_inventory_item_detail(items):
        return None
    if any("修复资源完整性" in text for text in texts):
        return "cancel_resource_repair"
    has_download_prompt = any(
        "需要下载资源包" in text
        or ("下载" in text and "资源包" in text)
        for text in texts
    )
    has_confirm_button = any("确认" in text for text in texts)
    if has_download_prompt and has_confirm_button:
        return "confirm_resource_download"
    if any(
        marker in text
        for text in texts
        for marker in ("点击屏幕进入游戏", "点击任意位置进入游戏")
    ):
        return "enter_game"
    if any("触碰空白区域退出" in text for text in texts):
        return "dismiss_startup_overlay"
    if any(any(marker in text for marker in ("正在加载", "加载中")) for text in texts):
        return "wait_for_game"
    # Trade prices (for example 112%) and other ordinary in-game statistics
    # must never be mistaken for the login loading percentage.  The actual
    # loading screen exposes very little other OCR content.
    gameplay_markers = (
        "交易品",
        "全部买入",
        "我要买",
        "我要卖",
        "交易所",
        "便当柜",
        "恢复疲劳值方式",
        "FATIGUE",
        "编组",
        "建造车厢",
        "列车总览",
    )
    if not any(marker in text for text in texts for marker in gameplay_markers) and any(
        "%" in text and any(character.isdigit() for character in text)
        for text in texts
    ):
        return "wait_for_game"
    return None
