import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSizePolicy

import app.view.shop_planner_interface as shop_interface
import auto.shop_purchase as shop_purchase
from core.services.shop_catalog import load_shop_catalog


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _DryRunWorker:
    instances = []

    def __init__(self, parent):
        self.parent = parent
        self.succeeded = _Signal()
        self.failed = _Signal()
        self.finished = _Signal()
        self.started = False
        self.deleted = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return self.started

    def start(self):
        self.started = True

    def deleteLater(self):
        self.deleted = True


def test_dry_run_entry_calls_shop_runtime_with_dry_run_true(monkeypatch):
    calls = []
    expected = {"success": True, "dry_run": True, "shops": []}
    monkeypatch.setattr(
        shop_purchase,
        "run_shop_purchase",
        lambda *, dry_run: calls.append(dry_run) or expected,
    )

    assert shop_interface._run_shop_dry_run() is expected
    assert calls == [True]


def test_shop_page_starts_independent_dry_run_worker(monkeypatch):
    application = QApplication.instance() or QApplication([])
    _DryRunWorker.instances.clear()
    monkeypatch.setattr(shop_interface, "ShopDryRunWorker", _DryRunWorker)
    page = shop_interface.ShopPlannerInterface()

    page.startDryRun()

    worker = _DryRunWorker.instances[-1]
    assert worker.parent is page
    assert worker.started is True
    assert page.dryRunWorker is worker
    assert page.dryRunButton.isEnabled() is False
    assert page.dryRunButton.text() == "正在扫描商店…"
    assert worker.succeeded.callback == page._dryRunSucceeded
    assert worker.failed.callback == page._dryRunFailed
    assert worker.finished.callback == page._dryRunFinished
    assert application is not None
    page.deleteLater()


