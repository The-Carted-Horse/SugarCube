"""oref.py — the fallback forecast.

This is a reimplementation of somebody else's model, so the tests check
the properties the model has to hold (insulin decays to nothing, carbs
push the curve up, the display clamp is respected) rather than pinning
exact numbers that would freeze an approximation in place.
"""

import math

import pytest

from glucocube import oref
from glucocube.oref import (
    CLAMP_HI,
    CLAMP_LO,
    Therapy,
    insulin_model,
    predict,
    therapy_from_params,
)

MINUTE = 60 * 1000
NOW = 1_700_000_000_000


def minutes_ago(minutes: float) -> int:
    return int(NOW - minutes * MINUTE)


# -------------------------------------------------------------- therapy ----

def test_therapy_defaults_when_nothing_is_known():
    assert therapy_from_params(None) == Therapy()
    assert therapy_from_params({}) == Therapy()


def test_therapy_takes_plausible_settings():
    therapy = therapy_from_params({"isf": 45, "cr": 8, "dia_hours": 5,
                                   "peak_min": 55})
    assert (therapy.isf, therapy.cr) == (45.0, 8.0)
    assert (therapy.dia_hours, therapy.peak_min) == (5.0, 55.0)


@pytest.mark.parametrize("params", [
    {"isf": 720},        # Trio's dummy Nightscout profile
    {"isf": 0},
    {"isf": -50},
    {"isf": 9},
    {"isf": 401},
    {"isf": None},
])
def test_therapy_rejects_an_implausible_isf(params):
    assert therapy_from_params(params).isf == Therapy().isf


@pytest.mark.parametrize("params", [{"cr": 200}, {"cr": 1}, {"cr": 0}])
def test_therapy_rejects_an_implausible_carb_ratio(params):
    assert therapy_from_params(params).cr == Therapy().cr


@pytest.mark.parametrize("params", [{"dia_hours": 1}, {"dia_hours": 24}])
def test_therapy_rejects_an_implausible_duration(params):
    assert therapy_from_params(params).dia_hours == Therapy().dia_hours


def test_therapy_keeps_the_good_half_of_a_mixed_document():
    therapy = therapy_from_params({"isf": 45, "cr": 200})
    assert therapy.isf == 45.0
    assert therapy.cr == Therapy().cr


# -------------------------------------------------------- insulin model ----

def test_iob_starts_whole_and_ends_at_nothing():
    _activity, iob_frac = insulin_model(360, 75)
    assert iob_frac(0) == 1.0
    assert iob_frac(-5) == 1.0
    assert iob_frac(360) == 0.0
    assert iob_frac(500) == 0.0


def test_iob_decays_monotonically():
    _activity, iob_frac = insulin_model(360, 75)
    values = [iob_frac(t) for t in range(0, 361, 5)]
    assert all(later <= earlier + 1e-9
               for earlier, later in zip(values, values[1:]))


def test_iob_is_roughly_half_gone_by_the_peak_time_plus():
    """Sanity band on the shape, not a pinned number."""
    _activity, iob_frac = insulin_model(360, 75)
    assert 0.6 < iob_frac(75) < 0.95
    assert 0.05 < iob_frac(240) < 0.35


def test_activity_is_zero_outside_the_insulin_duration():
    activity, _iob = insulin_model(360, 75)
    assert activity(-1, 1.0) == 0.0
    assert activity(0, 1.0) == 0.0
    assert activity(360, 1.0) == 0.0
    assert activity(999, 1.0) == 0.0


def test_activity_peaks_near_the_configured_peak_time():
    activity, _iob = insulin_model(360, 75)
    peak_at = max(range(1, 360), key=lambda t: activity(t, 1.0))
    assert abs(peak_at - 75) <= 15


def test_activity_integrates_to_the_whole_bolus():
    """Total activity over the duration must account for every unit."""
    activity, _iob = insulin_model(360, 75)
    total = sum(activity(t + 0.5, 3.0) for t in range(360))
    assert total == pytest.approx(3.0, rel=0.02)


def test_activity_scales_with_the_dose():
    activity, _iob = insulin_model(360, 75)
    assert activity(60, 2.0) == pytest.approx(2 * activity(60, 1.0))


@pytest.mark.parametrize("dia_hours, peak", [(3, 55), (5, 75), (7, 100)])
def test_the_model_holds_for_other_insulins(dia_hours, peak):
    td = dia_hours * 60
    activity, iob_frac = insulin_model(td, peak)
    assert iob_frac(0) == 1.0
    assert iob_frac(td) == 0.0
    assert sum(activity(t + 0.5, 1.0) for t in range(int(td))) == \
        pytest.approx(1.0, rel=0.03)


# -------------------------------------------------------------- predict ----

def flat_history(value: float = 120, count: int = 10) -> list[tuple[int, float]]:
    return [(minutes_ago(5 * i), value) for i in range(count, 0, -1)]


def test_predict_returns_one_value_per_step():
    values, _curve = predict(120, flat_history(), [], None, None, None, NOW,
                             steps=24)
    assert len(values) == 24


