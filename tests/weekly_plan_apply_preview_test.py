from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace

import pytest

from core.services.trade_planning import (
    StalePriceSnapshot,
    validate_executable_trade_budget,
)
from core.services.weekly_plan_state import build_weekly_plan_state


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))


def optimizer_result(**overrides):
    result = {
        "cycle": ["岚心城", "铁盟哨站"],
        "execution_batches": [
            {"runs": 2, "books": {"岚心城": 1}},
            {"runs": 1, "books": {"岚心城": 0}},
        ],
        "books_used": 2,
        "combined_profit": 12345,
        "cycle_fatigue": 10,
        "repeats": 3,
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "fixture-r1",
        "current_price_evidence": {
            "source": "game_observed",
            "observed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(minutes=30)).isoformat(),
            "revision": "fixture-r1",
            "station_pair": ["岚心城", "铁盟哨站"],
            "server_day_id": "2026-07-25",
        },
    }
    result.update(overrides)
    return result


def test_optimizer_execution_batches_expand_into_executable_preview_runs():
    preview = build_weekly_plan_state(optimizer_result())

    assert preview["total_runs"] == 3
    assert preview["runs"] == [
        {"岚心城": 1},
        {"岚心城": 1},
        {"岚心城": 0},
    ]
    assert preview["books_total"] == 2
    assert preview["expected_profit"] == 12345


def test_expanded_preview_passes_executable_budget_validation():
    plan = validate_executable_trade_budget(
        build_weekly_plan_state(optimizer_result()),
        now=NOW,
        fatigue_budget=100,
        purchase_books=2,
    )

    assert plan.expected_total_net_profit > 0


def test_empty_execution_batches_are_rejected():
    with pytest.raises(ValueError, match="execution_batches_required"):
        build_weekly_plan_state(optimizer_result(execution_batches=[]))


@pytest.mark.parametrize("runs", [0, -1])
def test_non_positive_execution_batch_runs_are_rejected(runs):
    with pytest.raises(ValueError, match="runs_must_be_positive"):
        build_weekly_plan_state(
            optimizer_result(execution_batches=[{"runs": runs, "books": {}}])
        )


def test_missing_cycle_books_and_price_evidence_are_controlled_errors():
    without_cycle = optimizer_result()
    without_cycle.pop("cycle")
    with pytest.raises(ValueError, match="cycle_invalid"):
        build_weekly_plan_state(without_cycle)

    with pytest.raises(ValueError, match="books_required"):
        build_weekly_plan_state(
            optimizer_result(execution_batches=[{"runs": 1}])
        )

    without_price = optimizer_result(current_price_evidence=None)
    with pytest.raises(StalePriceSnapshot):
        validate_executable_trade_budget(
            build_weekly_plan_state(without_price),
            now=NOW,
            fatigue_budget=100,
            purchase_books=2,
        )


class _Value:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _Combo:
    def __init__(self):
        self.values = []

    def setCurrentText(self, value):
        self.values.append(value)


def _fake_gui(result):
    calls = []
    fake = SimpleNamespace(
        optimizationResult=result,
        optimizerFatigueSpinBox=_Value(100),
        optimizerBooksSpinBox=_Value(2),
        buyCityComboBox=_Combo(),
        sellCityComboBox=_Combo(),
        appliedWeeklyPlan="unchanged",
        updateRouteTradeSettings=lambda: calls.append("settings"),
        updateRouteInfo=lambda: calls.append("info"),
        refreshWeeklyProgress=lambda: calls.append("progress"),
    )
    return fake, calls


