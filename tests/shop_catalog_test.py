from datetime import datetime

import pytest

from core.services.shop_catalog import (
    ROOT,
    ShopAttemptAlreadyActive,
    active_shop_attempt,
    configured_purchases,
    default_shop_plan,
    load_shop_catalog,
    load_shop_plan,
    next_monthly_shop_reset,
    next_shop_reset,
    next_weekly_shop_reset,
    normalize_shop_plan,
    record_shop_attempt,
    save_shop_plan,
)


def test_catalog_keeps_resume_intelligence_and_black_moon_ticket_distinct():
    catalog = load_shop_catalog()

    assert len(catalog.shop("headquarters_black_moon").items) == 23
    assert catalog.currencies["resume_intelligence"].name == "履历情报"
    assert catalog.currencies["birch_stone"].name == "桦石"

    ticket = catalog.item("black_moon_ticket_weekly_birch")
    assert ticket.name == "黑月采购券"
    assert ticket.currency == "birch_stone"
    assert ticket.price == 100
    assert ticket.period == "weekly"
    assert ticket.max_limit == 5


def test_default_plan_is_safe_and_selects_nothing():
    catalog = load_shop_catalog()
    plan = default_shop_plan(catalog)

    assert plan["enabled"] is False
    assert plan["capture_evidence"] is True
    assert configured_purchases(plan, catalog) == []
    assert all(
        not rule["enabled"]
        for shop in plan["shops"].values()
        for rule in shop["items"].values()
    )
    assert all(
        rule["quantity"] == "one"
        for shop in plan["shops"].values()
        for rule in shop["items"].values()
    )


def test_every_catalog_icon_exists():
    catalog = load_shop_catalog()

    for currency in catalog.currencies.values():
        assert (ROOT / currency.icon).is_file(), currency.name
    for item in catalog.items:
        assert (ROOT / item.icon).is_file(), item.id


def test_plan_normalization_rejects_unsupported_shop_and_bad_quantity():
    catalog = load_shop_catalog()
    plan = normalize_shop_plan(
        {
            "enabled": True,
            "shops": {
                "headquarters_black_moon": {
                    "enabled": True,
                    "items": {
                        "black_moon_ticket_weekly_birch": {
                            "enabled": True,
                            "quantity": "unknown",
                        }
                    },
                },
                "bureau_exchange": {"enabled": True, "items": {}},
            },
        },
        catalog,
    )

    assert plan["shops"]["bureau_exchange"]["enabled"] is False
    rule = plan["shops"]["headquarters_black_moon"]["items"][
        "black_moon_ticket_weekly_birch"
    ]
    assert rule == {"enabled": True, "quantity": "one"}
    purchases = configured_purchases(plan, catalog)
    assert [purchase.item.id for purchase in purchases] == [
        "black_moon_ticket_weekly_birch"
    ]


def test_plan_normalization_never_enables_string_booleans():
    catalog = load_shop_catalog()
    plan = normalize_shop_plan(
        {
            "enabled": "false",
            "capture_evidence": "false",
            "shops": {
                "headquarters_black_moon": {
                    "enabled": "false",
                    "items": {
                        "black_moon_ticket_weekly_birch": {
                            "enabled": "true",
                            "quantity": "max",
                        }
                    },
                }
            },
        },
        catalog,
    )

    assert plan["enabled"] is False
    assert plan["capture_evidence"] is True
    assert plan["shops"]["headquarters_black_moon"]["enabled"] is False
    assert plan["shops"]["headquarters_black_moon"]["items"][
        "black_moon_ticket_weekly_birch"
    ]["enabled"] is False


def test_plan_round_trip_uses_atomic_json_file(tmp_path):
    catalog = load_shop_catalog()
    path = tmp_path / "shop-plan.json"
    plan = default_shop_plan(catalog)
    plan["enabled"] = True
    rule = plan["shops"]["headquarters_black_moon"]["items"][
        "black_moon_ticket_weekly_birch"
    ]
    rule.update(enabled=True, quantity="one")

    saved = save_shop_plan(plan, path, catalog)
    loaded = load_shop_plan(path, catalog)

    assert loaded == saved
    assert not list(tmp_path.glob("*.tmp"))


def test_reset_schedule_matches_selected_periods():
    catalog = load_shop_catalog()
    now = datetime(2026, 7, 13, 6, 30)  # Monday, after the 05:00 reset.

    assert next_weekly_shop_reset(now) == datetime(2026, 7, 20, 5, 0)
    assert next_monthly_shop_reset(now) == datetime(2026, 8, 1, 5, 0)

    weekly = default_shop_plan(catalog)
    weekly["enabled"] = True
    weekly["shops"]["headquarters_black_moon"]["items"][
        "black_moon_ticket_weekly_birch"
    ]["enabled"] = True
    assert next_shop_reset(now, weekly, catalog) == datetime(2026, 7, 20, 5, 0)

    daily = default_shop_plan(catalog)
    daily["enabled"] = True
    daily["shops"]["headquarters_black_moon"]["items"][
        "self_observation_daily_iron"
    ]["enabled"] = True
    assert next_shop_reset(now, daily, catalog) == datetime(2026, 7, 14, 5, 0)


def test_item_attempt_ledger_blocks_each_item_until_its_own_reset(tmp_path):
    catalog = load_shop_catalog()
    path = tmp_path / "shop-attempts.json"
    now = datetime(2026, 7, 13, 6, 30)  # Monday
    daily = catalog.item("self_observation_daily_iron")
    weekly = catalog.item("black_moon_ticket_weekly_birch")

    record_shop_attempt(daily, "one", quantity=1, now=now, path=path)
    record_shop_attempt(weekly, "max", quantity=5, now=now, path=path)

    assert active_shop_attempt(daily, now, path) is not None
    assert active_shop_attempt(weekly, now, path) is not None
    assert active_shop_attempt(daily, datetime(2026, 7, 14, 5, 1), path) is None
    assert active_shop_attempt(weekly, datetime(2026, 7, 14, 5, 1), path) is not None
    assert active_shop_attempt(weekly, datetime(2026, 7, 20, 5, 1), path) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_item_attempt_ledger_refuses_to_overwrite_active_cycle(tmp_path):
    catalog = load_shop_catalog()
    path = tmp_path / "shop-attempts.json"
    item = catalog.item("black_moon_ticket_weekly_birch")
    first = datetime(2026, 7, 13, 6, 30)

    original = record_shop_attempt(item, "one", quantity=1, now=first, path=path)
    with pytest.raises(ShopAttemptAlreadyActive) as caught:
        record_shop_attempt(
            item,
            "max",
            quantity=5,
            now=datetime(2026, 7, 14, 6, 30),
            path=path,
        )

    assert caught.value.entry == original
    assert active_shop_attempt(item, datetime(2026, 7, 14, 6, 30), path) == original


def test_item_attempt_ledger_allows_new_write_after_own_reset(tmp_path):
    catalog = load_shop_catalog()
    path = tmp_path / "shop-attempts.json"
    item = catalog.item("self_observation_daily_iron")

    record_shop_attempt(
        item,
        "one",
        now=datetime(2026, 7, 13, 6, 30),
        path=path,
    )
    renewed = record_shop_attempt(
        item,
        "one",
        now=datetime(2026, 7, 14, 5, 1),
        path=path,
    )

    assert renewed["attempted_at"] == "2026-07-14T05:01:00"
    assert renewed["blocked_until"] == "2026-07-15T05:00:00"
