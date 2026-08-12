from unittest.mock import patch

from core.module.bgr import BGR
from core.preset import station as station_module


class _Frame:
    def __init__(self, *, attack=None, reach=None, run=None):
        self.attack = attack or [BGR(0, 0, 0)] * 4
        self.reach = reach or [BGR(0, 0, 0)] * 4
        self.run = run or BGR(0, 0, 0)

    def get_bgrs(self, points):
        return self.attack if len(points) == 4 and points[0] == (944, 247) else self.reach

    def get_bgr(self, point):
        return self.run

    def ocr(self):
        return []


def test_station_wait_reports_departure_not_established():
    travel = station_module.STATION(False)
    assert travel.wait() is False
    assert travel.last_wait_outcome == "DEPARTURE_NOT_ESTABLISHED"


def test_station_wait_reports_destination_already_confirmed():
    travel = station_module.STATION(True, is_destine=True)
    assert travel.wait() is True
    assert travel.last_wait_outcome == "DESTINATION_ALREADY_CONFIRMED"


def test_station_wait_reports_fixed_pixel_arrival(monkeypatch):
    frame = _Frame(
        reach=[BGR(22, 22, 22), BGR(253, 253, 253), BGR(0, 0, 0), BGR(0, 0, 0)]
    )
    taps = []
    monkeypatch.setattr(station_module, "screenshot", lambda: frame)
    monkeypatch.setattr(station_module, "input_tap", lambda point: taps.append(point))
    monkeypatch.setattr(station_module.time, "sleep", lambda _seconds: None)

    travel = station_module.STATION(True)
    assert travel.wait() is True
    assert travel.last_wait_outcome == "ARRIVAL_FIXED_PIXEL_CONFIRMED"
    assert taps == [(877, 359)]


def test_station_wait_reports_hud_arrival_without_extra_tap(monkeypatch):
    frame = _Frame(run=BGR(0, 174, 243))
    taps = []
    monkeypatch.setattr(station_module, "screenshot", lambda: frame)
    monkeypatch.setattr(station_module, "input_tap", lambda point: taps.append(point))
    monkeypatch.setattr(station_module.time, "sleep", lambda _seconds: None)

    travel = station_module.STATION(True)
    assert travel.wait() is True
    assert travel.last_wait_outcome == "ARRIVAL_HUD_CONFIRMED"
    assert taps == []


def test_station_wait_reports_timeout(monkeypatch):
    ticks = iter((0.0, station_module.MAP_WAIT_TIME + 1.0))
    monkeypatch.setattr(station_module.time, "perf_counter", lambda: next(ticks))

    travel = station_module.STATION(True)
    assert travel.wait() is False
    assert travel.last_wait_outcome == "ARRIVAL_MONITOR_TIMEOUT"
