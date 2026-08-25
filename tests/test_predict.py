"""predict.py — choosing between the pump's forecast and our own.

The rule the module exists to enforce: the AID system's own curve wins
while it is fresh, our estimate fills the gap, and nothing is drawn at all
from stale data.
"""

import time

import pytest

from glucocube import predict as predict_mod
from glucocube.predict import HORIZONS, MAX_PREDICTION_AGE_MS, device_series
from glucocube.store import UserSnapshot

MINUTE = 60 * 1000
NOW = 1_700_000_000_000


def minutes_ago(minutes: float) -> int:
    return int(NOW - minutes * MINUTE)


def fresh_snapshot(**overrides) -> UserSnapshot:
    snap = UserSnapshot(
        sgv=120.0,
        sgv_date=minutes_ago(2),
        history=[(minutes_ago(5 * i), 120.0) for i in range(10, 0, -1)],
    )
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


# -------------------------------------------------------- device_series ----

def test_device_series_reads_a_loop_prediction():
    raw = {"loop": {"predicted": {"startDate": "2023-11-14T22:13:20Z",
                                  "values": [120, 118, 115]}}}
    start, values = device_series(raw)
    assert values == [120, 118, 115]
    assert start == 1_700_000_000_000


def test_device_series_reads_an_oref_prediction():
    raw = {"openaps": {"suggested": {"timestamp": NOW,
                                     "predBGs": {"IOB": [120, 118, 115]}}}}
    start, values = device_series(raw)
    assert (start, values) == (NOW, [120, 118, 115])


def test_device_series_picks_the_curve_that_lands_near_eventual_bg():
    """oref uploads several scenarios; the zero-temp one is a worst case."""
    raw = {"openaps": {"suggested": {
        "timestamp": NOW,
        "eventualBG": 110,
        "predBGs": {"COB": [120, 115, 112], "ZT": [120, 60, 39],
                    "IOB": [120, 118, 150]},
    }}}
    _start, values = device_series(raw)
    assert values == [120, 115, 112]


def test_device_series_falls_back_to_the_first_curve_without_eventual_bg():
    raw = {"openaps": {"suggested": {"timestamp": NOW,
                                     "predBGs": {"COB": [120, 115],
                                                 "IOB": [120, 100]}}}}
    _start, values = device_series(raw)
    assert values == [120, 115]


def test_device_series_prefers_loop_over_openaps():
    raw = {"loop": {"predicted": {"startDate": NOW, "values": [100, 101]}},
           "openaps": {"suggested": {"timestamp": NOW,
                                     "predBGs": {"IOB": [200, 201]}}}}
    _start, values = device_series(raw)
    assert values == [100, 101]


@pytest.mark.parametrize("raw", [
    None,
    {},
    "not a document",
    {"loop": {"predicted": {"values": []}}},
    {"openaps": {"suggested": {"predBGs": {}}}},
    {"openaps": {"suggested": {"predBGs": {"COB": []}}}},
    {"pump": {"battery": {"percent": 90}}},
])
def test_device_series_returns_nothing_when_there_is_no_curve(raw):
    assert device_series(raw) is None


# -------------------------------------------------------------- predict ----

def test_a_fresh_device_curve_is_used_as_is():
    snap = fresh_snapshot(
        status_date=minutes_ago(3),
        status_raw={"openaps": {"suggested": {
            "timestamp": minutes_ago(3),
            "predBGs": {"IOB": [120 - 2 * i for i in range(30)]}}}},
    )
    horizons, timeline, source = predict_mod.predict(snap, NOW)
    assert source == "device"
    assert sorted(horizons) == list(HORIZONS)
    assert timeline[0][0] > NOW
    assert timeline[-1][0] <= NOW + HORIZONS[-1] * MINUTE


def test_a_stale_device_curve_is_replaced_by_our_own_estimate():
    snap = fresh_snapshot(
        status_date=minutes_ago(40),
        status_raw={"openaps": {"suggested": {
            "timestamp": minutes_ago(40),
            "predBGs": {"IOB": [300] * 30}}}},
    )
    horizons, _timeline, source = predict_mod.predict(snap, NOW)
    assert source == "est"
    assert horizons[120] < 200


