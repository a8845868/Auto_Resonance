from core.preset import presets


def test_closed_station_returns_specific_departure_outcome(monkeypatch):
    monkeypatch.setattr(
        presets,
        "station_unavailable_reason",
        lambda _name: "closed",
    )

    travel = presets.click_station("closed-station", cur_station="origin")

    assert bool(travel) is False
    assert travel.last_wait_outcome == "STATION_UNAVAILABLE"


def test_missing_route_coordinates_returns_specific_departure_outcome(monkeypatch):
    monkeypatch.setattr(presets, "station_unavailable_reason", lambda _name: "")
    monkeypatch.setattr(presets, "STATION_NAME2PNG", {"target": "target.png"})
    monkeypatch.setattr(presets, "STATION_DIFFERENCES", {})
    monkeypatch.setattr(presets, "go_home", lambda: True)

    class _Image:
        def match_template(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(presets, "screenshot", lambda: _Image())

    travel = presets.click_station("target", cur_station="origin")

    assert bool(travel) is False
    assert travel.last_wait_outcome == "ROUTE_COORDINATES_UNAVAILABLE"
