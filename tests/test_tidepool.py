"""tidepool.py — converting Tidepool's documents into Nightscout shapes.

Tidepool normalises glucose to mmol/L and has no trend arrows, so the
transform has to reconstruct both. Everything downstream (the chart, the
arrow, IOB/COB) reads what comes out of here.
"""

import json
import urllib.error
import urllib.request

import pytest

from glucocube import tidepool
from glucocube.tidepool import (
    MGDL_PER_MMOLL,
    TidepoolPoller,
    direction_from_rate,
    latest_cbg,
    login,
    params_from_pumpsettings,
    to_mgdl,
    transform,
)

from helpers import FakeResponse, RecordingOpener


def cbg(time: str, value: float, units: str = "mmol/L") -> dict:
    return {"type": "cbg", "time": time, "value": value, "units": units}


# --------------------------------------------------------------- to_mgdl ----

def test_mmol_is_converted():
    assert to_mgdl(5.5, "mmol/L") == pytest.approx(99.09, abs=0.01)


@pytest.mark.parametrize("units", ["mg/dL", "mg/dl", None, ""])
def test_mgdl_is_left_alone(units):
    assert to_mgdl(99.0, units) == 99.0


# ------------------------------------------------------ direction arrows ----

@pytest.mark.parametrize("rate, expected", [
    (25, "DoubleUp"), (18, "DoubleUp"),
    (12, "SingleUp"), (10.5, "SingleUp"),
    (7, "FortyFiveUp"), (5.5, "FortyFiveUp"),
    (0, "Flat"), (3, "Flat"), (-3, "Flat"),
    (-7, "FortyFiveDown"),
    (-12, "SingleDown"),
    (-25, "DoubleDown"),
])
def test_the_slope_becomes_a_nightscout_arrow(rate, expected):
    assert direction_from_rate(rate) == expected


def test_no_slope_means_no_arrow():
    assert direction_from_rate(None) is None


# ------------------------------------------------------------- transform ----

def test_readings_are_converted_and_ordered():
    entries, _t, _d = transform([
        cbg("2024-03-01T12:05:00Z", 6.0),
        cbg("2024-03-01T12:00:00Z", 5.5),
    ])
    assert [e["sgv"] for e in entries] == [99, 108]
    assert entries[0]["date"] < entries[1]["date"]
    assert entries[0]["device"] == "tidepool"


def test_the_first_reading_has_no_arrow_to_derive_one_from():
    entries, _t, _d = transform([cbg("2024-03-01T12:00:00Z", 5.5)])
    assert entries[0]["direction"] is None


def test_an_arrow_is_derived_from_consecutive_readings():
    entries, _t, _d = transform([
        cbg("2024-03-01T12:00:00Z", 5.5),
        cbg("2024-03-01T12:05:00Z", 6.5),      # +18 mg/dL in 5 minutes
    ])
    assert entries[1]["direction"] == "DoubleUp"


def test_no_arrow_is_derived_across_a_sensor_gap():
    entries, _t, _d = transform([
        cbg("2024-03-01T12:00:00Z", 5.5),
        cbg("2024-03-01T13:00:00Z", 6.5),
    ])
    assert entries[1]["direction"] is None


def test_readings_in_mgdl_are_not_converted_twice():
    entries, _t, _d = transform([cbg("2024-03-01T12:00:00Z", 99, "mg/dL")])
    assert entries[0]["sgv"] == 99


def test_a_bolus_becomes_a_treatment():
    _e, treatments, _d = transform([
        {"type": "bolus", "id": "b1", "normal": 2.5,
         "time": "2024-03-01T12:00:00Z"}])
    assert treatments[0]["eventType"] == "Bolus"
    assert treatments[0]["insulin"] == 2.5
    assert treatments[0]["_id"] == "b1"


def test_an_extended_bolus_counts_both_halves():
    _e, treatments, _d = transform([
        {"type": "bolus", "id": "b1", "normal": 1.0, "extended": 1.5,
         "time": "2024-03-01T12:00:00Z"}])
    assert treatments[0]["insulin"] == 2.5


def test_a_zero_bolus_is_dropped():
    _e, treatments, _d = transform([
        {"type": "bolus", "id": "b1", "normal": 0,
         "time": "2024-03-01T12:00:00Z"}])
    assert treatments == []


def test_food_becomes_a_carb_treatment():
    _e, treatments, _d = transform([{
        "type": "food", "id": "f1", "time": "2024-03-01T12:00:00Z",
        "nutrition": {"carbohydrate": {"net": 40, "units": "grams"}}}])
    assert treatments[0]["eventType"] == "Carb Correction"
    assert treatments[0]["carbs"] == 40


