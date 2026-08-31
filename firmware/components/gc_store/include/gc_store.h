/*
 * What the display needs to draw, and the ring buffers it comes out of.
 *
 * The Raspberry Pi keeps this in SQLite (glucocube/store.py). A wall clock
 * with 8 MB of PSRAM does not need a database: it needs the last few hours,
 * which is a fixed-size ring per person. The shapes below mirror
 * store.py's UserSnapshot field for field, so predict/oref/ui code reads
 * the same way in both languages.
 *
 * Everything is milliseconds since the epoch and mg/dL, exactly as on the
 * Pi. Conversion to mmol/L is a display concern and happens in gc_ui.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "gc_contract.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The dashboard splits into one panel per person; past four on an 800px
 * panel each column is too narrow to read across a room. */
#define GC_MAX_USERS 4

#define GC_MAX_NAME 48
#define GC_MAX_ID 40
#define GC_MAX_DIRECTION 20
#define GC_MAX_EVENT_TYPE 32

/* Ring sizes. A 5-minute CGM fills 288 slots in a day; a 1-minute uploader
 * fills them in under five hours, which still covers the 3-hour chart and
 * the 45-minute deviation window with room to spare. */
#define GC_MAX_ENTRIES 384
#define GC_MAX_TREATMENTS 128

/* The longest device prediction we keep: oref uploads 48 five-minute
 * points (4 hours); the display only reads the first two hours of it. */
#define GC_MAX_PRED 64

typedef struct {
    int64_t date_ms;
    float sgv;
    char direction[GC_MAX_DIRECTION];
} gc_entry_t;

typedef struct {
    char id[GC_MAX_ID];
    int64_t created_at_ms;
    char event_type[GC_MAX_EVENT_TYPE];
    float carbs;
    float insulin;
    bool has_carbs;
    bool has_insulin;
} gc_treatment_t;

/* Therapy settings, from a Nightscout profile or Tidepool pump settings.
 * Absent fields fall back to the contract's defaults in gc_oref. */
typedef struct {
    float isf;
    float cr;
    float dia_hours;
    float peak_min;
    bool has_isf;
    bool has_cr;
    bool has_dia_hours;
    bool has_peak_min;
} gc_params_t;

/* The AID system's own forecast, pulled out of a devicestatus document when
 * it arrives rather than kept as raw JSON — the Pi can afford to hold the
 * document and re-read it every frame; this cannot. */
typedef struct {
    bool valid;
    int64_t start_ms;
    int count;
    float values[GC_MAX_PRED];
} gc_device_pred_t;

typedef struct {
    int64_t ms;
    float value;
} gc_point_t;

/* Mirrors store.py's UserSnapshot. */
typedef struct {
    char name[GC_MAX_NAME];

    bool has_sgv;
    float sgv;
    int64_t sgv_date;
    char direction[GC_MAX_DIRECTION];

    bool has_delta;
    float delta;

    bool has_iob;
    float iob;
    bool has_cob;
    float cob;

    bool has_status;
    int64_t status_date;
    gc_device_pred_t device_pred;

    bool has_last_carbs;
    float last_carbs;
    int64_t last_carbs_date;
    bool has_last_bolus;
    float last_bolus;
    int64_t last_bolus_date;

    int history_count;
    gc_point_t history[GC_MAX_ENTRIES];   /* oldest first, as on the Pi */

    int bolus_count;
    gc_point_t boluses[GC_MAX_TREATMENTS]; /* (ms, units), newest last */

    gc_params_t params;
} gc_snapshot_t;

/* ------------------------------------------------------------- store ---- */

typedef struct gc_store gc_store_t;

/* Allocates in PSRAM; one store is shared by the pollers (writers, each on
 * its own task) and the draw loop (reader), so every call takes a mutex. */
gc_store_t *gc_store_create(void);
void gc_store_destroy(gc_store_t *store);

/* Users are addressed by index, matching the order in the config. */
int gc_store_add_entry(gc_store_t *store, int user, const gc_entry_t *entry);
int gc_store_add_treatment(gc_store_t *store, int user,
                           const gc_treatment_t *treatment);
void gc_store_set_device_status(gc_store_t *store, int user, int64_t created_at_ms,
                                bool has_iob, float iob, bool has_cob, float cob,
                                const gc_device_pred_t *prediction);
void gc_store_set_params(gc_store_t *store, int user, const gc_params_t *params);

/* Drop everything for one person — used when the config changes who a
 * panel belongs to, so the new person never shows the old one's readings. */
void gc_store_clear_user(gc_store_t *store, int user);

/* How far back a snapshot reaches, matching store.py's snapshot(). */
#define GC_SNAPSHOT_HISTORY_MINUTES GC_CHART_HISTORY_MINUTES
#define GC_SNAPSHOT_BOLUS_HOURS 8
#define GC_DELTA_MAX_GAP_MS (15 * 60 * 1000)

/* Fills out with the same fields store.py's snapshot() returns:
 *
 *   - sgv, direction and sgv_date from the newest entry;
 *   - delta from the two newest entries, but only when they are less than
 *     GC_DELTA_MAX_GAP_MS apart, so a reading after a sensor gap does not
 *     show a two-hour jump as a five-minute one;
 *   - history, ascending, over the last GC_SNAPSHOT_HISTORY_MINUTES;
 *   - iob/cob and the device prediction from the newest device status that
 *     carried either;
 *   - last_carbs and last_bolus from the newest treatment carrying each;
 *   - boluses, ascending, over the last GC_SNAPSHOT_BOLUS_HOURS.
 *
 * Returns false for an out-of-range user index. */
bool gc_store_snapshot(gc_store_t *store, int user, int64_t now_ms,
                       gc_snapshot_t *out);

#ifdef __cplusplus
}
#endif
