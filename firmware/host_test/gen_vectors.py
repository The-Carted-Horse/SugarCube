#!/usr/bin/env python3
"""Golden forecast vectors, generated from the Python and checked by the C.

The Raspberry Pi runs glucocube/oref.py and glucocube/predict.py; the ESP32
runs firmware/components/gc_oref and gc_predict, which are ports of them. A
port that is only nearly right shows a person a number their pump does not
agree with, so the two are pinned together here: this script runs the Python
over a spread of deliberately awkward inputs and writes the answers out as C
arrays, and firmware/host_test/test_parity.c runs the C over the same inputs
and compares.

    python3 firmware/host_test/gen_vectors.py     # regenerate
    make -C firmware/host_test                    # build and run the check

``tests/test_contract.py`` also runs it with --check, so a change to the
Python forecast that is not mirrored in the C fails CI rather than shipping.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from glucocube import contract, oref, predict  # noqa: E402
from glucocube.store import UserSnapshot  # noqa: E402

OUT = "firmware/host_test/vectors.c"
OUT_H = "firmware/host_test/vectors.h"

NOW = 1_700_000_000_000          # a fixed instant; nothing here reads the clock
MINUTE = 60 * 1000
CURVES = {"IOB": 0, "COB": 1, "UAM": 2}


def history(count, start_value, per_step, step_min=5, end_offset_min=0):
    """`count` readings ending `end_offset_min` ago, ascending."""
    out = []
    for i in range(count):
        ago = (count - 1 - i) * step_min + end_offset_min
        out.append((NOW - ago * MINUTE, start_value + per_step * i))
    return out


def bolus(minutes_ago, units):
    return (NOW - minutes_ago * MINUTE, units)


# Each case is exercised through oref.predict directly.
OREF_CASES = [
    ("flat_nothing_known", dict(
        sgv=120.0, history=history(12, 120, 0), boluses=[],
        pump_iob=None, cob=None, params=None)),
    ("flat_with_two_boluses", dict(
        sgv=142.0, history=history(12, 142, 0),
        boluses=[bolus(90, 2.5), bolus(25, 1.2)],
        pump_iob=2.4, cob=None, params=None)),
    ("pump_iob_but_no_visible_boluses", dict(
        sgv=175.0, history=history(10, 160, 1.5), boluses=[],
        pump_iob=3.2, cob=None, params=None)),
    ("negative_pump_iob", dict(
        sgv=88.0, history=history(10, 100, -1.2), boluses=[],
        pump_iob=-0.8, cob=None, params=None)),
    ("carbs_on_board", dict(
        sgv=134.0, history=history(12, 120, 1.2),
        boluses=[bolus(20, 4.0)], pump_iob=3.8, cob=45.0, params=None)),
    ("carbs_on_board_tiny", dict(
        sgv=101.0, history=history(12, 101, 0), boluses=[],
        pump_iob=None, cob=3.0, params=None)),
    ("rising_hard_unannounced_meal", dict(
        sgv=196.0, history=history(10, 150, 5.2), boluses=[],
        pump_iob=0.0, cob=None, params=None)),
    ("falling_into_the_low_clamp", dict(
        sgv=62.0, history=history(10, 130, -7.5),
        boluses=[bolus(30, 6.0)], pump_iob=5.4, cob=None, params=None)),
    ("high_and_climbing_into_the_top_clamp", dict(
        sgv=352.0, history=history(10, 300, 5.5), boluses=[],
        pump_iob=None, cob=120.0, params={"isf": 20.0, "cr": 4.0})),
    ("implausible_profile_falls_back_to_defaults", dict(
        sgv=150.0, history=history(12, 150, 0), boluses=[bolus(45, 3.0)],
        pump_iob=2.0, cob=None,
        params={"isf": 720.0, "cr": 200.0, "dia_hours": 0.5, "peak_min": 900.0})),
    ("custom_but_plausible_profile", dict(
        sgv=150.0, history=history(12, 150, 0), boluses=[bolus(45, 3.0)],
        pump_iob=2.0, cob=None,
        params={"isf": 35.0, "cr": 8.0, "dia_hours": 5.0, "peak_min": 55.0})),
    ("gappy_history_skips_wide_pairs", dict(
        sgv=145.0,
        history=[(NOW - 40 * MINUTE, 120.0), (NOW - 25 * MINUTE, 132.0),
                 (NOW - 20 * MINUTE, 138.0), (NOW - 5 * MINUTE, 145.0)],
        boluses=[], pump_iob=None, cob=None, params=None)),
    ("one_minute_cadence_skips_narrow_pairs", dict(
        sgv=160.0, history=history(20, 150, 0.5, step_min=1),
        boluses=[], pump_iob=None, cob=None, params=None)),
    ("history_older_than_the_deviation_window", dict(
        sgv=130.0, history=history(8, 120, 1.5, step_min=5, end_offset_min=60),
        boluses=[], pump_iob=None, cob=None, params=None)),
    ("peak_at_half_the_duration", dict(
        # 3-hour DIA with a 90-minute peak: both inside THERAPY_RANGES, and
        # exactly where the model's tau term would divide by zero.
        sgv=140.0, history=history(12, 140, 0), boluses=[bolus(20, 2.0)],
        pump_iob=1.8, cob=None,
        params={"dia_hours": 3.0, "peak_min": 90.0})),
    ("peak_past_half_the_duration", dict(
        sgv=140.0, history=history(12, 140, 0), boluses=[bolus(20, 2.0)],
        pump_iob=1.8, cob=None,
        params={"dia_hours": 3.0, "peak_min": 120.0})),
    ("scale_clamped_at_the_ceiling", dict(
        sgv=200.0, history=history(12, 200, 0), boluses=[bolus(200, 0.2)],
        pump_iob=9.0, cob=None, params=None)),
    ("scale_clamped_at_the_floor", dict(
        sgv=200.0, history=history(12, 200, 0),
        boluses=[bolus(10, 9.0), bolus(30, 8.0)],
        pump_iob=0.4, cob=None, params=None)),
]

STEPS = contract.HORIZONS[-1] // 5


def snapshot_case(**kwargs) -> UserSnapshot:
    snap = UserSnapshot()
    for key, value in kwargs.items():
        setattr(snap, key, value)
    return snap


# Exercised through predict.predict, which chooses between the pump's own
# curve and ours and then indexes horizons out of whichever won.
PREDICT_CASES = [
    ("device_curve_fresh", snapshot_case(
        sgv=140.0, sgv_date=NOW - 2 * MINUTE, direction="Flat",
        history=history(12, 140, 0), boluses=[], iob=1.0, cob=0.0,
        status_date=NOW - 3 * MINUTE,
        status_raw={"openaps": {"suggested": {
            "timestamp": NOW - 3 * MINUTE,
            "eventualBG": 120,
            "predBGs": {"IOB": [140, 138, 135, 131, 128, 125, 123, 121,
                                120, 120, 119, 119, 118, 118, 118, 118,
                                118, 118, 118, 118, 118, 118, 118, 118,
                                118, 118]},
        }}})),
    ("device_curve_stale_falls_back", snapshot_case(
        sgv=140.0, sgv_date=NOW - 2 * MINUTE, direction="Flat",
        history=history(12, 140, 0), boluses=[], iob=1.0, cob=0.0,
        status_date=NOW - 40 * MINUTE,
        status_raw={"openaps": {"suggested": {
            "timestamp": NOW - 40 * MINUTE,
            "predBGs": {"IOB": [140, 138, 135]},
        }}})),
    ("loop_style_curve", snapshot_case(
        sgv=155.0, sgv_date=NOW - 1 * MINUTE, direction="FortyFiveUp",
        history=history(12, 150, 0.5), boluses=[], iob=0.5, cob=12.0,
        status_date=NOW - 1 * MINUTE,
        status_raw={"loop": {"predicted": {
            "startDate": NOW - 1 * MINUTE,
            "values": [155, 158, 161, 163, 164, 165, 165, 164, 163, 161,
                       160, 158, 157, 156, 155, 154, 153, 152, 151, 150,
                       149, 148, 147, 146, 145],
        }}})),
    ("no_reading_at_all", snapshot_case(
        history=[], boluses=[])),
    ("reading_too_old_to_forecast", snapshot_case(
        sgv=120.0, sgv_date=NOW - 45 * MINUTE, direction="Flat",
        history=history(6, 120, 0, end_offset_min=45), boluses=[])),
    ("estimate_from_a_fresh_reading", snapshot_case(
        sgv=118.0, sgv_date=NOW - 4 * MINUTE, direction="Flat",
        history=history(12, 118, 0), boluses=[bolus(35, 2.0)],
        iob=1.6, cob=0.0)),
]


def cfloat(value) -> str:
    return repr(float(value)) + "f"


def emit_points(name, points, out):
    if not points:
        out.append(f"static const gc_point_t {name}[1] = {{{{0, 0.0f}}}};")
        return 0
    out.append(f"static const gc_point_t {name}[{len(points)}] = {{")
    for ms, value in points:
        out.append(f"    {{{int(ms)}LL, {cfloat(value)}}},")
    out.append("};")
    return len(points)


def emit_floats(name, values, out):
    if not values:
        out.append(f"static const float {name}[1] = {{0.0f}};")
        return 0
    out.append(f"static const float {name}[{len(values)}] = {{")
    literals = [cfloat(v) for v in values]
    for i in range(0, len(literals), 6):
        out.append("    " + ", ".join(literals[i:i + 6]) + ",")
    out.append("};")
    return len(values)


def params_fields(params) -> str:
    params = params or {}
    parts = []
    for key in ("isf", "cr", "dia_hours", "peak_min"):
        if key in params:
            parts.append(f".{key} = {cfloat(params[key])}, .has_{key} = true")
        else:
            parts.append(f".has_{key} = false")
    return "{" + ", ".join(parts) + "}"


def gen_header() -> str:
    return f"""/*
 * Generated by firmware/host_test/gen_vectors.py — do not edit by hand.
 * Expected values come from running glucocube/oref.py and predict.py.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "gc_oref.h"
#include "gc_predict.h"
#include "gc_store.h"

#define GC_VECTOR_NOW {NOW}LL
#define GC_VECTOR_STEPS {STEPS}

typedef struct {{
    const char *name;
    float sgv;
    const gc_point_t *history;
    int history_count;
    const gc_point_t *boluses;
    int bolus_count;
    bool has_pump_iob;
    float pump_iob;
    bool has_cob;
    float cob;
    gc_params_t params;
    int steps;
    const float *expected;
    int expected_count;
    gc_curve_t expected_curve;
}} gc_oref_vector_t;

typedef struct {{
    const char *name;
    gc_snapshot_t snapshot;
    bool expect_valid;
    bool expect_estimated;
    bool horizon_valid[GC_HORIZON_COUNT];
    float horizons[GC_HORIZON_COUNT];
    int series_count;
    const gc_point_t *series;
}} gc_predict_vector_t;

extern const gc_oref_vector_t gc_oref_vectors[];
extern const int gc_oref_vector_count;
extern const gc_predict_vector_t gc_predict_vectors[];
extern const int gc_predict_vector_count;

/* The snapshot in a predict vector carries counts but not the arrays
 * themselves — they are too long to sit in a designated initialiser. The
 * runner copies them in from these. */