def test_food_with_no_carbohydrate_is_dropped():
    _e, treatments, _d = transform([
        {"type": "food", "id": "f1", "time": "2024-03-01T12:00:00Z"},
        {"type": "food", "id": "f2", "time": "2024-03-01T12:00:00Z",
         "nutrition": {"carbohydrate": {"net": 0}}}])
    assert treatments == []


def test_a_dosing_decision_becomes_a_devicestatus():
    _e, _t, devicestatus = transform([{
        "type": "dosingDecision", "time": "2024-03-01T12:00:00Z",
        "insulinOnBoard": {"amount": 1.4},
        "carbsOnBoard": {"amount": 25}}])
    suggested = devicestatus[0]["openaps"]["suggested"]
    assert (suggested["IOB"], suggested["COB"]) == (1.4, 25)


def test_carbs_on_board_is_read_from_either_spelling():
    _e, _t, devicestatus = transform([{
        "type": "dosingDecision", "time": "2024-03-01T12:00:00Z",
        "insulinOnBoard": {"amount": 1.0},
        "carbohydratesOnBoard": {"amount": 30}}])
    assert devicestatus[0]["openaps"]["suggested"]["COB"] == 30


def test_carbs_on_board_falls_back_to_the_decisions_own_food():
    _e, _t, devicestatus = transform([{
        "type": "dosingDecision", "time": "2024-03-01T12:00:00Z",
        "insulinOnBoard": {"amount": 1.0},
        "food": {"nutrition": {"carbohydrate": {"net": 18}}}}])
    assert devicestatus[0]["openaps"]["suggested"]["COB"] == 18


def test_a_dosing_decision_with_neither_number_is_dropped():
    _e, _t, devicestatus = transform([
        {"type": "dosingDecision", "time": "2024-03-01T12:00:00Z"}])
    assert devicestatus == []


def test_unknown_document_types_are_ignored():
    result = transform([{"type": "wizard", "time": "2024-03-01T12:00:00Z"},
                        {"type": "upload"}, {}])
    assert result == ([], [], [])


def test_an_empty_download_transforms_to_nothing():
    assert transform([]) == ([], [], [])


def test_a_reading_with_no_value_is_skipped():
    entries, _t, _d = transform([
        {"type": "cbg", "time": "2024-03-01T12:00:00Z", "value": None},
        cbg("2024-03-01T12:05:00Z", 5.5)])
    assert len(entries) == 1


# --------------------------------------------------- params_from_settings ----

def test_therapy_settings_are_read_from_the_newest_document():
    docs = [
        {"type": "pumpSettings", "time": "2024-01-01T00:00:00Z",
         "insulinSensitivity": [{"amount": 2.0}], "carbRatio": [{"amount": 15}]},
        {"type": "pumpSettings", "time": "2024-03-01T00:00:00Z",
         "insulinSensitivity": [{"amount": 3.0}], "carbRatio": [{"amount": 9}]},
    ]
    params = params_from_pumpsettings(docs)
    assert params["cr"] == 9.0
    assert params["isf"] == pytest.approx(3.0 * MGDL_PER_MMOLL, abs=0.01)


def test_a_named_settings_schedule_is_read_too():
    docs = [{"type": "pumpSettings", "time": "2024-03-01T00:00:00Z",
             "insulinSensitivities": {"standard": [{"amount": 2.5}]},
             "carbRatios": {"standard": [{"amount": 12}]}}]
    params = params_from_pumpsettings(docs)
    assert params["cr"] == 12.0
    assert params["isf"] == pytest.approx(2.5 * MGDL_PER_MMOLL, abs=0.01)


def test_a_sensitivity_already_in_mgdl_is_not_converted():
    docs = [{"type": "pumpSettings", "time": "2024-03-01T00:00:00Z",
             "insulinSensitivity": [{"amount": 45}]}]
    assert params_from_pumpsettings(docs)["isf"] == 45.0


@pytest.mark.parametrize("docs", [[], [{"type": "cbg"}],
                                  [{"type": "pumpSettings",
                                    "time": "2024-03-01T00:00:00Z"}]])
def test_settings_with_nothing_usable_yield_nothing(docs):
    assert params_from_pumpsettings(docs) == {}


# ----------------------------------------------------------------- login ----

def test_login_sends_basic_credentials_and_returns_the_session(monkeypatch):
    opener = RecordingOpener({"auth/login": FakeResponse(
        {"userid": "u-123"}, headers={"x-tidepool-session-token": "tok"})})
    monkeypatch.setattr(urllib.request, "urlopen", opener)

    token, userid = login("cassidy@example.invalid", "pw")

    assert (token, userid) == ("tok", "u-123")
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.get_header("Authorization").startswith("Basic ")


def test_login_without_a_token_is_a_failure(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener(
        {"auth/login": FakeResponse({"userid": "u-123"})}))
    with pytest.raises(RuntimeError, match="no session token"):
        login("cassidy@example.invalid", "pw")


