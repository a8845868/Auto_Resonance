import pytest

from core.services.train_eta import (
    TrainArrivalEstimator,
    parse_remaining_distance,
    polling_interval,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("剩余行程：830km", 830),
        ("剩余距离 12.5 公里", 12.5),
        ("距离目的地: 800m", 0.8),
        ("剩余行程：1,200km", 1200),
    ],
)
def test_parse_remaining_distance(text, expected):
    assert parse_remaining_distance([{"text": text}]) == expected


def test_parse_remaining_distance_from_split_ocr_boxes():
    assert parse_remaining_distance(
        [{"text": "剩余行程："}, {"text": "830km"}]
    ) == 830


def test_estimator_uses_recent_median_speed():
    estimator = TrainArrivalEstimator()
    assert estimator.observe(100, 0) is None
    assert estimator.observe(90, 10) == pytest.approx(90)
    assert estimator.observe(78, 20) == pytest.approx(78 / 1.1)
    assert estimator.speed == pytest.approx(1.1)


def test_estimator_ignores_distance_increase_and_extreme_ocr_jump():
    estimator = TrainArrivalEstimator()
    estimator.observe(100, 0)
    estimator.observe(90, 10)
    estimator.observe(95, 20)
    estimator.observe(20, 30)
    assert estimator.speed == pytest.approx(1.0)


def test_polling_interval_is_adaptive_and_auto_pick_stays_responsive():
    assert polling_interval(None) == 0.3
    assert polling_interval(20) == 0.3
    assert polling_interval(60) == 2.0
    assert polling_interval(300) == 5.0
    assert polling_interval(300, auto_pick=True) == 0.5
