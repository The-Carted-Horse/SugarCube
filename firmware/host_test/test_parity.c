/*
 * Runs the firmware's forecast over the golden vectors and compares it with
 * what glucocube/oref.py and predict.py produced for the same inputs.
 *
 * Build and run:  make -C firmware/host_test
 *
 * Tolerance is 0.01 mg/dL. The Python works in doubles throughout; the C
 * does its arithmetic in doubles too but takes its inputs as floats, so the
 * two agree to about a thousandth of a mg/dL. Anything looser than that
 * would let a real divergence hide, and anything tighter would fail on the
 * float inputs alone.
 */

#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "gc_oref.h"
#include "gc_predict.h"
#include "gc_store.h"
#include "vectors.h"

#define TOLERANCE 0.01f

static int failures;
static int checks;

__attribute__((format(printf, 2, 3)))
static void fail(const char *vector, const char *fmt, ...)
{
    va_list args;
    printf("  FAIL %s: ", vector);
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
    printf("\n");
    failures++;
}

static bool close_enough(float a, float b)
{
    return fabsf(a - b) <= TOLERANCE;
}

static const char *curve_name(gc_curve_t curve)
{
    switch (curve) {
    case GC_CURVE_IOB: return "IOB";
    case GC_CURVE_COB: return "COB";
    case GC_CURVE_UAM: return "UAM";
    default: return "?";
    }
}

/* ------------------------------------------------------------- gc_oref -- */

static void run_oref_vectors(void)
{
    printf("oref (%d vectors)\n", gc_oref_vector_count);
    for (int i = 0; i < gc_oref_vector_count; i++) {
        const gc_oref_vector_t *v = &gc_oref_vectors[i];
        gc_oref_input_t input = {
            .sgv = v->sgv,
            .history = v->history,
            .history_count = v->history_count,
            .boluses = v->boluses,
            .bolus_count = v->bolus_count,
            .has_pump_iob = v->has_pump_iob,
            .pump_iob = v->pump_iob,
            .has_cob = v->has_cob,
            .cob = v->cob,
            .params = &v->params,
            .now_ms = GC_VECTOR_NOW,
        };
        float got[GC_OREF_MAX_STEPS];
        gc_curve_t curve = GC_CURVE_IOB;
        int n = gc_oref_predict(&input, v->steps, got, &curve);
        checks++;

        if (n != v->expected_count) {
            fail(v->name, "returned %d values, Python gave %d",
                 n, v->expected_count);
            continue;
        }
        if (curve != v->expected_curve) {
            fail(v->name, "chose the %s curve, Python chose %s",
                 curve_name(curve), curve_name(v->expected_curve));
            continue;
        }
        for (int step = 0; step < n; step++) {
            if (!close_enough(got[step], v->expected[step])) {
                fail(v->name, "step %d is %.4f, Python says %.4f",
                     step, got[step], v->expected[step]);
                break;
            }
        }
    }
}

/* ---------------------------------------------------------- gc_predict -- */

