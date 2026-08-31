/*
 * A port of glucocube/predict.py. See gc_oref.c on why this follows the
 * Python's shape rather than a tidier one.
 */

#include "gc_predict.h"

#include <fenv.h>
#include <math.h>
#include <string.h>

#include "gc_oref.h"

/* Two hours of five-minute steps: what the chart draws. */
#define FORECAST_STEPS (GC_HORIZON_FAR / 5)

static int series_from_oref(const gc_snapshot_t *snap, int64_t now_ms,
                            float *values, int capacity)
{
    if (capacity < 1) {
        return 0;
    }
    gc_oref_input_t input = {
        .sgv = snap->sgv,
        .history = snap->history,
        .history_count = snap->history_count,
        .boluses = snap->boluses,
        .bolus_count = snap->bolus_count,
        .has_pump_iob = snap->has_iob,
        .pump_iob = snap->iob,
        .has_cob = snap->has_cob,
        .cob = snap->cob,
        .params = &snap->params,
        .now_ms = now_ms,
    };
    int steps = FORECAST_STEPS;
    if (steps > capacity - 1) {
        steps = capacity - 1;
    }
    /* Index 0 sits at start_ms, so the current reading leads the series —
     * the same convention the device curves use. */
    values[0] = snap->sgv;
    int written = gc_oref_predict(&input, steps, values + 1, NULL);
    return written > 0 ? written + 1 : 1;
}

bool gc_predict(const gc_snapshot_t *snap, int64_t now_ms, gc_forecast_t *out)
{
    if (snap == NULL || out == NULL) {
        return false;
    }
    memset(out, 0, sizeof(*out));

    float values[GC_MAX_PRED];
    int count = 0;
    int64_t start_ms = 0;
    bool estimated = false;

    if (snap->has_status && snap->device_pred.valid
        && now_ms - snap->status_date <= GC_MAX_PREDICTION_AGE_MS) {
        count = snap->device_pred.count;
        if (count > GC_MAX_PRED) {
            count = GC_MAX_PRED;
        }
        memcpy(values, snap->device_pred.values, (size_t)count * sizeof(float));
        start_ms = snap->device_pred.start_ms;
    }

    if (count == 0) {
        if (!snap->has_sgv || snap->sgv_date == 0) {
            return false;
        }
        if (now_ms - snap->sgv_date > GC_MAX_PREDICTION_AGE_MS) {
            return false;
        }
        start_ms = now_ms;
        count = series_from_oref(snap, now_ms, values, GC_MAX_PRED);
        estimated = true;
    }

    if (count <= 0) {
        return false;
    }

    /* Horizons. Python rounds with round(), which is round-half-to-even;
     * nearbyint() in the default rounding mode is the same, and the two
     * disagree on exact halves often enough to matter when a pump's
     * timestamp lands on a 2.5-minute boundary. */
    bool any = false;
    for (int i = 0; i < GC_HORIZON_COUNT; i++) {
        double offset = (double)(now_ms + (int64_t)gc_horizons[i] * 60000 - start_ms);
        double idx = nearbyint(offset / (double)GC_STEP_MS);
        if (idx < 0) {
            continue;
        }
        int index = (int)idx;
        if (index > count - 1) {
            index = count - 1;
        }
        out->horizons[i] = values[index];
        out->horizon_valid[i] = true;
        any = true;
    }
    if (!any) {
        return false;
    }

    int64_t far_ms = now_ms + (int64_t)GC_HORIZON_FAR * 60000;
    for (int i = 0; i < count && out->series_count < GC_MAX_PRED; i++) {
        int64_t at = start_ms + (int64_t)i * GC_STEP_MS;
        if (at > now_ms && at <= far_ms) {
            out->series[out->series_count].ms = at;
            out->series[out->series_count].value = values[i];
            out->series_count++;
        }
    }

    out->estimated = estimated;
    out->valid = true;
    return true;
}

bool gc_forecast_at(const gc_forecast_t *forecast, int minutes, float *out_value)
{
    if (forecast == NULL || !forecast->valid) {
        return false;
    }
    for (int i = 0; i < GC_HORIZON_COUNT; i++) {
        if (gc_horizons[i] == minutes && forecast->horizon_valid[i]) {
            if (out_value != NULL) {
                *out_value = forecast->horizons[i];
            }
            return true;
        }
    }
    return false;
}
