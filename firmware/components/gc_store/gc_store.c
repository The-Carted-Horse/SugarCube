/*
 * Ring buffers standing in for glucocube/store.py's SQLite tables.
 *
 * Compiles both for the device and for the host test: on the device the
 * rings live in PSRAM and a FreeRTOS mutex separates the poller tasks from
 * the draw loop, and on a host neither exists, so both are behind
 * ESP_PLATFORM. The snapshot logic — the part that decides what a person
 * sees — is identical either way, which is the point of building it here.
 */

#include "gc_store.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#endif

typedef struct {
    gc_entry_t entries[GC_MAX_ENTRIES];
    int entry_count;   /* how many slots are live, up to GC_MAX_ENTRIES */
    int entry_head;    /* index one past the newest */

    gc_treatment_t treatments[GC_MAX_TREATMENTS];
    int treatment_count;
    int treatment_head;

    bool has_status;
    int64_t status_date;
    bool has_iob;
    float iob;
    bool has_cob;
    float cob;
    gc_device_pred_t device_pred;

    gc_params_t params;
} user_slot_t;

struct gc_store {
    user_slot_t users[GC_MAX_USERS];
#ifdef ESP_PLATFORM
    SemaphoreHandle_t lock;
#endif
};

/* ------------------------------------------------------------- locking -- */

static void store_lock(gc_store_t *store)
{
#ifdef ESP_PLATFORM
    xSemaphoreTake(store->lock, portMAX_DELAY);
#else
    (void)store;
#endif
}

static void store_unlock(gc_store_t *store)
{
#ifdef ESP_PLATFORM
    xSemaphoreGive(store->lock);
#else
    (void)store;
#endif
}

/* ------------------------------------------------------------ lifetime -- */

