"""oref0-style glucose prediction (openaps `determine-basal` flavor).

Implements the core forecasting math of oref0 for display purposes:

- Exponential insulin-activity model (oref0 lib/iob/calculate.js): each bolus
  contributes activity over the insulin duration with a configurable peak.
- Blood-glucose impact (BGI): -activity * ISF per 5-minute step.
- Deviations: how much recent BG movement differs from what insulin alone
  explains — oref's signal for carb absorption / unannounced meals.
- Prediction arrays: IOBpredBG (insulin only), COBpredBG (carb absorption
  decaying linearly, total area = COB * CSF), UAMpredBG (deviation decaying
  over 60 minutes) — clamped to oref's 39..401 display range.

Inputs come from what a monitor can see: bolus history, pump-reported
IOB/COB, and therapy settings (ISF/CR/DIA) pulled from the user's Nightscout
profile or Tidepool pump settings, with defaults when unknown. Computed IOB
from known boluses is rescaled to match the pump's reported IOB, which
absorbs what we can't see (basal modulation, micro-boluses).

This is for display only — it informs a wall monitor, not dosing.
"""

import math
from dataclasses import dataclass

from .contract import (
    CLAMP_HI,
    CLAMP_LO,
    DEVIATION_MAX_GAP_MIN,
    DEVIATION_MIN_GAP_MIN,
    DEVIATION_WINDOW_MS,
    IOB_SCALE_MAX,
    IOB_SCALE_MIN,
    MIN_5M_CARBIMPACT,
    PEAK_MAX_FRACTION_OF_DIA,
    STEP_MIN,
    SYNTHETIC_BOLUS_AGE_MIN,
    SYNTHETIC_BOLUS_MIN_FRAC,
    THERAPY_DEFAULTS,
    THERAPY_RANGES,
    UAM_DECAY_STEPS,
    UAM_DEVIATION_THRESHOLD,
)


@dataclass
class Therapy:
    isf: float = THERAPY_DEFAULTS["isf"]              # mg/dL per U
    cr: float = THERAPY_DEFAULTS["cr"]                # g per U
    dia_hours: float = THERAPY_DEFAULTS["dia_hours"]
    peak_min: float = THERAPY_DEFAULTS["peak_min"]    # oref0 exponential model


def therapy_from_params(params: dict | None) -> Therapy:
    """Build therapy settings, accepting only physiologically plausible
    values — profile endpoints sometimes carry placeholder junk (e.g. Trio
    uploads a dummy Nightscout profile with sens=720, carbratio=200)."""
    params = params or {}
    t = Therapy()

    def plausible(key, lo, hi):
        value = params.get(key)
        return float(value) if value and lo <= value <= hi else None

    for key in ("isf", "cr", "dia_hours", "peak_min"):
        value = plausible(key, *THERAPY_RANGES[key])
        if value is not None:
            setattr(t, key, value)
    return t


