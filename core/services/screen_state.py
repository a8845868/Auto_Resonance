"""Small, side-effect-free classifiers for top-level game screen state."""


# The control layer normalizes every frame to 1280x720.  This point is the
# middle of the wide blue confirmation button on the pre-login resource-pack
# prompt, not merely the OCR text bounding box.
RESOURCE_DOWNLOAD_CONFIRM_TAP = (640, 506)
# Ordinary navigation keeps its existing 45-attempt limit.  Only after this
# prompt is observed do callers grant roughly five minutes for downloading.
RESOURCE_DOWNLOAD_WAIT_ATTEMPTS = 150


def _texts(items: list[dict]) -> list[str]:
    return [str(item.get("text", "")).replace(" ", "") for item in items]


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
