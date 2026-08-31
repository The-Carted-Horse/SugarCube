/*
 * oref0-style glucose prediction, in C.
 *
 * A line-for-line port of glucocube/oref.py, which is itself the core
 * forecasting math of oref0's determine-basal for display purposes:
 * exponential insulin activity, blood-glucose impact, deviation-driven
 * carb absorption, and the IOB/COB/UAM prediction arrays.
 *
 * Every constant comes from gc_contract.h, generated from the same
 * glucocube/contract.py the Python reads, so the two cannot drift. The host
 * test in firmware/host_test/ runs both against shared golden vectors.
 *
 * This is for display only — it informs a wall monitor, not dosing.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "gc_contract.h"
#include "gc_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The most 5-minute steps a caller may ask for; two hours is 24. */
#define GC_OREF_MAX_STEPS 48

typedef struct {
    float isf;        /* mg/dL per U */
    float cr;         /* g per U */
    float dia_hours;
    float peak_min;
} gc_therapy_t;

typedef enum {
    GC_CURVE_IOB,
    GC_CURVE_COB,
    GC_CURVE_UAM,
} gc_curve_t;

/* Therapy settings with implausible values dropped, exactly as
 * oref.therapy_from_params does: a Nightscout profile that says the ISF is
 * 720 is a placeholder, not a person. */
gc_therapy_t gc_therapy_from_params(const gc_params_t *params);

/* The oref0 exponential insulin curves for a given duration and peak.
 * gc_insulin_activity is per-minute glucose-lowering activity for a bolus
 * of u units at age t minutes; gc_insulin_iob_frac is the fraction of a
 * bolus still active at age t. */
typedef struct {
    float td_min;
    float tp_min;
    float tau;
    float a;
    float s;
} gc_insulin_model_t;

gc_insulin_model_t gc_insulin_model(float td_min, float tp_min);
float gc_insulin_activity(const gc_insulin_model_t *model, float t_min, float units);
float gc_insulin_iob_frac(const gc_insulin_model_t *model, float t_min);

typedef struct {
    float sgv;

    const gc_point_t *history;  /* (ms, mg/dL), ascending */
    int history_count;

    const gc_point_t *boluses;  /* (ms, units), ascending */
    int bolus_count;

    bool has_pump_iob;
    float pump_iob;
    bool has_cob;
    float cob;

    const gc_params_t *params;  /* may be NULL */

    int64_t now_ms;
} gc_oref_input_t;

/*
 * Writes `steps` five-minute predicted values into out_values, the first
 * being now + 5 minutes, and reports which oref array was chosen.
 *
 * Returns the number of values written, or 0 if the input is unusable
 * (no steps requested, or steps beyond GC_OREF_MAX_STEPS).
 */
int gc_oref_predict(const gc_oref_input_t *input, int steps,
                    float *out_values, gc_curve_t *out_curve);

#ifdef __cplusplus
}
#endif