gc_store_t *gc_store_create(void)
{
#ifdef ESP_PLATFORM
    gc_store_t *store = heap_caps_calloc(1, sizeof(gc_store_t),
                                         MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
#else
    gc_store_t *store = calloc(1, sizeof(gc_store_t));
#endif
    if (store == NULL) {
        return NULL;
    }
#ifdef ESP_PLATFORM
    store->lock = xSemaphoreCreateMutex();
    if (store->lock == NULL) {
        heap_caps_free(store);
        return NULL;
    }
#endif
    return store;
}

void gc_store_destroy(gc_store_t *store)
{
    if (store == NULL) {
        return;
    }
#ifdef ESP_PLATFORM
    vSemaphoreDelete(store->lock);
    heap_caps_free(store);
#else
    free(store);
#endif
}

static bool valid_user(int user)
{
    return user >= 0 && user < GC_MAX_USERS;
}

/* --------------------------------------------------------------- rings -- */

/* Entries are kept newest-last in a ring. Readings arrive in bursts and not
 * always in order, so an entry older than the newest is placed by walking
 * back — the same "insert and re-sort" a UNIQUE(user, date) index gives the
 * Pi for free. A reading with a date we already hold replaces it, matching
 * the ON CONFLICT REPLACE in store.py's schema. */
static int entry_index(const user_slot_t *slot, int offset)
{
    int index = slot->entry_head - 1 - offset;
    while (index < 0) {
        index += GC_MAX_ENTRIES;
    }
    return index % GC_MAX_ENTRIES;
}

int gc_store_add_entry(gc_store_t *store, int user, const gc_entry_t *entry)
{
    if (store == NULL || entry == NULL || !valid_user(user)) {
        return 0;
    }
    store_lock(store);
    user_slot_t *slot = &store->users[user];

    /* Replace a reading with the same timestamp rather than storing both. */
    for (int i = 0; i < slot->entry_count; i++) {
        int index = entry_index(slot, i);
        if (slot->entries[index].date_ms == entry->date_ms) {
            slot->entries[index] = *entry;
            store_unlock(store);
            return 1;
        }
        /* Entries are ordered, so once we are past the new one's time there
         * is nothing left to match. */
        if (slot->entries[index].date_ms < entry->date_ms) {
            break;
        }
    }

    slot->entries[slot->entry_head] = *entry;
    slot->entry_head = (slot->entry_head + 1) % GC_MAX_ENTRIES;
    if (slot->entry_count < GC_MAX_ENTRIES) {
        slot->entry_count++;
    }

    /* Bubble the new entry back into date order. Out-of-order arrivals are
     * rare and never far, so this is a swap or two, not a sort. */
    for (int i = 0; i + 1 < slot->entry_count; i++) {
        int newer = entry_index(slot, i);
        int older = entry_index(slot, i + 1);
        if (slot->entries[older].date_ms <= slot->entries[newer].date_ms) {
            break;
        }
        gc_entry_t tmp = slot->entries[newer];
        slot->entries[newer] = slot->entries[older];
        slot->entries[older] = tmp;
    }
    store_unlock(store);
    return 1;
}

static int treatment_index(const user_slot_t *slot, int offset)
{
    int index = slot->treatment_head - 1 - offset;
    while (index < 0) {
        index += GC_MAX_TREATMENTS;
    }
    return index % GC_MAX_TREATMENTS;
}

int gc_store_add_treatment(gc_store_t *store, int user,
                           const gc_treatment_t *treatment)
{
    if (store == NULL || treatment == NULL || !valid_user(user)) {
        return 0;
    }
    store_lock(store);
    user_slot_t *slot = &store->users[user];

    /* store.py upserts on the document id; a pump that re-sends a bolus
     * must not double it. */
    if (treatment->id[0] != '\0') {
        for (int i = 0; i < slot->treatment_count; i++) {
            int index = treatment_index(slot, i);
            if (strncmp(slot->treatments[index].id, treatment->id,
                        GC_MAX_ID) == 0) {
                slot->treatments[index] = *treatment;
                store_unlock(store);
                return 1;
            }
        }
    }

    slot->treatments[slot->treatment_head] = *treatment;
    slot->treatment_head = (slot->treatment_head + 1) % GC_MAX_TREATMENTS;
    if (slot->treatment_count < GC_MAX_TREATMENTS) {
        slot->treatment_count++;
    }

    for (int i = 0; i + 1 < slot->treatment_count; i++) {
        int newer = treatment_index(slot, i);
        int older = treatment_index(slot, i + 1);
        if (slot->treatments[older].created_at_ms
            <= slot->treatments[newer].created_at_ms) {
            break;
        }
        gc_treatment_t tmp = slot->treatments[newer];
        slot->treatments[newer] = slot->treatments[older];
        slot->treatments[older] = tmp;
    }
    store_unlock(store);
    return 1;
}

void gc_store_set_device_status(gc_store_t *store, int user, int64_t created_at_ms,
                                bool has_iob, float iob, bool has_cob, float cob,
                                const gc_device_pred_t *prediction)
{
    if (store == NULL || !valid_user(user)) {
        return;
    }
    /* store.py's snapshot only looks at statuses carrying an IOB or a COB,
     * so one carrying neither is not worth keeping. */
    if (!has_iob && !has_cob) {
        return;
    }
    store_lock(store);
    user_slot_t *slot = &store->users[user];
    if (slot->has_status && created_at_ms < slot->status_date) {
        store_unlock(store);   /* an older status never replaces a newer one */
        return;
    }
    slot->has_status = true;
    slot->status_date = created_at_ms;
    slot->has_iob = has_iob;
    slot->iob = iob;
    slot->has_cob = has_cob;
    slot->cob = cob;
    if (prediction != NULL) {
        slot->device_pred = *prediction;
    } else {
        memset(&slot->device_pred, 0, sizeof(slot->device_pred));
    }
    store_unlock(store);
}

void gc_store_set_params(gc_store_t *store, int user, const gc_params_t *params)
{
    if (store == NULL || params == NULL || !valid_user(user)) {
        return;
    }
    store_lock(store);
    gc_params_t *held = &store->users[user].params;
    /* Merge, like store.py's set_params: profile sources arrive piecemeal
     * and a blank must never clobber a value we already know. */
    if (params->has_isf) {
        held->isf = params->isf;
        held->has_isf = true;
    }
    if (params->has_cr) {
        held->cr = params->cr;
        held->has_cr = true;
    }
    if (params->has_dia_hours) {
        held->dia_hours = params->dia_hours;
        held->has_dia_hours = true;
    }
    if (params->has_peak_min) {
        held->peak_min = params->peak_min;
        held->has_peak_min = true;
    }
    store_unlock(store);
}

void gc_store_clear_user(gc_store_t *store, int user)
{
    if (store == NULL || !valid_user(user)) {
        return;
    }
    store_lock(store);
    memset(&store->users[user], 0, sizeof(user_slot_t));
    store_unlock(store);
}

/* ------------------------------------------------------------ snapshot -- */

bool gc_store_snapshot(gc_store_t *store, int user, int64_t now_ms,
                       gc_snapshot_t *out)
{
    if (store == NULL || out == NULL || !valid_user(user)) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    store_lock(store);
    const user_slot_t *slot = &store->users[user];

    if (slot->entry_count > 0) {
        const gc_entry_t *newest = &slot->entries[entry_index(slot, 0)];
        out->has_sgv = true;
        out->sgv = newest->sgv;
        out->sgv_date = newest->date_ms;
        snprintf(out->direction, sizeof(out->direction), "%s", newest->direction);

        if (slot->entry_count > 1) {
            const gc_entry_t *previous = &slot->entries[entry_index(slot, 1)];
            if (newest->date_ms - previous->date_ms < GC_DELTA_MAX_GAP_MS) {
                out->has_delta = true;
                out->delta = newest->sgv - previous->sgv;
            }
        }
    }

    int64_t history_from =
        now_ms - (int64_t)GC_SNAPSHOT_HISTORY_MINUTES * 60 * 1000;
    for (int i = slot->entry_count - 1; i >= 0; i--) {
        const gc_entry_t *entry = &slot->entries[entry_index(slot, i)];
        if (entry->date_ms < history_from) {
            continue;
        }
        out->history[out->history_count].ms = entry->date_ms;
        out->history[out->history_count].value = entry->sgv;
        out->history_count++;
    }

    out->has_status = slot->has_status;
    out->status_date = slot->status_date;
    out->has_iob = slot->has_iob;
    out->iob = slot->iob;
    out->has_cob = slot->has_cob;
    out->cob = slot->cob;
    out->device_pred = slot->device_pred;

    int64_t bolus_from = now_ms - (int64_t)GC_SNAPSHOT_BOLUS_HOURS * 3600 * 1000;
    for (int i = slot->treatment_count - 1; i >= 0; i--) {
        const gc_treatment_t *treatment =
            &slot->treatments[treatment_index(slot, i)];
        if (treatment->has_insulin && treatment->insulin > 0.0f
            && treatment->created_at_ms >= bolus_from) {
            out->boluses[out->bolus_count].ms = treatment->created_at_ms;
            out->boluses[out->bolus_count].value = treatment->insulin;
            out->bolus_count++;
        }
    }

    /* Newest treatment carrying each, whatever its age — "3D AGO" under
     * CARBS is information; a blank is not. */
    for (int i = 0; i < slot->treatment_count; i++) {
        const gc_treatment_t *treatment =
            &slot->treatments[treatment_index(slot, i)];
        if (!out->has_last_carbs && treatment->has_carbs && treatment->carbs > 0.0f) {
            out->has_last_carbs = true;
            out->last_carbs = treatment->carbs;
            out->last_carbs_date = treatment->created_at_ms;
        }
        if (!out->has_last_bolus && treatment->has_insulin
            && treatment->insulin > 0.0f) {
            out->has_last_bolus = true;
            out->last_bolus = treatment->insulin;
            out->last_bolus_date = treatment->created_at_ms;
        }
        if (out->has_last_carbs && out->has_last_bolus) {
            break;
        }
    }

    out->params = slot->params;
    store_unlock(store);
    return true;
}