const gc_point_t *gc_predict_history(int index);
const gc_point_t *gc_predict_boluses(int index);
const float *gc_predict_device_values(int index);
"""


def gen_source() -> str:
    out = ["/*",
           " * Generated by firmware/host_test/gen_vectors.py — do not edit by hand.",
           " */",
           "",
           '#include "vectors.h"',
           ""]

    # ---- oref vectors ----
    entries = []
    for index, (name, case) in enumerate(OREF_CASES):
        values, curve = oref.predict(
            sgv=case["sgv"], history=case["history"], boluses=case["boluses"],
            pump_iob=case["pump_iob"], cob=case["cob"], params=case["params"],
            now_ms=NOW, steps=STEPS,
        )
        hist_n = emit_points(f"oref_hist_{index}", case["history"], out)
        bol_n = emit_points(f"oref_bolus_{index}", case["boluses"], out)
        exp_n = emit_floats(f"oref_expect_{index}", values, out)
        out.append("")
        entries.append(
            f'    {{\n'
            f'        .name = "{name}",\n'
            f'        .sgv = {cfloat(case["sgv"])},\n'
            f'        .history = oref_hist_{index}, .history_count = {hist_n},\n'
            f'        .boluses = oref_bolus_{index}, .bolus_count = {bol_n},\n'
            f'        .has_pump_iob = {"true" if case["pump_iob"] is not None else "false"},'
            f' .pump_iob = {cfloat(case["pump_iob"] or 0.0)},\n'
            f'        .has_cob = {"true" if case["cob"] is not None else "false"},'
            f' .cob = {cfloat(case["cob"] or 0.0)},\n'
            f'        .params = {params_fields(case["params"])},\n'
            f'        .steps = {STEPS},\n'
            f'        .expected = oref_expect_{index}, .expected_count = {exp_n},\n'
            f'        .expected_curve = {["GC_CURVE_IOB", "GC_CURVE_COB", "GC_CURVE_UAM"][CURVES[curve]]},\n'
            f'    }},'
        )
    out.append("const gc_oref_vector_t gc_oref_vectors[] = {")
    out += entries
    out.append("};")
    out.append(f"const int gc_oref_vector_count = {len(OREF_CASES)};")
    out.append("")

    # ---- predict vectors ----
    entries = []
    for index, (name, snap) in enumerate(PREDICT_CASES):
        horizons, series, source = predict.predict(snap, NOW)
        hist_n = emit_points(f"pred_hist_{index}", snap.history, out)
        bol_n = emit_points(f"pred_bolus_{index}", snap.boluses, out)
        series_n = emit_points(f"pred_series_{index}", series or [], out)

        device = predict.device_series(snap.status_raw or {})
        fresh = (snap.status_raw and snap.status_date
                 and NOW - snap.status_date <= contract.MAX_PREDICTION_AGE_MS)
        if device and fresh:
            start, values = device
            values = list(values)[:contract.HORIZONS[-1] // 5 + 2]
            pred_n = emit_floats(f"pred_device_{index}", values, out)
            device_block = (
                f".device_pred = {{.valid = true, .start_ms = {int(start)}LL, "
                f".count = {pred_n}}}")
            copy = f"pred_device_{index}"
        else:
            device_block = ".device_pred = {.valid = false}"
            copy = None
        out.append("")

        horizon_valid = []
        horizon_values = []
        for h in contract.HORIZONS:
            present = bool(horizons) and h in horizons
            horizon_valid.append("true" if present else "false")
            horizon_values.append(cfloat(horizons[h] if present else 0.0))

        entries.append(
            f'    {{\n'
            f'        .name = "{name}",\n'
            f'        .snapshot = {{\n'
            f'            .has_sgv = {"true" if snap.sgv is not None else "false"},'
            f' .sgv = {cfloat(snap.sgv or 0.0)},'
            f' .sgv_date = {int(snap.sgv_date or 0)}LL,\n'
            f'            .history = {{{{0, 0.0f}}}}, .history_count = {hist_n},\n'
            f'            .boluses = {{{{0, 0.0f}}}}, .bolus_count = {bol_n},\n'
            f'            .has_iob = {"true" if snap.iob is not None else "false"},'
            f' .iob = {cfloat(snap.iob or 0.0)},\n'
            f'            .has_cob = {"true" if snap.cob is not None else "false"},'
            f' .cob = {cfloat(snap.cob or 0.0)},\n'
            f'            .has_status = {"true" if snap.status_date else "false"},'
            f' .status_date = {int(snap.status_date or 0)}LL,\n'
            f'            {device_block},\n'
            f'            .params = {params_fields(snap.params)},\n'
            f'        }},\n'
            f'        .expect_valid = {"true" if horizons else "false"},\n'
            f'        .expect_estimated = {"true" if source == "est" else "false"},\n'
            f'        .horizon_valid = {{{", ".join(horizon_valid)}}},\n'
            f'        .horizons = {{{", ".join(horizon_values)}}},\n'
            f'        .series_count = {series_n},\n'
            f'        .series = pred_series_{index},\n'
            f'    }},'
        )
        # The snapshot's flexible arrays are filled in by the runner, which
        # knows the source arrays by name.
        out.append(f"const gc_point_t *gc_pred_hist_{index} = pred_hist_{index};")
        out.append(f"const gc_point_t *gc_pred_bolus_{index} = pred_bolus_{index};")
        if copy:
            out.append(f"const float *gc_pred_device_{index} = {copy};")
        else:
            out.append(f"const float *gc_pred_device_{index} = NULL;")
        out.append("")

    out.append("const gc_predict_vector_t gc_predict_vectors[] = {")
    out += entries
    out.append("};")
    out.append(f"const int gc_predict_vector_count = {len(PREDICT_CASES)};")
    out.append("")

    # Accessors so the runner can copy the variable-length arrays into the
    # fixed-size snapshot fields without knowing each case by name.
    out.append("const gc_point_t *gc_predict_history(int index) {")
    out.append("    switch (index) {")
    for index in range(len(PREDICT_CASES)):
        out.append(f"    case {index}: return gc_pred_hist_{index};")
    out.append("    default: return NULL;")
    out.append("    }")
    out.append("}")
    out.append("")
    out.append("const gc_point_t *gc_predict_boluses(int index) {")
    out.append("    switch (index) {")
    for index in range(len(PREDICT_CASES)):
        out.append(f"    case {index}: return gc_pred_bolus_{index};")
    out.append("    default: return NULL;")
    out.append("    }")
    out.append("}")
    out.append("")
    out.append("const float *gc_predict_device_values(int index) {")
    out.append("    switch (index) {")
    for index in range(len(PREDICT_CASES)):
        out.append(f"    case {index}: return gc_pred_device_{index};")
    out.append("    default: return NULL;")
    out.append("    }")
    out.append("}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    root = Path(args.out)
    files = {root / OUT_H: gen_header(), root / OUT: gen_source()}
    stale = []
    for path, content in files.items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {path}")
    if stale:
        print("out of date: " + ", ".join(str(p) for p in stale), file=sys.stderr)
        print("run: python3 firmware/host_test/gen_vectors.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
