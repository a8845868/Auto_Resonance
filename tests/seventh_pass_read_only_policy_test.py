from __future__ import annotations

import inspect
from unittest.mock import Mock

import pytest

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
    canaries = probe.run_policy_canaries(guard)
    report = probe.policy_report(guard, policy_canary_results=canaries)
    assert {entry["action_key"] for entry in report["policy_canary_results"]} >= {"transaction_buy", "reward_claim", "fatigue_confirm"}
    assert report["actual_blocked_production_actions"] == []


def test_read_only_probe_has_no_direct_input_tap_bypass():
    assert "input_tap" not in inspect.getsource(probe)


def test_navigation_anchor_is_allowed_but_buy_button_is_blocked():
    tap = Mock()
    guard = ReadOnlyActionGuard(tap)
    # Generic caller-labelled navigation is no longer sufficient: a trusted
    # observation-backed permit issuer must classify the concrete action.
    assert guard.tap("exchange_buy_anchor", (805, 324), "exchange_menu") is False
    assert guard.tap("unclassified_tap", (1000, 650), "预计买入 买入总价") is False
    tap.assert_not_called()


def test_transaction_page_still_allows_normalized_back_button():
    guard = ReadOnlyActionGuard()
    with pytest.raises(PermissionError, match="trusted issuer"):
        guard.issue_permit(
            action_key="back", page_id="exchange", page_fingerprint="exchange-r1",
            anchor_key="top_left_back", coordinate=(80, 40),
        )
