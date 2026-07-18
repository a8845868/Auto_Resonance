from __future__ import annotations

import inspect
from unittest.mock import Mock

import tools.sixth_read_only_probe as probe
from core.services.read_only_policy import ReadOnlyActionGuard


def test_read_only_guard_blocks_transaction_action():
    tap = Mock()
    guard = ReadOnlyActionGuard(tap)
    assert guard.tap("transaction_buy", (1000, 650), "exchange_buy") is False
    tap.assert_not_called()


def test_read_only_guard_blocks_reward_claim():
    guard = ReadOnlyActionGuard(Mock())
    assert guard.tap("reward_claim", (1000, 620), "daily_reward") is False


def test_read_only_guard_blocks_fatigue_confirmation():
    guard = ReadOnlyActionGuard(Mock())
    assert guard.tap("fatigue_confirm", (900, 600), "rest_area") is False


def test_read_only_probe_reports_actual_blocked_actions():
    guard = ReadOnlyActionGuard(Mock())
    probe.run_policy_canaries(guard)
    report = probe.policy_report(guard)
    assert set(report["blocked_actions"]) >= {"transaction_buy", "reward_claim", "fatigue_confirm"}
    assert all(entry["allowed"] is False for entry in report["journal"])


def test_read_only_probe_has_no_direct_input_tap_bypass():
    assert "input_tap" not in inspect.getsource(probe)


def test_navigation_anchor_is_allowed_but_buy_button_is_blocked():
    tap = Mock()
    guard = ReadOnlyActionGuard(tap)
    assert guard.tap("exchange_buy_anchor", (805, 324), "exchange_menu") is True
    assert guard.tap("unclassified_tap", (1000, 650), "预计买入 买入总价") is False
    tap.assert_called_once_with((805, 324))