def test_the_estimate_is_used_when_the_pump_uploads_no_curve():
    snap = fresh_snapshot(status_date=minutes_ago(2),
                          status_raw={"openaps": {"suggested": {"IOB": 1.0}}})
    _horizons, _timeline, source = predict_mod.predict(snap, NOW)
    assert source == "est"


def test_nothing_is_predicted_from_a_stale_reading():
    """A sensor that dropped out an hour ago has nothing to say about now."""
    snap = fresh_snapshot(sgv_date=minutes_ago(45))
    assert predict_mod.predict(snap, NOW) == (None, None, None)


def test_nothing_is_predicted_with_no_reading_at_all():
    assert predict_mod.predict(UserSnapshot(), NOW) == (None, None, None)


def test_the_boundary_of_freshness_is_respected():
    just_fresh = fresh_snapshot(sgv_date=NOW - MAX_PREDICTION_AGE_MS + 1000)
    just_stale = fresh_snapshot(sgv_date=NOW - MAX_PREDICTION_AGE_MS - 1000)
    assert predict_mod.predict(just_fresh, NOW)[2] == "est"
    assert predict_mod.predict(just_stale, NOW) == (None, None, None)


def test_horizons_land_on_the_expected_points_of_a_device_curve():
    """Index n of a 5-minute series is n*5 minutes after its start."""
    values = [100 + i for i in range(30)]     # 100, 101, ... at 5-min steps
    snap = fresh_snapshot(
        status_date=NOW,
        status_raw={"openaps": {"suggested": {"timestamp": NOW,
                                              "predBGs": {"IOB": values}}}},
    )
    horizons, _timeline, _source = predict_mod.predict(snap, NOW)
    assert horizons[30] == 106     # index 6
    assert horizons[60] == 112     # index 12
    assert horizons[120] == 124    # index 24


def test_a_short_device_curve_is_held_at_its_last_value():
    snap = fresh_snapshot(
        status_date=NOW,
        status_raw={"openaps": {"suggested": {"timestamp": NOW,
                                              "predBGs": {"IOB": [120, 118]}}}},
    )
    horizons, _timeline, _source = predict_mod.predict(snap, NOW)
    assert horizons[120] == 118


def test_a_curve_that_started_before_now_is_indexed_from_its_own_start():
    """Trio's timestamp is minutes old by the time we draw it."""
    values = [100 + i for i in range(30)]
    snap = fresh_snapshot(
        status_date=minutes_ago(10),
        status_raw={"openaps": {"suggested": {"timestamp": minutes_ago(10),
                                              "predBGs": {"IOB": values}}}},
    )
    horizons, timeline, _source = predict_mod.predict(snap, NOW)
    assert horizons[30] == 108     # index 8: 10 minutes of the curve elapsed
    assert all(ms > NOW for ms, _value in timeline)


def test_the_timeline_never_looks_backwards():
    snap = fresh_snapshot(
        status_date=minutes_ago(12),
        status_raw={"loop": {"predicted": {"startDate": minutes_ago(12),
                                           "values": [120] * 40}}},
    )
    _horizons, timeline, _source = predict_mod.predict(snap, NOW)
    assert timeline
    assert all(ms > NOW for ms, _value in timeline)


def test_the_estimated_timeline_starts_after_now_and_is_five_minutes_apart():
    _horizons, timeline, source = predict_mod.predict(fresh_snapshot(), NOW)
    assert source == "est"
    gaps = {b - a for (a, _), (b, _) in zip(timeline, timeline[1:])}
    assert gaps == {5 * MINUTE}


def test_predict_defaults_to_the_current_time():
    now_ms = int(time.time() * 1000)
    snap = UserSnapshot(sgv=120.0, sgv_date=now_ms - 2 * MINUTE,
                        history=[(now_ms - 5 * MINUTE, 118.0),
                                 (now_ms - 2 * MINUTE, 120.0)])
    horizons, timeline, source = predict_mod.predict(snap)
    assert source == "est"
    assert horizons and timeline


def test_an_empty_device_curve_yields_nothing():
    snap = fresh_snapshot(
        sgv_date=minutes_ago(45),      # too stale for the fallback as well
        status_date=NOW,
        status_raw={"loop": {"predicted": {"startDate": NOW, "values": []}}},
    )
    assert predict_mod.predict(snap, NOW) == (None, None, None)