def test_a_recent_bolus_pulls_the_forecast_down():
    values, curve = predict(
        sgv=180, history=flat_history(180),
        boluses=[(minutes_ago(10), 4.0)],
        pump_iob=3.8, cob=0, params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert curve == "IOB"
    assert values[-1] < 180
    assert all(later <= earlier + 1e-9
               for earlier, later in zip(values, values[1:]))


def test_carbs_on_board_choose_the_carb_curve_and_lift_it():
    with_carbs, curve = predict(
        sgv=120, history=flat_history(), boluses=[], pump_iob=0, cob=40,
        params={"isf": 50, "cr": 10}, now_ms=NOW)
    without, _ = predict(
        sgv=120, history=flat_history(), boluses=[], pump_iob=0, cob=0,
        params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert curve == "COB"
    assert with_carbs[-1] > without[-1]


def test_a_rising_trend_with_no_carbs_is_treated_as_an_unannounced_meal():
    rising = [(minutes_ago(5 * i), 120 + (10 - i) * 9) for i in range(8, 0, -1)]
    values, curve = predict(
        sgv=rising[-1][1], history=rising, boluses=[], pump_iob=0, cob=0,
        params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert curve == "UAM"
    assert values[0] > rising[-1][1]


def test_the_unannounced_meal_rise_runs_out():
    """oref decays the deviation over an hour rather than extrapolating it."""
    rising = [(minutes_ago(5 * i), 120 + (10 - i) * 9) for i in range(8, 0, -1)]
    values, _curve = predict(
        sgv=rising[-1][1], history=rising, boluses=[], pump_iob=0, cob=0,
        params={"isf": 50, "cr": 10}, now_ms=NOW, steps=24)
    late_rise = values[-1] - values[-2]
    early_rise = values[1] - values[0]
    assert late_rise < early_rise


def test_a_flat_line_with_no_insulin_stays_flat():
    values, curve = predict(120, flat_history(), [], 0, 0, None, NOW)
    assert curve == "IOB"
    assert values == pytest.approx([120.0] * len(values))


@pytest.mark.parametrize("sgv", [39, 401, 30, 500])
def test_predictions_stay_inside_the_display_clamp(sgv):
    values, _curve = predict(
        sgv=sgv, history=flat_history(sgv), boluses=[(minutes_ago(5), 20.0)],
        pump_iob=19.0, cob=200, params={"isf": 100, "cr": 5}, now_ms=NOW)
    assert all(CLAMP_LO <= value <= CLAMP_HI for value in values)


def test_reported_iob_with_no_visible_boluses_still_bends_the_curve():
    """A device that uploads IOB but no bolus history is the common case."""
    values, _curve = predict(
        sgv=180, history=flat_history(180), boluses=[], pump_iob=3.0, cob=0,
        params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert values[-1] < 175


def test_negative_reported_iob_pushes_the_curve_up():
    """Below-target basal reductions show up as negative IOB."""
    values, _curve = predict(
        sgv=90, history=flat_history(90), boluses=[], pump_iob=-1.0, cob=0,
        params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert values[-1] > 90


def test_computed_insulin_is_rescaled_towards_the_pumps_own_iob():
    """The pump sees basal and micro-boluses that never reach us."""
    modest, _ = predict(180, flat_history(180), [(minutes_ago(10), 1.0)],
                        1.0, 0, {"isf": 50, "cr": 10}, NOW)
    scaled, _ = predict(180, flat_history(180), [(minutes_ago(10), 1.0)],
                        3.5, 0, {"isf": 50, "cr": 10}, NOW)
    assert scaled[-1] < modest[-1]


def test_the_rescale_is_bounded():
    """An absurd reported IOB must not produce an absurd forecast."""
    values, _curve = predict(
        sgv=180, history=flat_history(180), boluses=[(minutes_ago(10), 1.0)],
        pump_iob=500.0, cob=0, params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert values[-1] >= CLAMP_LO


def test_boluses_older_than_the_insulin_duration_are_ignored():
    stale, _ = predict(120, flat_history(), [(minutes_ago(10 * 60), 5.0)],
                       None, 0, None, NOW)
    none_at_all, _ = predict(120, flat_history(), [], None, 0, None, NOW)
    assert stale == pytest.approx(none_at_all)


def test_a_future_dated_bolus_is_ignored():
    values, _curve = predict(120, flat_history(), [(NOW + 60 * MINUTE, 5.0)],
                             None, 0, None, NOW)
    assert values == pytest.approx([120.0] * len(values))


def test_history_gaps_do_not_become_deviations():
    """A 45-minute hole is a sensor outage, not a 45-minute rise."""
    gappy = [(minutes_ago(50), 100.0), (minutes_ago(5), 160.0)]
    values, curve = predict(160, gappy, [], 0, 0, None, NOW)
    assert curve == "IOB"
    assert values == pytest.approx([160.0] * len(values))


def test_no_history_at_all_is_survivable():
    values, curve = predict(120, [], [], None, None, None, NOW)
    assert len(values) == 24
    assert curve == "IOB"


def test_steps_is_honoured():
    for steps in (6, 12, 36):
        values, _curve = predict(120, flat_history(), [], 0, 0, None, NOW,
                                 steps=steps)
        assert len(values) == steps


def test_carb_impact_has_a_floor_while_carbs_remain():
    """oref assumes a minimum absorption rather than believing a flat line."""
    values, curve = predict(
        sgv=120, history=flat_history(), boluses=[], pump_iob=0, cob=60,
        params={"isf": 50, "cr": 10}, now_ms=NOW, steps=6)
    assert curve == "COB"
    assert values[0] >= 120 + oref.MIN_5M_CARBIMPACT - 1e-6


def test_every_value_is_a_finite_number():
    values, _curve = predict(
        sgv=120, history=flat_history(), boluses=[(minutes_ago(30), 2.0)],
        pump_iob=1.5, cob=25, params={"isf": 50, "cr": 10}, now_ms=NOW)
    assert all(math.isfinite(value) for value in values)
