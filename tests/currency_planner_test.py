from core.services.currency_planner import calculate_acquisition, calculate_currency_plan, currency_for_asset


def test_currency_alias_lookup():
    assert currency_for_asset("里程点").name == "里程点数"
    assert currency_for_asset("金币").name == "铁盟币"


def test_plan_shortfall_and_surplus():
    assert calculate_currency_plan(100, 30, 90) == {
        "available_after_spend": 70, "shortfall": 20, "surplus": 0
    }
    assert calculate_currency_plan(100, 10, 40)["surplus"] == 50


def test_acquisition_range_and_expectation():
    result = calculate_acquisition(10, 20, 4, 50)
    assert result == {"possible_min": 0, "possible_max": 80, "expected": 30.0}


def test_medal_catalog_contains_dual_currency_exchange_and_fixed_yield():
    currency = currency_for_asset("绝命奖章")
    balloon = next(item for item in currency.exchanges if item.name == "胡尔顿气球")
    assert balloon.costs == {"jue_ming": 500, "fu_ming": 2500}
    level_three = next(item for item in currency.activities if item.name == "混响浮标 Lv3")
    assert level_three.rewards == {"fu_ming": (60, 60), "jue_ming": (12, 12)}


def test_iron_currency_has_concrete_budgets_and_manual_income():
    currency = currency_for_asset("铁盟币")
    warehouse = next(item for item in currency.exchanges if item.name == "仓库扩建许可证")
    assert warehouse.costs == {"tie_meng_bi": 1_000_000, "jue_ming": 300, "fu_ming": 1_500}
    train = next(item for item in currency.exchanges if item.name.startswith("列车基础性能改装"))
    assert train.costs["tie_meng_bi"] == 2_100_000
    manual = next(item for item in currency.activities if item.name == "其他预计收入（每10万）")
    assert manual.rewards["tie_meng_bi"] == (100_000, 100_000)
