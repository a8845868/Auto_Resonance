import core.preset.station as station
from core.module.bgr import BGR


def test_bgr_iteration_and_indexing_share_bgr_order():
    color = BGR(10, 20, 30)

    assert tuple(color) == (10, 20, 30)
    assert (color[0], color[1], color[2]) == (10, 20, 30)
    assert (color[-3], color[-2], color[-1]) == (10, 20, 30)


def test_bgr_matches_uses_explicit_tolerance():
    reference = BGR(10, 20, 30, offset=2)

    assert reference.matches((12, 18, 31))
    assert not reference.matches((13, 20, 30))
    assert reference.matches((10, 20, 30), offset=0)


def test_bgr_in_range_is_componentwise_and_inclusive():
    assert BGR(5, 10, 15).in_range((0, 9, 14), (5, 10, 15))
    assert not BGR(6, 10, 15).in_range((0, 9, 14), (5, 10, 15))


def test_speed_boost_condition_is_reachable_without_guessing_new_thresholds():
    assert station._should_use_speed_boost(BGR(251, 253, 253, offset=0), True)
    assert not station._should_use_speed_boost(BGR(251, 253, 253, offset=0), False)
    assert not station._should_use_speed_boost(BGR(240, 240, 252, offset=0), True)
