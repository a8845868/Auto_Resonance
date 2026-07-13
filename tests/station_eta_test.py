from datetime import datetime

from core.preset.station import TravelEtaTracker, parse_remaining_distance


def test_parse_remaining_distance_from_driving_hud():
    assert parse_remaining_distance(["目的地：武林源", "剩余行程：1,002km"]) == 1002
    assert parse_remaining_distance(["自动巡航中"]) is None


def test_eta_logs_first_estimate_then_throttles_updates():
    tracker = TravelEtaTracker(sample_interval=10, log_interval=30)
    wall = datetime(2026, 7, 12, 23, 0, 0)

    initial = tracker.observe(["剩余行程：1000km"], now=0, wall_now=wall)
    first_eta = tracker.observe(["剩余行程：950km"], now=10, wall_now=wall)
    throttled = tracker.observe(["剩余行程：900km"], now=20, wall_now=wall)
    next_eta = tracker.observe(["剩余行程：800km"], now=40, wall_now=wall)

    assert "正在采样" in initial
    assert "预计" in first_eta and "950 km" in first_eta
    assert throttled is None
    assert "预计" in next_eta and "800 km" in next_eta