def test_login_propagates_a_rejection(monkeypatch):
    error = urllib.error.HTTPError(tidepool.API_BASE, 401, "no", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen",
                        RecordingOpener({"auth/login": error}))
    with pytest.raises(urllib.error.HTTPError):
        login("cassidy@example.invalid", "wrong")


def test_latest_cbg_asks_for_one_reading_with_the_session_token(monkeypatch):
    opener = RecordingOpener({"/data/": [{"type": "cbg", "value": 5.5}]})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    assert latest_cbg("tok", "u-123") == [{"type": "cbg", "value": 5.5}]
    assert "latest=true" in opener.urls[0]
    assert opener.requests[0].get_header("X-tidepool-session-token") == "tok"


# ---------------------------------------------------------------- poller ----

@pytest.fixture
def poller(store):
    return TidepoolPoller("Ada", {"email": "cassidy@example.invalid",
                                  "password": "pw", "poll_seconds": 60}, store)


def test_a_poll_logs_in_then_ingests(poller, monkeypatch, store):
    docs = [cbg("2024-03-01T12:00:00Z", 5.5),
            {"type": "bolus", "id": "b1", "normal": 2.0,
             "time": "2024-03-01T12:00:00Z"},
            {"type": "dosingDecision", "time": "2024-03-01T12:00:00Z",
             "insulinOnBoard": {"amount": 1.2}}]
    logins = []
    monkeypatch.setattr(poller, "_login",
                        lambda: (logins.append(1),
                                 setattr(poller, "_token", "tok"),
                                 setattr(poller, "_userid", "u"))[0])
    monkeypatch.setattr(poller, "_fetch", lambda: docs)
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener(
        {"pumpSettings": [{"type": "pumpSettings",
                           "time": "2024-03-01T00:00:00Z",
                           "carbRatio": [{"amount": 10}]}]}))

    poller._poll_once()

    assert logins == [1]
    snap = store.snapshot("Ada")
    assert snap.sgv == 99
    assert snap.last_bolus == 2.0
    assert snap.iob == 1.2
    assert snap.params["cr"] == 10.0


def test_a_second_poll_reuses_the_session(poller, monkeypatch):
    logins = []
    monkeypatch.setattr(poller, "_login",
                        lambda: (logins.append(1),
                                 setattr(poller, "_token", "tok"))[0])
    monkeypatch.setattr(poller, "_fetch", lambda: [])
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener({"": []}))
    poller._poll_once()
    poller._poll_once()
    assert logins == [1]


def test_an_expired_session_is_dropped_so_the_next_poll_logs_in(poller,
                                                                monkeypatch):
    """Otherwise the poller retries forever with a dead token."""
    monkeypatch.setattr(poller, "_login", lambda: setattr(poller, "_token", "tok"))

    def expired():
        raise urllib.error.HTTPError(tidepool.API_BASE, 401, "no", {}, None)

    monkeypatch.setattr(poller, "_fetch", expired)
    with pytest.raises(urllib.error.HTTPError):
        poller._poll_once()
    assert poller._token is None


def test_a_server_error_leaves_the_session_alone(poller, monkeypatch):
    monkeypatch.setattr(poller, "_login", lambda: setattr(poller, "_token", "tok"))

    def broken():
        raise urllib.error.HTTPError(tidepool.API_BASE, 500, "oops", {}, None)

    monkeypatch.setattr(poller, "_fetch", broken)
    with pytest.raises(urllib.error.HTTPError):
        poller._poll_once()
    assert poller._token == "tok"


def test_the_fetch_window_is_a_few_hours_of_data(poller, monkeypatch):
    opener = RecordingOpener({"/data/": []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    poller._token, poller._userid = "tok", "u-123"
    poller._fetch()
    url = opener.urls[0]
    assert "type=cbg,bolus,food,dosingDecision" in url
    assert "startDate=" in url


def test_a_poll_is_recorded_in_the_sync_log(poller, monkeypatch):
    from glucocube import synclog

    monkeypatch.setattr(poller, "_login", lambda: setattr(poller, "_token", "t"))
    monkeypatch.setattr(poller, "_fetch",
                        lambda: [cbg("2024-03-01T12:00:00Z", 5.5)])
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener({"": []}))
    poller._poll_once()
    assert synclog.recent()[0]["source"] == "tidepool"


def test_credentials_never_reach_the_sync_log(poller, monkeypatch):
    from glucocube import synclog

    monkeypatch.setattr(poller, "_login", lambda: setattr(poller, "_token", "t"))
    monkeypatch.setattr(poller, "_fetch", lambda: [])
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener({"": []}))
    poller._poll_once()
    assert "pw" not in json.dumps(synclog.recent())
