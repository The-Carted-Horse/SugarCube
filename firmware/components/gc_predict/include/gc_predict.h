/*
 * Which forecast the display shows, and where it came from.
 *
 * A port of glucocube/predict.py. The preferred source is the AID system's
 * own prediction curve — oref-family systems upload openaps.suggested.predBGs
 * and Loop-family systems upload loop.predicted — because those models know
 * the person's insulin activity and carb absorption. The fallback is our own
 * oref0-style forecast (gc_oref), marked with "~" on screen.
 *
 * The Pi keeps the whole devicestatus document and re-reads it every frame.
 * Here the curve is pulled out once, when the document arrives (see
 * gc_sources), and what survives is gc_device_pred_t.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "gc_contract.h"
#include "gc_store.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool valid;

    /* Indexed like gc_horizons[]: 30, 60, 90 and 120 minutes out. A horizon
     * the series does not reach is simply not set. */
    bool horizon_valid[GC_HORIZON_COUNT];
    float horizons[GC_HORIZON_COUNT];

    /* The 5-minute series between now and the far horizon, which is what the
     * chart plots. */
    int series_count;
    gc_point_t series[GC_MAX_PRED];

    /* true when this is our own estimate rather than the pump's own curve;
     * the UI writes "~" in front of an estimated value. */
    bool estimated;
} gc_forecast_t;

/*
 * Fills out from the snapshot. Returns false — with out->valid false — when
 * there is nothing sane to predict from: no device curve fresh enough, and
 * no reading fresh enough to run our own model on.
 */
bool gc_predict(const gc_snapshot_t *snap, int64_t now_ms, gc_forecast_t *out);

/* The value at a horizon, or false if the forecast does not reach it.
 * `minutes` is one of the values in gc_horizons[]. */
bool gc_forecast_at(const gc_forecast_t *forecast, int minutes, float *out_value);

#ifdef __cplusplus
}
#endif