def insulin_model(td_min: float, tp_min: float):
    """oref0 exponential insulin curves. Returns (activity(t,u), iob_frac(t)).

    activity is per-minute glucose-lowering activity for a bolus of u units
    at age t minutes; iob_frac is the fraction of a bolus still active.

    The peak is clamped below half the duration: the model's tau term
    divides by zero at exactly peak == duration / 2, and a profile saying
    "3-hour DIA, 90-minute peak" — both values a Nightscout profile is
    allowed to carry — is that case.
    """
    td = td_min
    tp = min(tp_min, td * PEAK_MAX_FRACTION_OF_DIA)
    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))

    def activity(t: float, u: float) -> float:
        if t <= 0 or t >= td:
            return 0.0
        return u * (s / tau**2) * t * (1 - t / td) * math.exp(-t / tau)

    def iob_frac(t: float) -> float:
        if t <= 0:
            return 1.0
        if t >= td:
            return 0.0
        return 1 - s * (1 - a) * (
            (t**2 / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
        )

    return activity, iob_frac


def predict(
    sgv: float,
    history: list[tuple[int, float]],
    boluses: list[tuple[int, float]],
    pump_iob: float | None,
    cob: float | None,
    params: dict | None,
    now_ms: int,
    steps: int = 24,
) -> tuple[list[float], str]:
    """Return (values, curve_name) — 5-min predBGs starting at now+5m.

    curve_name is which oref array was chosen for display:
    "COB", "UAM", or "IOB".
    """
    therapy = therapy_from_params(params)
    td = therapy.dia_hours * 60
    activity_fn, iob_frac = insulin_model(td, therapy.peak_min)

    # --- effective bolus list, rescaled to the pump's reported IOB ---
    known = [
        ((now_ms - t) / 60000.0, u)
        for t, u in boluses
        if 0 <= (now_ms - t) / 60000.0 < td and u > 0
    ]
    computed_iob = sum(u * iob_frac(age) for age, u in known)
    scale = 1.0
    if pump_iob is not None:
        if computed_iob > 0.1:
            scale = max(IOB_SCALE_MIN, min(IOB_SCALE_MAX, pump_iob / computed_iob))
        else:
            # No visible boluses: model the reported IOB as one synthetic
            # bolus about an hour old (mid-decay). Works for negative IOB
            # too, which then correctly pushes predictions upward.
            known = [(SYNTHETIC_BOLUS_AGE_MIN,
                      pump_iob / max(iob_frac(SYNTHETIC_BOLUS_AGE_MIN),
                                     SYNTHETIC_BOLUS_MIN_FRAC))]

    def activity_at(minutes_ahead: float) -> float:
        return scale * sum(
            activity_fn(age + minutes_ahead, u) for age, u in known
        )

    # --- deviations: actual BG movement minus insulin-explained movement ---
    recent = [(t, v) for t, v in history if now_ms - t <= DEVIATION_WINDOW_MS]
    deviations = []
    for (t0, v0), (t1, v1) in zip(recent, recent[1:]):
        gap_min = (t1 - t0) / 60000.0
        if not DEVIATION_MIN_GAP_MIN <= gap_min <= DEVIATION_MAX_GAP_MIN:
            continue
        age_mid = (now_ms - (t0 + t1) / 2) / 60000.0
        expected = -scale * sum(
            activity_fn(age - age_mid, u) for age, u in known
        ) * therapy.isf * gap_min
        actual = v1 - v0
        deviations.append((actual - expected) * (STEP_MIN / gap_min))
    avg_dev = sum(deviations) / len(deviations) if deviations else 0.0

    # --- carb impact (oref: deviation-driven, floored while COB remains) ---
    cob = max(0.0, cob or 0.0)
    csf = therapy.isf / therapy.cr           # mg/dL per gram
    ci = max(avg_dev, MIN_5M_CARBIMPACT if cob > 0 else 0.0)
    # Linear decay duration so the area under predCI equals COB * CSF.
    cob_steps = (2 * cob * csf / ci) / STEP_MIN if (cob > 0 and ci > 0) else 0.0

    iob_pred, cob_pred, uam_pred = [sgv], [sgv], [sgv]
    for i in range(steps):
        bgi = -activity_at((i + 0.5) * STEP_MIN) * therapy.isf * STEP_MIN
        pred_ci = ci * max(0.0, 1 - i / cob_steps) if cob_steps > 0 else 0.0
        uam_ci = avg_dev * max(0.0, 1 - i / UAM_DECAY_STEPS)
        clamp = lambda v: max(CLAMP_LO, min(CLAMP_HI, v))
        iob_pred.append(clamp(iob_pred[-1] + bgi))
        cob_pred.append(clamp(cob_pred[-1] + bgi + pred_ci))
        uam_pred.append(clamp(uam_pred[-1] + bgi + uam_ci))

    if cob > 0:
        return cob_pred[1:], "COB"
    if abs(avg_dev) > UAM_DEVIATION_THRESHOLD:
        return uam_pred[1:], "UAM"
    return iob_pred[1:], "IOB"
