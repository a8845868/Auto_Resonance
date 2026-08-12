from auto.run_business import main as business


class _Travel:
    def __init__(self, success, *, arrived=None, outcome="TEST_OUTCOME"):
        self.success = success
        self.arrived = success if arrived is None else arrived
        self.last_wait_outcome = outcome

    def __bool__(self):
        return self.success

    def wait(self):
        return self.arrived


def _capture_calls(monkeypatch):
    calls = []

    def capture(
        transition,
        *,
        ledger_context,
        leg_id,
        current_page_classification,
    ):
        calls.append(
            (
                transition,
                ledger_context,
                leg_id,
                current_page_classification,
            )
        )

    monkeypatch.setattr(business, "_capture_run_business_evidence", capture)
    return calls


def test_begin_departure_records_request_and_verified_transit(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ledger = {"cycle_id": "cycle"}
    travel = _Travel(True)

    def click_station(destination, *, cur_station, on_departure_requested):
        assert destination == "B"
        assert cur_station == "A"
        on_departure_requested()
        return travel

    monkeypatch.setattr(business, "click_station", click_station)
    monkeypatch.setattr(business, "_record_ledger_event", lambda *args, **kwargs: None)

    assert business._begin_departure(
        ledger, origin="A", destination="B", leg_id="A|B"
    ) is travel
    assert [call[0] for call in calls] == [
        "DEPARTURE_REQUEST_DISPATCHED",
        "DEPARTURE_TRANSIT_VERIFIED",
    ]
    assert all(call[1] is ledger and call[2] == "A|B" for call in calls)
    assert calls[0][3] == "origin=A|destination=B|requested=true"
    assert calls[1][3] == "origin=A|destination=B|transit_verified=true"


def test_begin_departure_records_missing_transit_without_inventing_request(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ledger = {"cycle_id": "cycle"}
    travel = _Travel(False)
    monkeypatch.setattr(business, "click_station", lambda *args, **kwargs: travel)
    monkeypatch.setattr(business, "_record_ledger_event", lambda *args, **kwargs: None)

    assert business._begin_departure(
        ledger, origin="A", destination="B", leg_id="A|B"
    ) is travel
    assert [call[0] for call in calls] == ["DEPARTURE_TRANSIT_NOT_VERIFIED"]
    assert calls[0][3] == (
        "origin=A|destination=B|transit_verified=false"
        "|departure_outcome=TEST_OUTCOME"
    )


def test_arrival_wait_records_success_and_failure_without_changing_result(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ledger = {"cycle_id": "cycle"}

    assert business._wait_for_arrival_with_evidence(
        _Travel(True, arrived=True), ledger_context=ledger, leg_id="A|B"
    ) is True
    assert business._wait_for_arrival_with_evidence(
        _Travel(True, arrived=False), ledger_context=ledger, leg_id="A|B"
    ) is False

    assert [call[0] for call in calls] == [
        "ARRIVAL_CONFIRMATION_AFTER",
        "ARRIVAL_CONFIRMATION_AFTER",
    ]
    assert [call[3] for call in calls] == [
        "ARRIVAL_VERIFIED_BY_NAVIGATION_WAIT|monitor_outcome=TEST_OUTCOME",
        "ARRIVAL_NOT_VERIFIED_BY_NAVIGATION_WAIT|monitor_outcome=TEST_OUTCOME",
    ]
    assert all(call[1] is ledger and call[2] == "A|B" for call in calls)


def test_departure_classification_preserves_outcome_for_adjacent_boundaries():
    travel = _Travel(False, outcome="GO_STATION_BUTTON_NOT_FOUND")

    assert business._departure_boundary_classification(travel) == (
        "DEPARTURE_NOT_VERIFIED"
        "|departure_outcome=GO_STATION_BUTTON_NOT_FOUND"
    )


def test_departure_classification_preserves_success_state():
    travel = _Travel(True, outcome="DEPARTURE_TRANSIT_CONFIRMED")

    assert business._departure_boundary_classification(travel) == (
        "TRAIN_IN_TRANSIT_VERIFIED_BY_DEPARTURE"
        "|departure_outcome=DEPARTURE_TRANSIT_CONFIRMED"
    )


def test_arrival_classification_preserves_monitor_outcome_for_sell_boundary():
    travel = _Travel(
        True,
        arrived=True,
        outcome="ARRIVAL_FIXED_PIXEL_CONFIRMED",
    )

    assert business._arrival_boundary_classification(travel) == (
        "ARRIVAL_VERIFIED_BY_NAVIGATION_WAIT"
        "|monitor_outcome=ARRIVAL_FIXED_PIXEL_CONFIRMED"
    )


def test_arrival_classification_does_not_invent_success():
    travel = _Travel(
        True,
        arrived=False,
        outcome="ARRIVAL_MONITOR_TIMEOUT",
    )

    assert business._arrival_boundary_classification(travel) == (
        "ARRIVAL_NOT_VERIFIED_BY_NAVIGATION_WAIT"
        "|monitor_outcome=ARRIVAL_MONITOR_TIMEOUT"
    )


def test_exchange_result_classification_preserves_navigation_failure():
    result = business.exchange_navigation.ExchangeNavigationResult(
        False,
        business.exchange_navigation.ExchangeAction.SELL,
        reason="exchange_menu_not_confirmed",
        clicked=(901, 276),
        stage="exchange_menu_wait",
    )

    assert business._exchange_result_classification(
        result,
        success_label="SELL_PAGE_VERIFIED",
        failure_label="SELL_PAGE_NOT_VERIFIED",
    ) == (
        "SELL_PAGE_NOT_VERIFIED"
        "|stage=exchange_menu_wait"
        "|reason=exchange_menu_not_confirmed"
        "|clicked=(901, 276)"
    )


def test_go_business_result_wrapper_respects_compatibility_mock(monkeypatch):
    monkeypatch.setattr(business, "go_business", lambda kind: kind == "sell")

    result = business._go_business_with_result("sell")

    assert result.success is True
    assert result.source == "compatibility_boundary"
    assert result.reason == "structured_result_unavailable"
