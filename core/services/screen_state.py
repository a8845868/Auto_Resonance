"""Small, side-effect-free classifiers for top-level game screen state."""

from enum import Enum

from core.services.page_templates import (
    HOME_TEMPLATE_PATH,
    HOME_TEMPLATE_ROI,
    _load_template,
    match_page_template,
)


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


def resident_home_state(
    items: list[dict], *, frame_img=None
) -> ResidentHomeState | None:
    """Classify resident-activity home/overlay state without clicking it."""

    if match_page_template(
        frame_img, _load_template(HOME_TEMPLATE_PATH), HOME_TEMPLATE_ROI
    ):
        return ResidentHomeState.HOME_READY
    if is_inventory_item_detail(items):
        return None
    texts = _texts(items)
    joined = "|".join(texts)
    home_markers = ("访问城市", "作战终端", "启程")
    if sum(marker in joined for marker in home_markers) >= 2:
        return ResidentHomeState.HOME_READY
    shop_markers = (
        "特惠礼包", "总部商店", "黑月商店", "赴命商店", "EXCHANGESTATION",
    )
    confirmed_shop_page = any(marker in joined for marker in shop_markers)
    has_overlay_exit = any("触碰空白区域退出" in text for text in texts)
    if (
        not confirmed_shop_page
        and has_overlay_exit
        and any(marker in joined for marker in ("公告", "资讯"))
    ):
        return ResidentHomeState.ANNOUNCEMENT_OVERLAY
    if (
        not confirmed_shop_page
        and has_overlay_exit
        and any(marker in joined for marker in ("每日签到奖励", "签到奖励"))
    ):
        return ResidentHomeState.CHECKIN_OVERLAY
    if has_overlay_exit:
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
    has_auto_cruise = any("自动巡航" in text for text in texts)
    has_remaining_trip = any("剩余行程" in text for text in texts)
    has_destination = any("目的地" in text for text in texts)
    if sum((has_auto_cruise, has_remaining_trip, has_destination)) >= 2:
        return True
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
    from core.services.inventory_page_observer import InventoryPageState, observe_inventory_page

    return observe_inventory_page(items).state is InventoryPageState.INVENTORY_PAGE_VISIBLE


def is_inventory_item_detail(items: list[dict]) -> bool:
    """Separate an item detail overlay from the visually similar startup overlay."""
    from core.services.inventory_page_observer import InventoryPageState, observe_inventory_page

    return observe_inventory_page(items).state is InventoryPageState.INVENTORY_DETAIL_VISIBLE


def startup_screen_action(items: list[dict], *, frame_img=None) -> str | None:
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
        "特惠礼包",
        "总部商店",
        "黑月商店",
        "赴命商店",
        "EXCHANGESTATION",
        "访问城市",
        "作战终端",
        "启程",
        "整备列车",
        "城市发展度",
        "城市设施",
        "城市手册",
        "商会",
        "休息区",
        "资产",
        "车厢内",
        "副官室",
        "行动汇总",
    )
    gameplay_context = any(
        marker in text for text in texts for marker in gameplay_markers
    )
    if not gameplay_context and any(
        any(marker in text for marker in ("正在加载", "加载中"))
        for text in texts
    ):
        return "wait_for_game"
    if not gameplay_context and any(
        "%" in text and any(character.isdigit() for character in text)
        for text in texts
    ):
        return "wait_for_game"
    return None
