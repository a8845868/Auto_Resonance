import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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

    assert "第 1 件：边际 100,000，累计 100,000 铁盟币" in text
    assert "第 2 件：边际 200,000，累计 300,000 铁盟币" in text
    assert "第 3 件：价格未采集（不可选为累计目标）" in text
    assert options == (
        ("one", "从当前状态仅买 1 件（按当前档位）"),
        ("max", "买完当前剩余（弹窗实时总价）"),
    )
    assert application is not None


def test_fixed_price_item_keeps_simple_quantity_options():
    catalog = load_shop_catalog()
    item = catalog.item("self_observation_daily_iron")
    currency = catalog.currencies[item.currency]

    assert shop_interface._price_breakdown_text(item, currency) == ""
    assert shop_interface._quantity_options(item, currency) == shop_interface.QUANTITY_OPTIONS
