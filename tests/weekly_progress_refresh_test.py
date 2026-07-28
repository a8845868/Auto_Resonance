from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace

import pytest

import core.services.weekly_plan_state as weekly_state
from core.services.weekly_plan_state import (
    WeeklyProgressContractError,
    load_weekly_plan,
    progress_summary,
    serialize_progress_summary,
    validate_progress_summary,
)


SERVER_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=SERVER_TZ)


def _state(*, completed=0, total=3):
    return {
        "version": 3,
        "schema_version": 3,
        "week_start": "2026-07-20",
        "cycle": ["岚心城", "汇流塔"],
        "total_runs": total,
        "runs": [{"岚心城": 1, "汇流塔": 0} for _ in range(total)],
        "completed_runs": completed,
        "completed_books": 0,
        "books_total": total,
        "cycle_fatigue": 100,
        "leg_fatigue_schedule": [50, 50],
        "expected_profit": total * 1000,
        "run_expected_profits": [1000] * total,
        "price_time": NOW.isoformat(),
        "price_source": "game_observed",
        "price_revision": "fixture-r1",
    }


def _facts(*, partial=None):
    return SimpleNamespace(
        server_week_id="2026-07-20",
        source=SimpleNamespace(value="UNKNOWN"),
        confidence="UNKNOWN",
        baseline_known=False,
        baseline_round_trips=None,
        tracking_started_at=None,
        confirmed_delta_since_baseline=0,
        full_week_total=None,
        confirmed_round_trips=None,
        confirmed_legs=int((partial or {}).get("confirmed_legs", 0)),
        current_partial_cycle=partial,
        purchase_books_used=0,
        confirmed_profit=0,
        last_reconciled_at=None,
        last_confirmed_transaction_at=None,
    )


def _summary(monkeypatch, tmp_path, state, *, partial=None, now=NOW):
    monkeypatch.setattr(weekly_state, "load_trade_week_state", lambda *_args, **_kwargs: _facts(partial=partial))
    return progress_summary(state, ledger_path=tmp_path / "ledger.json", now=now)


def test_active_batch_contract_uses_mapping_and_separate_evaluation_time(monkeypatch, tmp_path):
    summary = _summary(
        monkeypatch,
        tmp_path,
        _state(),
        partial={"confirmed_legs": 1, "last_destination": "汇流塔"},
    )

    validate_progress_summary(summary)
    assert isinstance(summary["current_batch"], dict)
    assert summary["current_batch"] == {
        "runs": 3,
        "books": {"岚心城": 1, "汇流塔": 0},
    }
    assert summary["current_batch_index"] == 0
    assert summary["next_batch"] is None
    assert summary["evaluated_at"] is NOW
    assert not isinstance(summary["current_batch"], datetime)
    serialized = serialize_progress_summary(summary)
    assert serialized["evaluated_at"] == NOW.isoformat(timespec="seconds")
    json.dumps(serialized)


def test_not_started_has_no_current_batch_and_one_valid_next_batch(monkeypatch, tmp_path):
    summary = _summary(monkeypatch, tmp_path, _state())

    assert summary["status"] == "NOT_STARTED"
    assert summary["current_batch"] is None
    assert summary["next_batch"]["runs"] == 3
    assert summary["current_batch_started_at"] is None
    assert summary["current_batch_scheduled_at"] is None
    assert summary["next_batch_scheduled_at"] is None


def test_completed_plan_has_no_current_or_next_batch(monkeypatch, tmp_path):
    summary = _summary(monkeypatch, tmp_path, _state(completed=3))

    assert summary["status"] == "COMPLETED"
    assert summary["finished"] is True
    assert summary["current_batch"] is None
    assert summary["next_batch"] is None


def test_between_batches_is_in_progress_with_next_batch_only(monkeypatch, tmp_path):
    summary = _summary(monkeypatch, tmp_path, _state(completed=1))

    assert summary["status"] == "IN_PROGRESS"
    assert summary["current_batch"] is None
    assert summary["next_batch"]["runs"] == 2


def test_naive_progress_time_is_rejected_with_contract_error(monkeypatch, tmp_path):
    with pytest.raises(
        WeeklyProgressContractError,
        match="weekly_progress_now_must_be_timezone_aware",
    ):
        _summary(monkeypatch, tmp_path, _state(), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 7, 25, 23, 59, tzinfo=SERVER_TZ),
        datetime(2026, 7, 26, 0, 1, tzinfo=SERVER_TZ),
    ],
)
def test_aware_cross_midnight_time_never_changes_batch_type(
    monkeypatch, tmp_path, observed_at
):
    summary = _summary(monkeypatch, tmp_path, _state(), now=observed_at)

    assert summary["current_batch"] is None
    assert isinstance(summary["next_batch"], dict)
    assert summary["evaluated_at"] == observed_at