def test_shop_dry_run_bypasses_attempt_lock_and_never_records_purchase(monkeypatch):
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    shop = catalog.shop(item.shop_id)
    purchase = shop_purchase.ConfiguredPurchase(shop, item, "one")
    calls = []

    class _Adapter:
        def open(self):
            calls.append("open")

        def scan(self, purchases, *, dry_run):
            calls.append((tuple(purchases), dry_run))
            return {
                "success": True,
                "requires_attention": False,
                "pages": 1,
                "results": [],
                "missing": [],
            }

    monkeypatch.setattr(shop_purchase, "load_shop_catalog", lambda: catalog)
    monkeypatch.setattr(
        shop_purchase,
        "load_shop_plan",
        lambda *, catalog: {"capture_evidence": False},
    )
    monkeypatch.setattr(
        shop_purchase,
        "configured_purchases",
        lambda plan, loaded_catalog: [purchase],
    )
    monkeypatch.setattr(
        shop_purchase,
        "active_shop_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run queried the purchase lock")
        ),
    )
    monkeypatch.setattr(
        shop_purchase,
        "record_shop_attempt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run wrote the purchase ledger")
        ),
    )
    monkeypatch.setattr(
        shop_purchase,
        "ShopEvidenceRecorder",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setitem(
        shop_purchase.ADAPTERS,
        shop.adapter,
        lambda _shop, _recorder: _Adapter(),
    )
    monkeypatch.setattr(shop_purchase, "_connected_run", lambda callback: callback())

    result = shop_purchase.run_shop_purchase(dry_run=True)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["blocked_by_period"] == []
    assert result["completed_at"]
    assert result["result_file"] == ""
    assert calls == ["open", ((purchase,), True)]


def test_shop_dry_run_summary_surfaces_attention_without_claiming_purchase():
    summary = shop_interface._shop_dry_run_summary(
        {
            "success": False,
            "dry_run": True,
            "requires_attention": True,
            "shops": [
                {
                    "pages": 4,
                    "results": [{"status": "dry_run"}, {"status": "dry_run"}],
                    "missing": [{"id": "unknown"}],
                }
            ],
        }
    )

    assert summary == "干跑完成：扫描 4 页，处理 2 项，未定位 1 项，需要人工复核"
    assert "购买成功" not in summary


def test_stepped_item_card_shows_marginal_and_cumulative_price_details():
    application = QApplication.instance() or QApplication([])
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    currency = catalog.currencies[item.currency]

    text = shop_interface._price_breakdown_text(item, currency)
    options = shop_interface._quantity_options(item, currency)

    assert "1 件  ｜  边际 100,000  ｜  累计 100,000" in text
    assert "2 件  ｜  边际 200,000  ｜  累计 300,000" in text
    assert "3 件  ｜  价格未采集（不可作为累计目标）" in text
    assert text.startswith("目录价格档位（铁盟币）\n")
    assert "  ·  " not in text
    assert options == (
        ("one", "仅买 1 件（当前档位）"),
        ("max", "买完剩余（实时总价）"),
    )
    assert application is not None


def test_fixed_price_item_keeps_simple_quantity_options():
    catalog = load_shop_catalog()
    item = catalog.item("self_observation_daily_iron")
    currency = catalog.currencies[item.currency]

    assert shop_interface._price_breakdown_text(item, currency) == ""
    assert shop_interface._quantity_options(item, currency) == shop_interface.QUANTITY_OPTIONS


def test_observed_price_breakdown_formats_live_marginal_and_cumulative_costs():
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    currency = catalog.currencies[item.currency]

    text = shop_interface._observed_price_breakdown_text(
        [
            {"quantity": 1, "marginal_cost": 200000, "cumulative_cost": 200000},
            {"quantity": 2, "marginal_cost": 200000, "cumulative_cost": 400000},
        ],
        currency,
    )

    assert text == (
        "本次实机只读观察（铁盟币）\n"
        "1 件  ｜  边际 200,000  ｜  累计 200,000\n"
        "2 件  ｜  边际 200,000  ｜  累计 400,000"
    )


def test_stepped_card_can_show_read_only_observed_price_schedule():
    application = QApplication.instance() or QApplication([])
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    currency = catalog.currencies[item.currency]
    card = shop_interface.ShopItemCard(
        item,
        currency,
        {"enabled": False, "quantity": "one"},
    )

    card.setObservedPriceSchedule([
        {"quantity": 1, "marginal_cost": 200000, "cumulative_cost": 200000},
        {"quantity": 2, "marginal_cost": 200000, "cumulative_cost": 400000},
    ])

    assert card.observedPriceLabel.isVisible() is False  # parent is not shown
    assert "2 件  ｜  边际 200,000  ｜  累计 400,000" in (
        card.observedPriceLabel.text()
    )
    assert card.catalogPriceLabel.text().startswith("目录价格档位（铁盟币）\n")
    assert card.observedPriceLabel.text().startswith("本次实机只读观察（铁盟币）\n")
    assert card.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Preferred
    assert card.minimumHeight() > card._baseMinimumHeight
    assert application is not None
    card.deleteLater()


def test_observed_schedule_survives_shop_card_rebuild_in_same_gui_session():
    application = QApplication.instance() or QApplication([])
    page = shop_interface.ShopPlannerInterface()
    item_id = "laplace_weekly_iron"
    observations = [
        {"quantity": 1, "marginal_cost": 200000, "cumulative_cost": 200000},
        {"quantity": 2, "marginal_cost": 200000, "cumulative_cost": 400000},
    ]
    page.observedPriceSchedules[item_id] = observations

    page.showShop("headquarters_black_moon")

    assert "2 件  ｜  边际 200,000  ｜  累计 400,000" in (
        page.itemCards[item_id].observedPriceLabel.text()
    )
    assert application is not None
    page.deleteLater()


def test_shop_dry_run_summary_counts_failed_items():
    result = {
        "success": False,
        "dry_run": True,
        "requires_attention": True,
        "shops": [
            {
                "pages": 5,
                "results": [
                    {"id": "ok-1", "status": "validated"},
                    {"id": "fail-1", "status": "failed", "error": "价格校验失败"},
                    {"id": "ok-2", "status": "validated"},
                    {"id": "fail-2", "status": "failed", "error": "数量校验失败"},
                ],
                "missing": [{"id": "unlocated"}],
            }
        ],
    }

    summary = shop_interface._shop_dry_run_summary(result)

    assert "扫描 5 页" in summary
    assert "处理 4 项" in summary
    assert "2 项失败" in summary
    assert "未定位 1 项" in summary
    assert "需要人工复核" in summary


def test_shop_dry_run_diagnostics_shows_time_failed_missing_and_result_file():
    details = shop_interface._shop_dry_run_diagnostics({
        "completed_at": "2026-08-10T10:19:53+08:00",
        "result_file": "logs/shop_purchase/run/FINAL_RESULT.json",
        "shops": [{
            "results": [{
                "id": "laplace_monthly_iron",
                "name": "拉普拉斯协议",
                "status": "failed",
                "error": "商品数量只读探测结果不可信",
            }],
            "missing": [{
                "id": "nebula_4_daily_iron",
                "name": "星云物质（4钛）",
            }],
        }],
    })

    assert "完成时间：2026-08-10 10:19:53" in details
    assert "失败商品：拉普拉斯协议（laplace_monthly_iron）" in details
    assert "商品数量只读探测结果不可信" in details
    assert "未定位商品：星云物质（4钛）（nebula_4_daily_iron）" in details
    assert "结果文件：logs/shop_purchase/run/FINAL_RESULT.json" in details


def test_shop_dry_run_details_style_distinguishes_success_attention_and_error():
    application = QApplication.instance() or QApplication([])
    page = shop_interface.ShopPlannerInterface()

    page._dryRunSucceeded({
        "success": True,
        "requires_attention": False,
        "completed_at": "2026-08-11T13:03:43+08:00",
        "shops": [{"pages": 10, "results": [], "missing": []}],
    })
    assert "#76c893" in page.dryRunDetails.styleSheet()
    assert "#ff7b7b" not in page.dryRunDetails.styleSheet()

    page._dryRunSucceeded({
        "success": False,
        "requires_attention": True,
        "completed_at": "2026-08-11T13:04:00+08:00",
        "shops": [{"pages": 10, "results": [], "missing": []}],
    })
    assert "#f2c66d" in page.dryRunDetails.styleSheet()
    assert "#76c893" not in page.dryRunDetails.styleSheet()

    page._dryRunFailed("BlockedBySafetyError: test")
    assert "#ff7b7b" in page.dryRunDetails.styleSheet()
    assert "#f2c66d" not in page.dryRunDetails.styleSheet()


def test_shop_page_keeps_failed_and_missing_reasons_visible_after_rebuild():
    application = QApplication.instance() or QApplication([])
    page = shop_interface.ShopPlannerInterface()
    result = {
        "success": False,
        "dry_run": True,
        "requires_attention": True,
        "completed_at": "2026-08-10T10:19:53+08:00",
        "result_file": "logs/shop_purchase/run/FINAL_RESULT.json",
        "shops": [{
            "pages": 7,
            "results": [{
                "id": "laplace_monthly_iron",
                "name": "拉普拉斯协议",
                "status": "failed",
                "error": "商品数量只读探测结果不可信",
            }],
            "missing": [{
                "id": "nebula_4_daily_iron",
                "name": "星云物质（4钛）",
            }],
        }],
    }

    page._dryRunSucceeded(result)

    assert "失败商品：拉普拉斯协议" in page.dryRunDetails.text()
    assert "未定位商品：星云物质（4钛）" in page.dryRunDetails.text()
    assert "商品数量只读探测结果不可信" in (
        page.itemCards["laplace_monthly_iron"].failureLabel.text()
    )
    assert "未定位" in page.itemCards["nebula_4_daily_iron"].failureLabel.text()

    page.showShop("bureau_exchange")
    page.showShop("headquarters_black_moon")

    assert "商品数量只读探测结果不可信" in (
        page.itemCards["laplace_monthly_iron"].failureLabel.text()
    )
    assert "未定位" in page.itemCards["nebula_4_daily_iron"].failureLabel.text()
    assert application is not None
    page.deleteLater()


def test_failed_item_shows_error_in_card_without_claiming_observed_price():
    application = QApplication.instance() or QApplication([])
    catalog = load_shop_catalog()
    item = catalog.item("solitary_shard_monthly_resume")
    currency = catalog.currencies[item.currency]
    card = shop_interface.ShopItemCard(
        item, currency, {"enabled": False, "quantity": "one"},
    )

    card.setFailureReason("商品价格校验失败: 独石碎片，目录 2，实机 None")

    assert "干跑失败" in card.failureLabel.text()
    assert "独石碎片" in card.failureLabel.text()
    assert card.failureLabel.isVisible() is False  # parent not shown
    assert application is not None
    card.deleteLater()


def test_failure_reason_is_cleared_by_new_observed_price(monkeypatch):
    application = QApplication.instance() or QApplication([])
    catalog = load_shop_catalog()
    item = catalog.item("laplace_weekly_iron")
    currency = catalog.currencies[item.currency]
    card = shop_interface.ShopItemCard(
        item, currency, {"enabled": False, "quantity": "one"},
    )

    card.setFailureReason("上一次错误")
    card.setObservedPriceSchedule([
        {"quantity": 1, "marginal_cost": 100000, "cumulative_cost": 100000},
    ])

    assert not card.failureLabel.isVisible()
    assert card.observedPriceLabel.isVisible() is False
    assert application is not None
    card.deleteLater()
