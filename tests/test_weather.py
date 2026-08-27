"""weather.py — the temperature in the corner, and where it comes from.

The device asks Open-Meteo, which needs no key, and it asks only once it
has been told where it is. These pin the two things that would be quietly
wrong otherwise: the code-to-mark mapping, and that a temperature of zero
survives being stored.
"""

import pytest

from glucocube import synclog, weather
from glucocube.config import WeatherConfig
from helpers import RecordingOpener

FORECAST = {
    "current": {"temperature_2m": 72.4, "weather_code": 2},
    "daily": {"temperature_2m_max": [78.1], "temperature_2m_min": [61.3]},
}


# ------------------------------------------------------------- WMO codes ---

@pytest.mark.parametrize("code, mark", [
    (0, "clear"), (1, "partly"), (2, "partly"), (3, "cloudy"),
    (45, "fog"), (48, "fog"),
    (51, "rain"), (61, "rain"), (65, "rain"), (80, "rain"),
    (71, "snow"), (75, "snow"), (86, "snow"),
    (95, "storm"), (99, "storm"),
])
def test_a_wmo_code_becomes_a_shape_this_panel_can_draw(code, mark):
    assert weather.mark_for(code) == mark


@pytest.mark.parametrize("code", [None, "", "sunny", object()])
def test_anything_that_is_not_a_code_draws_the_plain_mark(code):
    assert weather.mark_for(code) == "clear"


# ------------------------------------------------------------- geocoding ---

def test_a_town_name_becomes_two_numbers_and_a_name(monkeypatch):
    calls = RecordingOpener({"geocoding-api": {"results": [{
        "name": "Sheffield", "admin1": "England", "country": "United Kingdom",
        "latitude": 53.38, "longitude": -1.47}]}})
    monkeypatch.setattr("urllib.request.urlopen", calls)
    found = weather.geocode("Sheffield")
    assert found == {"latitude": 53.38, "longitude": -1.47,
                     "place": "Sheffield, England, United Kingdom"}
    # The resolved name matters: it is how the settings page says which
    # Springfield it decided on rather than silently picking one.
    assert "name=Sheffield" in calls.urls[0]


def test_a_place_nobody_has_heard_of_is_not_found(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        RecordingOpener({"geocoding-api": {"results": []}}))
    assert weather.geocode("Nowhere-at-all") is None


def test_an_empty_search_asks_nothing(monkeypatch):
    # The autouse no-network fixture is the assertion: reaching out here
    # would raise.
    assert weather.geocode("   ") is None


# --------------------------------------------------------------- fetching --

def test_now_and_todays_range_arrive_in_one_request(monkeypatch):
    calls = RecordingOpener({"api.open-meteo": FORECAST})
    monkeypatch.setattr("urllib.request.urlopen", calls)
    reading = weather.fetch(53.38, -1.47, "celsius")
    assert reading["temp"] == 72.4
    assert reading["code"] == 2
    assert (reading["high"], reading["low"]) == (78.1, 61.3)
    url = calls.urls[0]
    assert "latitude=53.38" in url and "temperature_unit=celsius" in url
    assert "current=temperature_2m%2Cweather_code" in url


def test_an_answer_with_no_temperature_is_a_failure(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        RecordingOpener({"api.open-meteo": {"current": {}}}))
    poller = weather.WeatherPoller(
        WeatherConfig(enabled=True, latitude=1.0, longitude=2.0), store)
    with pytest.raises(ValueError):
        poller._poll_once()


# ---------------------------------------------------------------- reading --

def test_nothing_stored_is_nothing_to_draw(store):
    assert weather.current(store) is None


def test_a_stored_reading_comes_back_ready_to_draw(store):
    store.replace_params(weather.PARAMS_KEY, {
        "temp": 72.4, "code": 2, "high": 78.1, "low": 61.3,
        "fetched_at": 1_000_000})
    reading = weather.current(store, now_ms=1_000_000)
    assert reading["temp"] == "72°"
    # Two spaces: one reads as a single value with a stray letter in it.
    assert reading["range"] == "H 78  L 61"
    assert reading["fresh"] is True


def test_a_reading_old_enough_to_be_lying_says_so(store):
    store.replace_params(weather.PARAMS_KEY,
                         {"temp": 10, "code": 0, "fetched_at": 0})
    assert weather.current(store, now_ms=weather.STALE_MS + 1)["fresh"] is False


def test_freezing_is_a_temperature(store):
    """0° is falsy, and set_params drops falsy values — hence replace_params."""
    store.replace_params(weather.PARAMS_KEY,
                         {"temp": 0, "code": 0, "fetched_at": 1})
    assert weather.current(store, now_ms=2)["temp"] == "0°"


def test_a_reading_with_no_range_still_draws(store):
    store.replace_params(weather.PARAMS_KEY,
                         {"temp": 5, "code": 0, "fetched_at": 1})
    assert weather.current(store, now_ms=2)["range"] == ""


# ---------------------------------------------------------------- polling --

def test_a_poll_stores_the_reading_and_says_so_in_the_log(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        RecordingOpener({"api.open-meteo": FORECAST}))
    poller = weather.WeatherPoller(
        WeatherConfig(enabled=True, latitude=53.38, longitude=-1.47,
                      place="Sheffield"), store)
    poller._poll_once()
    assert store.get_params(weather.PARAMS_KEY)["temp"] == 72.4
    entry = synclog.recent()[0]
    assert entry["source"] == "weather" and entry["ok"] is True
    assert "Sheffield" in entry["message"]


def test_a_display_that_does_not_know_where_it_is_asks_nothing(store):
    poller = weather.WeatherPoller(WeatherConfig(enabled=True), store)
    poller._poll_once()          # no network fixture would raise
    assert weather.current(store) is None


def test_the_poller_only_starts_when_it_can_do_something(store):
    assert weather.start_poller(
        type("C", (), {"weather": WeatherConfig()})(), store) is None
    assert weather.start_poller(
        type("C", (), {"weather": WeatherConfig(enabled=True)})(), store) is None


def test_the_poller_starts_once_it_has_a_location(store):
    config = type("C", (), {"weather": WeatherConfig(
        enabled=True, latitude=1.0, longitude=2.0)})()
    poller = weather.start_poller(config, store)
    assert poller is not None
    poller.stop()
    # Every fifteen minutes, and it reports like any other source.
    assert poller.poll_seconds == weather.POLL_SECONDS
    assert poller.kind == "weather"