def test_legacy_valid_weekly_plan_still_loads_without_schema_migration(tmp_path):
    path = tmp_path / "weekly_plan.json"
    legacy = {
        "week_start": weekly_state.current_week_start(),
        "cycle": ["A", "B"],
        "total_runs": 1,
        "runs": [{"A": 0, "B": 0}],
        "completed_runs": 0,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    assert load_weekly_plan(path=path) == legacy


def test_reopened_saved_plan_uses_stable_progress_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(
        weekly_state,
        "current_week_start",
        lambda today=None: "2026-07-20",
    )
    path = tmp_path / "weekly_plan.json"
    state = _state()
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    reopened = load_weekly_plan(path=path)

    summary = _summary(monkeypatch, tmp_path, reopened)

    validate_progress_summary(summary)
    assert summary["current_batch"] is None
    assert isinstance(summary["next_batch"], dict)


class _Label:
    def __init__(self, *, destroyed=False):
        self.destroyed = destroyed
        self.values = []

    def setText(self, value):
        if self.destroyed:
            raise RuntimeError("wrapped C/C++ object has been deleted")
        self.values.append(value)


def _formatter_fake():
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    return SimpleNamespace(_format_books=TwoRunBusinessInterface._format_books)


def test_format_batch_accepts_valid_mapping_and_none():
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    fake = _formatter_fake()
    assert TwoRunBusinessInterface._format_batch(
        fake, {"runs": 2, "books": {"岚心城": 1}}
    ) == "2次往返每次到岚心城进货时用1本"
    assert TwoRunBusinessInterface._format_batch(fake, None) == "暂无当前批次"


@pytest.mark.parametrize("invalid", [NOW, [], "bad", 7])
def test_format_batch_invalid_types_are_safe_and_expose_reason(invalid):
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    rendered = TwoRunBusinessInterface._format_batch(_formatter_fake(), invalid)

    assert "invalid_weekly_progress_batch_type" in rendered


def test_refresh_boundary_keeps_fake_widget_alive_on_contract_error(monkeypatch):
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    label = _Label()
    fake = SimpleNamespace(
        weeklyProgressLabel=label,
        _refreshWeeklyProgress=lambda: (_ for _ in ()).throw(
            WeeklyProgressContractError("invalid_weekly_progress_batch_type")
        ),
    )

    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is False
    assert "weekly_progress_refresh_failed" in label.values[-1]


def test_refresh_boundary_is_safe_after_widget_destruction():
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    fake = SimpleNamespace(
        weeklyProgressLabel=_Label(destroyed=True),
        _refreshWeeklyProgress=lambda: (_ for _ in ()).throw(RuntimeError("deleted")),
    )

    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is False


def test_two_read_only_timer_refreshes_do_not_rewrite_weekly_plan(tmp_path):
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    path = tmp_path / "weekly_plan.json"
    path.write_text(json.dumps(_state(), ensure_ascii=False), encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    fake = SimpleNamespace(
        _refreshWeeklyProgress=lambda: None,
        weeklyProgressLabel=_Label(),
    )

    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is True
    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def _gui_summary():
    return {
        **_state(),
        "current_batch": None,
        "current_batch_index": None,
        "current_batch_started_at": None,
        "current_batch_scheduled_at": None,
        "next_batch": {"runs": 3, "books": {"岚心城": 1, "汇流塔": 0}},
        "next_batch_scheduled_at": None,
        "evaluated_at": NOW,
        "status": "NOT_STARTED",
        "reason": None,
        "remaining_batches": [
            {"runs": 3, "books": {"岚心城": 1, "汇流塔": 0}}
        ],
        "finished": False,
        "full_week_total": None,
        "confirmed_delta_since_baseline": 0,
        "baseline_known": False,
        "progress_source": "UNKNOWN",
        "current_partial_cycle": None,
        "confirmed_books_used": 0,
        "planned_round_trips_remaining": 3,
        "remaining_expected_profit": 3000,
        "remaining_required_fatigue": 300,
        "remaining_profit_per_fatigue": 10,
        "today_suggested_runs": None,
        "confirmed_available_fatigue": None,
        "recoverable_fatigue_today": None,
    }


def test_timer_refresh_renders_next_batch_when_no_batch_is_active(monkeypatch):
    import core.services as services
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    label = _Label()
    fake = SimpleNamespace(
        weeklyProgressLabel=label,
        _format_batch=MethodType(TwoRunBusinessInterface._format_batch, _formatter_fake()),
    )
    monkeypatch.setattr(services, "progress_summary", lambda: _gui_summary())

    TwoRunBusinessInterface._refreshWeeklyProgress(fake)

    assert "下一步动作：3次往返" in label.values[-1]


def test_timer_contract_failure_is_caught_without_save_or_business_action(monkeypatch):
    import core.services as services
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    invalid = _gui_summary()
    invalid["current_batch"] = NOW
    label = _Label()
    fake = SimpleNamespace(weeklyProgressLabel=label)
    fake._refreshWeeklyProgress = MethodType(
        TwoRunBusinessInterface._refreshWeeklyProgress,
        fake,
    )
    monkeypatch.setattr(services, "progress_summary", lambda: invalid)
    monkeypatch.setattr(
        services,
        "save_weekly_plan",
        lambda *_args, **_kwargs: pytest.fail("timer must not save"),
    )

    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is False
    assert "weekly_progress_refresh_failed" in label.values[-1]


def test_two_consecutive_timer_contract_failures_keep_widget_callable(monkeypatch):
    import core.services as services
    from app.view.two_city_run_business_interface import TwoRunBusinessInterface

    invalid = _gui_summary()
    invalid["next_batch"] = "bad"
    fake = SimpleNamespace(weeklyProgressLabel=_Label())
    fake._refreshWeeklyProgress = MethodType(
        TwoRunBusinessInterface._refreshWeeklyProgress,
        fake,
    )
    monkeypatch.setattr(services, "progress_summary", lambda: invalid)

    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is False
    assert TwoRunBusinessInterface.refreshWeeklyProgress(fake) is False
    assert len(fake.weeklyProgressLabel.values) == 2
