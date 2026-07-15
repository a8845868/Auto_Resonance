from core.services import station_facilities


def setup_function():
    station_facilities.clear_runtime_facility_cache()


def test_confirmed_core_and_special_stations_have_distinct_facility_results():
    assert station_facilities.rest_area_availability("岚心城") is True
    assert station_facilities.rest_area_availability("七号自由港") is True
    assert station_facilities.rest_area_availability("武林源") is False
    assert station_facilities.rest_area_availability("栖羽站") is False


def test_unknown_station_result_is_cached_only_after_visual_confirmation():
    assert station_facilities.rest_area_availability("未来核心城") is None

    station_facilities.remember_rest_area_availability("未来核心城", False)

    assert station_facilities.rest_area_availability("未来核心城") is False