def test_successful_fake_gui_apply_previews_and_persists_the_same_expanded_runs(
    tmp_path, monkeypatch
):
    import core.services as services
    import core.services.server_calendar as server_calendar
    import core.services.weekly_plan_state as weekly_plan_state
    from app.view import two_city_run_business_interface as gui_module

    result = optimizer_result()
    fake, calls = _fake_gui(result)
    built_states = []
    real_builder = weekly_plan_state.build_weekly_plan_state

    def recording_builder(value):
        state = real_builder(value)
        built_states.append(state)
        return state

    monkeypatch.setattr(weekly_plan_state, "STATE_PATH", tmp_path / "weekly.json")
    monkeypatch.setattr(weekly_plan_state, "build_weekly_plan_state", recording_builder)
    monkeypatch.setattr(services, "remaining_batches", lambda state: [{"runs": 2, "books": {}}])
    fake_clock = SimpleNamespace(
        server_now=lambda: NOW,
        server_week_date=lambda: NOW.date(),
    )
    monkeypatch.setattr(server_calendar, "SERVER_CLOCK", fake_clock)
    monkeypatch.setattr(weekly_plan_state, "SERVER_CLOCK", fake_clock)
    monkeypatch.setattr(gui_module.qconfig, "set", lambda *_args: None)
    monkeypatch.setattr(gui_module.InfoBar, "success", lambda **_kwargs: None)

    gui_module.TwoRunBusinessInterface._applyOptimizedRoute(fake)

    assert len(built_states) == 2
    assert built_states[0]["runs"] == built_states[1]["runs"]
    assert fake.buyCityComboBox.values == ["岚心城"]
    assert fake.sellCityComboBox.values == ["铁盟哨站"]
    assert fake.appliedWeeklyPlan is result
    assert calls == ["settings", "info", "progress"]


def test_validator_value_error_is_caught_without_save_or_state_change(monkeypatch):
    import core.services as services
    from app.view import two_city_run_business_interface as gui_module

    errors = []
    save_calls = []
    fake, calls = _fake_gui(optimizer_result())
    fake._applyOptimizedRoute = MethodType(
        gui_module.TwoRunBusinessInterface._applyOptimizedRoute,
        fake,
    )
    monkeypatch.setattr(
        "core.services.trade_planning.validate_executable_trade_budget",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid_budget")),
    )
    monkeypatch.setattr(services, "save_weekly_plan", lambda result: save_calls.append(result))
    monkeypatch.setattr(gui_module.InfoBar, "error", lambda **kwargs: errors.append(kwargs))

    gui_module.TwoRunBusinessInterface.applyOptimizedRoute(fake)

    assert fake.appliedWeeklyPlan == "unchanged"
    assert fake.buyCityComboBox.values == []
    assert fake.sellCityComboBox.values == []
    assert calls == []
    assert save_calls == []
    assert errors[0]["title"] == "套用路线失败"


def test_stale_price_keeps_specialized_qt_slot_error(monkeypatch):
    from app.view import two_city_run_business_interface as gui_module

    errors = []
    fake, _calls = _fake_gui(optimizer_result())
    fake._applyOptimizedRoute = lambda: (_ for _ in ()).throw(
        StalePriceSnapshot("price_snapshot_stale")
    )
    monkeypatch.setattr(gui_module.InfoBar, "error", lambda **kwargs: errors.append(kwargs))

    gui_module.TwoRunBusinessInterface.applyOptimizedRoute(fake)

    assert errors[0]["title"] == "价格快照不可执行"


def test_save_exception_is_caught_before_any_gui_or_qconfig_update(monkeypatch):
    import core.services as services
    from app.view import two_city_run_business_interface as gui_module

    errors = []
    qconfig_updates = []
    fake, calls = _fake_gui(optimizer_result())
    fake._applyOptimizedRoute = MethodType(
        gui_module.TwoRunBusinessInterface._applyOptimizedRoute,
        fake,
    )
    monkeypatch.setattr(
        "core.services.trade_planning.validate_executable_trade_budget",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        services,
        "save_weekly_plan",
        lambda _result: (_ for _ in ()).throw(OSError("disk_full")),
    )
    monkeypatch.setattr(gui_module.qconfig, "set", lambda *args: qconfig_updates.append(args))
    monkeypatch.setattr(gui_module.InfoBar, "error", lambda **kwargs: errors.append(kwargs))

    gui_module.TwoRunBusinessInterface.applyOptimizedRoute(fake)

    assert fake.appliedWeeklyPlan == "unchanged"
    assert fake.buyCityComboBox.values == []
    assert fake.sellCityComboBox.values == []
    assert qconfig_updates == []
    assert calls == []
    assert errors[0]["title"] == "套用路线失败"