static void run_predict_vectors(void)
{
    printf("predict (%d vectors)\n", gc_predict_vector_count);
    for (int i = 0; i < gc_predict_vector_count; i++) {
        const gc_predict_vector_t *v = &gc_predict_vectors[i];

        /* The vector's snapshot carries counts; the arrays are copied in
         * here because they are too long for a designated initialiser. */
        gc_snapshot_t snap = v->snapshot;
        const gc_point_t *history = gc_predict_history(i);
        if (history != NULL && snap.history_count > 0) {
            memcpy(snap.history, history,
                   (size_t)snap.history_count * sizeof(gc_point_t));
        }
        const gc_point_t *boluses = gc_predict_boluses(i);
        if (boluses != NULL && snap.bolus_count > 0) {
            memcpy(snap.boluses, boluses,
                   (size_t)snap.bolus_count * sizeof(gc_point_t));
        }
        const float *device = gc_predict_device_values(i);
        if (device != NULL && snap.device_pred.count > 0) {
            memcpy(snap.device_pred.values, device,
                   (size_t)snap.device_pred.count * sizeof(float));
        }

        gc_forecast_t forecast;
        bool ok = gc_predict(&snap, GC_VECTOR_NOW, &forecast);
        checks++;

        if (ok != v->expect_valid) {
            fail(v->name, "returned %s, Python %s a forecast",
                 ok ? "a forecast" : "nothing",
                 v->expect_valid ? "gave" : "gave none");
            continue;
        }
        if (!ok) {
            continue;
        }
        if (forecast.estimated != v->expect_estimated) {
            fail(v->name, "source is %s, Python says %s",
                 forecast.estimated ? "est" : "device",
                 v->expect_estimated ? "est" : "device");
            continue;
        }
        for (int h = 0; h < GC_HORIZON_COUNT; h++) {
            if (forecast.horizon_valid[h] != v->horizon_valid[h]) {
                fail(v->name, "horizon %d present=%d, Python present=%d",
                     gc_horizons[h], forecast.horizon_valid[h],
                     v->horizon_valid[h]);
                break;
            }
            if (v->horizon_valid[h]
                && !close_enough(forecast.horizons[h], v->horizons[h])) {
                fail(v->name, "horizon %d is %.4f, Python says %.4f",
                     gc_horizons[h], forecast.horizons[h], v->horizons[h]);
                break;
            }
        }
        if (forecast.series_count != v->series_count) {
            fail(v->name, "series has %d points, Python has %d",
                 forecast.series_count, v->series_count);
            continue;
        }
        for (int p = 0; p < forecast.series_count; p++) {
            if (forecast.series[p].ms != v->series[p].ms) {
                fail(v->name, "series point %d is at %lld, Python at %lld",
                     p, (long long)forecast.series[p].ms,
                     (long long)v->series[p].ms);
                break;
            }
            if (!close_enough(forecast.series[p].value, v->series[p].value)) {
                fail(v->name, "series point %d is %.4f, Python says %.4f",
                     p, forecast.series[p].value, v->series[p].value);
                break;
            }
        }
    }
}

/* ------------------------------------------------------------ gc_store -- */

/* The store is not generated from Python vectors — its behaviour is stated
 * in gc_store.h and mirrors store.py's snapshot(), so it is checked here
 * directly. These are the rules the display depends on. */
