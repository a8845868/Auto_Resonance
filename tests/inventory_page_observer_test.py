from core.services.inventory_page_observer import InventoryPageState, observe_inventory_page
from core.services.screen_state import is_inventory_item_detail, is_inventory_screen


def _items(*texts):
    return [{"text": text, "position": [[0, 0], [1, 0], [1, 1], [0, 1]]} for text in texts]


def test_inventory_entry_and_recovery_share_authoritative_page_classification():
    items = _items("道具", "材料", "装备", "载货")
    observed = observe_inventory_page(items)
    assert observed.state is InventoryPageState.INVENTORY_PAGE_VISIBLE
    assert is_inventory_screen(items) is True


def test_inventory_detail_is_distinct_from_startup_overlay():
    detail = _items("触碰空白区域退出", "拥有：12", "获取途径")
    startup = _items("触碰空白区域退出", "公告")
    assert observe_inventory_page(detail).state is InventoryPageState.INVENTORY_DETAIL_VISIBLE
    assert is_inventory_item_detail(detail) is True
    assert observe_inventory_page(startup).state is InventoryPageState.NOT_INVENTORY
