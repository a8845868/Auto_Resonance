from core.services.gacha_planner import GACHA_SOURCES, GACHA_SOURCE_CATALOG_VERSION, PAID_GACHA_PACKS, calculate_gacha_plan, expected_source_total, pulls_to_guarantee


def test_inventory_and_stones_are_combined_into_pulls():
    result = calculate_gacha_plan(27, 1280, 40)
    assert result["available_pulls"] == 35
    assert result["missing_pulls"] == 5
    assert result["missing_stones"] == 800


def test_sources_and_spend_are_accounted_for():
    result = calculate_gacha_plan(27, 1280, 40, planned_ticket_gain=5, planned_stone_gain=480)
    assert result["available_pulls"] == 43
    assert result["tickets_used"] == 32
    assert result["stones_used"] == 1280
    assert result["stones_left"] == 480


def test_guarantee_and_source_helpers_clamp_values():
    assert pulls_to_guarantee(55, 80) == 25
    assert expected_source_total(3, 4) == 12
    assert expected_source_total(-1, 4) == 0


def test_free_sources_have_evidence_backed_defaults_and_no_paid_pack():
    sources = {source.key: source for source in GACHA_SOURCES}
    assert "paid_pack" not in sources
    assert (sources["black_moon"].default_tickets, sources["black_moon"].default_times) == (6, 1)
    assert (sources["freeport_expulsion"].default_tickets, sources["freeport_expulsion"].default_stones) == (10, 300)
    assert (sources["travel_manual"].default_tickets, sources["travel_manual"].default_times) == (5, 1)


def test_paid_catalog_separates_standard_and_limited_pool_resources():
    packs = {pack.key: pack for pack in PAID_GACHA_PACKS}
    assert (packs["weekly_recruit"].tickets, packs["weekly_recruit"].price_yuan) == (1, 6)
    assert (packs["monthly_recruit"].tickets, packs["monthly_recruit"].stones) == (10, 1680)
    assert packs["event_premium"].special_pulls == 50
    assert packs["event_premium"].tickets == 0
    development = packs["travel_development"]
    boundless = packs["travel_boundless"]
    assert (development.tickets, development.stones) == (10, 680)
    assert (boundless.tickets, boundless.stones) == (development.tickets, development.stones)
    assert development.exclusive_group == boundless.exclusive_group == "travel_manual"


def test_full_manual_gacha_resources_match_live_reward_track():
    free = next(source for source in GACHA_SOURCES if source.key == "travel_manual")
    paid = next(pack for pack in PAID_GACHA_PACKS if pack.key == "travel_development")
    result = calculate_gacha_plan(
        0,
        0,
        999,
        planned_ticket_gain=free.default_tickets + paid.tickets,
        planned_stone_gain=free.default_stones + paid.stones,
    )
    assert GACHA_SOURCE_CATALOG_VERSION == 5
    assert result["ticket_pulls"] == 15
    assert result["stone_pulls"] == 4
    assert result["available_pulls"] == 19
    assert result["stones_left"] == 40