static void run_store_checks(void)
{
    printf("store\n");
    gc_store_t *store = gc_store_create();
    if (store == NULL) {
        fail("store", "could not be created");
        return;
    }
    const int64_t now = GC_VECTOR_NOW;

    gc_entry_t entry = {.date_ms = now - 5 * 60000, .sgv = 120.0f};
    snprintf(entry.direction, sizeof(entry.direction), "Flat");
    gc_store_add_entry(store, 0, &entry);
    entry.date_ms = now - 10 * 60000;
    entry.sgv = 114.0f;
    gc_store_add_entry(store, 0, &entry);

    gc_snapshot_t snap;
    checks++;
    if (!gc_store_snapshot(store, 0, now, &snap)) {
        fail("store", "snapshot failed");
    } else {
        if (!snap.has_sgv || !close_enough(snap.sgv, 120.0f)) {
            fail("store/newest", "sgv is %.1f, expected 120", snap.sgv);
        }
        if (!snap.has_delta || !close_enough(snap.delta, 6.0f)) {
            fail("store/delta", "delta is %.1f, expected 6", snap.delta);
        }
        if (snap.history_count != 2) {
            fail("store/history", "%d points, expected 2", snap.history_count);
        } else if (snap.history[0].ms >= snap.history[1].ms) {
            fail("store/history", "history is not ascending");
        }
    }

    /* A reading after a long gap must not read as a five-minute delta. */
    entry.date_ms = now;
    entry.sgv = 200.0f;
    gc_store_add_entry(store, 0, &entry);
    gc_store_clear_user(store, 1);
    gc_entry_t old = {.date_ms = now - 90 * 60000, .sgv = 100.0f};
    gc_store_add_entry(store, 1, &old);
    gc_entry_t fresh = {.date_ms = now, .sgv = 180.0f};
    gc_store_add_entry(store, 1, &fresh);
    checks++;
    if (gc_store_snapshot(store, 1, now, &snap) && snap.has_delta) {
        fail("store/gap", "reported a delta across a 90-minute gap");
    }

    /* An entry that arrives out of order still lands in date order. */
    gc_store_clear_user(store, 2);
    gc_entry_t a = {.date_ms = now - 5 * 60000, .sgv = 150.0f};
    gc_entry_t b = {.date_ms = now - 15 * 60000, .sgv = 140.0f};
    gc_entry_t c = {.date_ms = now - 10 * 60000, .sgv = 145.0f};
    gc_store_add_entry(store, 2, &a);
    gc_store_add_entry(store, 2, &b);
    gc_store_add_entry(store, 2, &c);
    checks++;
    if (gc_store_snapshot(store, 2, now, &snap)) {
        if (snap.history_count != 3) {
            fail("store/order", "%d points, expected 3", snap.history_count);
        } else if (snap.history[0].ms > snap.history[1].ms
                   || snap.history[1].ms > snap.history[2].ms) {
            fail("store/order", "out-of-order arrival was not re-sorted");
        } else if (!close_enough(snap.sgv, 150.0f)) {
            fail("store/order", "newest is %.1f, expected 150", snap.sgv);
        }
    }

    /* The same timestamp replaces rather than duplicating. */
    gc_store_add_entry(store, 2, &a);
    checks++;
    if (gc_store_snapshot(store, 2, now, &snap) && snap.history_count != 3) {
        fail("store/replace", "a repeated timestamp added a fourth point");
    }

    /* Treatments: upsert by id, newest carbs and bolus, 8-hour bolus window. */
    gc_store_clear_user(store, 3);
    gc_treatment_t meal = {.created_at_ms = now - 30 * 60000,
                           .carbs = 40.0f, .has_carbs = true};
    snprintf(meal.id, sizeof(meal.id), "meal-1");
    gc_store_add_treatment(store, 3, &meal);
    meal.carbs = 55.0f;
    gc_store_add_treatment(store, 3, &meal);   /* same id: an edit, not a second meal */

    gc_treatment_t shot = {.created_at_ms = now - 20 * 60000,
                           .insulin = 3.5f, .has_insulin = true};
    snprintf(shot.id, sizeof(shot.id), "bolus-1");
    gc_store_add_treatment(store, 3, &shot);
    gc_treatment_t ancient = {.created_at_ms = now - 10 * 3600 * 1000,
                              .insulin = 9.0f, .has_insulin = true};
    snprintf(ancient.id, sizeof(ancient.id), "bolus-old");
    gc_store_add_treatment(store, 3, &ancient);

    checks++;
    if (gc_store_snapshot(store, 3, now, &snap)) {
        if (!snap.has_last_carbs || !close_enough(snap.last_carbs, 55.0f)) {
            fail("store/carbs", "last carbs is %.1f, expected the edited 55",
                 snap.last_carbs);
        }
        if (!snap.has_last_bolus || !close_enough(snap.last_bolus, 3.5f)) {
            fail("store/bolus", "last bolus is %.2f, expected 3.5",
                 snap.last_bolus);
        }
        if (snap.bolus_count != 1) {
            fail("store/bolus-window",
                 "%d boluses in the 8-hour window, expected 1", snap.bolus_count);
        }
    }

    /* Params merge rather than clobber. */
    gc_params_t first = {.isf = 42.0f, .has_isf = true};
    gc_params_t second = {.cr = 9.0f, .has_cr = true};
    gc_store_set_params(store, 3, &first);
    gc_store_set_params(store, 3, &second);
    checks++;
    if (gc_store_snapshot(store, 3, now, &snap)) {
        if (!snap.params.has_isf || !close_enough(snap.params.isf, 42.0f)
            || !snap.params.has_cr || !close_enough(snap.params.cr, 9.0f)) {
            fail("store/params", "a later partial update clobbered an earlier one");
        }
    }

    gc_store_destroy(store);
}

/* ---------------------------------------------------------- the header -- */

static void run_contract_checks(void)
{
    printf("contract\n");
    checks++;
    if (gc_color888(GC_THEME_DARK, GC_C_BG) != 0x0A0C0F) {
        fail("contract/palette", "the dark background is not 0x0A0C0F");
    }
    checks++;
    if (gc_rgb_to_565(0xFFFFFF) != 0xFFFF || gc_rgb_to_565(0x000000) != 0x0000) {
        fail("contract/565", "the RGB565 packing is wrong at the extremes");
    }
    checks++;
    if (gc_direction_lookup("DoubleDown")->heads != 2
        || gc_direction_lookup("FortyFiveUp")->angle_deg != -45) {
        fail("contract/arrows", "a trend arrow does not match the Python");
    }
    checks++;
    if (strcmp(gc_source_label("tidepool"), "TWIIST") != 0
        || strcmp(gc_source_label("something-else"), "TRIO") != 0) {
        fail("contract/badges", "a source badge does not match the Python");
    }
}

int main(void)
{
    run_contract_checks();
    run_oref_vectors();
    run_predict_vectors();
    run_store_checks();

    printf("\n%d checks, %d failures\n", checks, failures);
    if (failures == 0) {
        printf("the firmware forecast matches the Python\n");
    }
    return failures == 0 ? 0 : 1;
}
