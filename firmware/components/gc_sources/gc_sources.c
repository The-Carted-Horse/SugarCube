/*
 * Where a person's readings come from.
 *
 * One task per configured person, each speaking its own service and writing
 * into the shared store — glucocube/sources.py's shape, with its backoff
 * rule: an authentication failure backs off hard, because a display that
 * retries a rejected password every thirty seconds locks the account out,
 * and then nobody's readings arrive.
 *
 * The three protocols are ports of nspull.py, tidepool.py and
 * glucocore.py/glucocore_poll.py. The one deliberate departure from the Pi
 * is that a devicestatus document's prediction curve is pulled out here,
 * when the document arrives, rather than kept as raw JSON and re-read every
 * frame — see predict.py's device_series, ported below as device_series().
 */

#include "gc_sources.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"

#include "gc_http.h"
#include "gc_net.h"
#include "gc_synclog.h"

static const char *TAG = "gc_sources";

/* What this display calls itself in GlucoCore's device list before somebody
 * renames it. The board profile's own marketing name lives in the board
 * header, which is the hardware layer and none of this file's business. */
#define DEVICE_NAME "GlucoCube " GC_BOARD_ID

#define TIDEPOOL_API_BASE "https://api.tidepool.org"
#define TIDEPOOL_FETCH_WINDOW_HOURS 6
#define GLUCOCORE_FETCH_WINDOW_HOURS 6

/* nspull.py's counts: six hours of five-minute readings, and enough
 * treatments to cover the eight-hour bolus window the forecast reads. */
#define NS_ENTRY_COUNT 72
#define NS_TREATMENT_COUNT 50
#define NS_DEVICESTATUS_COUNT 12
#define PROFILE_EVERY_N_POLLS 15

typedef struct {
    TaskHandle_t task;
    volatile bool stop;
    int user;
    gc_user_config_t config;
    gc_store_t *store;

    /* Tidepool hands out a session token at login and a user id with it;
     * both are held for the life of the poller and re-fetched when the
     * service stops accepting them. */
    char session_token[256];
    char user_id[GC_MAX_ID];

    /* Nightscout accepts either an API secret or an access token, and
     * nspull.py tries each style and remembers what worked. */
    int auth_style;
    int poll_count;
} poller_t;

static poller_t s_pollers[GC_MAX_USERS];
static int s_poller_count;
static char s_device_token[GC_MAX_TOKEN];

/* ------------------------------------------------------------ helpers -- */

static void sha1_hex(const char *text, char out[41])
{
    unsigned char digest[20];
    mbedtls_sha1((const unsigned char *)text, strlen(text), digest);
    for (int i = 0; i < 20; i++) {
        snprintf(out + i * 2, 3, "%02x", digest[i]);
    }
    out[40] = '\0';
}

static void basic_auth(const char *user, const char *password, char *out,
                       size_t length)
{
    char pair[GC_MAX_EMAIL + GC_MAX_SECRET + 2];
    snprintf(pair, sizeof(pair), "%s:%s", user, password);
    unsigned char encoded[((sizeof(pair) + 2) / 3) * 4 + 4];
    size_t written = 0;
    if (mbedtls_base64_encode(encoded, sizeof(encoded), &written,
                              (const unsigned char *)pair, strlen(pair)) != 0) {
        snprintf(out, length, "Basic ");
        return;
    }
    encoded[written] = '\0';
    snprintf(out, length, "Basic %s", (const char *)encoded);
}

/* Trailing slashes double up when a path is appended, and some Nightscout
 * hosts answer 404 rather than normalising. */
static void trim_url(const char *url, char *out, size_t length)
{
    snprintf(out, length, "%s", url);
    size_t end = strlen(out);
    while (end > 0 && out[end - 1] == '/') {
        out[--end] = '\0';
    }
}

/* ---------------------------------------------------- shared ingestion -- */

static const char *const ENTRY_DATE_KEYS[] = {"date", "dateString"};
static const char *const TREATMENT_DATE_KEYS[] = {"created_at", "timestamp",
                                                  "date"};
static const char *const STATUS_DATE_KEYS[] = {"created_at", "date"};

static void ingest_entries(poller_t *poller, const cJSON *docs, int *count)
{
    const int64_t now = gc_now_ms();
    const cJSON *doc = NULL;
    cJSON_ArrayForEach(doc, docs) {
        if (!cJSON_IsObject(doc)) {
            continue;
        }
        bool found = false;
        double sgv = gc_json_number(doc, "sgv", 0.0, &found);
        if (!found) {
            sgv = gc_json_number(doc, "glucose", 0.0, &found);
        }
        if (!found) {
            continue;
        }
        gc_entry_t entry = {0};
        entry.date_ms = gc_parse_time_ms(doc, now, ENTRY_DATE_KEYS, 2);
        entry.sgv = (float)sgv;
        snprintf(entry.direction, sizeof(entry.direction), "%s",
                 gc_json_string(doc, "direction", ""));
        gc_store_add_entry(poller->store, poller->user, &entry);
        (*count)++;
    }
}

static void ingest_treatments(poller_t *poller, const cJSON *docs, int *count)
{
    const int64_t now = gc_now_ms();
    const cJSON *doc = NULL;
    cJSON_ArrayForEach(doc, docs) {
        if (!cJSON_IsObject(doc)) {
            continue;
        }
        gc_treatment_t treatment = {0};
        const char *id = gc_json_string(doc, "_id", NULL);
        if (id == NULL) {
            id = gc_json_string(doc, "id", NULL);
        }
        if (id != NULL) {
            snprintf(treatment.id, sizeof(treatment.id), "%s", id);
        } else {
            /* store.py mints one so a document without an id still
             * upserts rather than piling up on every poll. */
            snprintf(treatment.id, sizeof(treatment.id), "%08" PRIx32 "%08" PRIx32,
                     esp_random(), esp_random());
        }
        treatment.created_at_ms =
            gc_parse_time_ms(doc, now, TREATMENT_DATE_KEYS, 3);
        snprintf(treatment.event_type, sizeof(treatment.event_type), "%s",
                 gc_json_string(doc, "eventType", ""));
        treatment.carbs =
            (float)gc_json_number(doc, "carbs", 0.0, &treatment.has_carbs);
        treatment.insulin =
            (float)gc_json_number(doc, "insulin", 0.0, &treatment.has_insulin);
        gc_store_add_treatment(poller->store, poller->user, &treatment);
        (*count)++;
    }
}

/* store.py's extract_iob_cob: an openaps-style (Trio, oref) or a Loop
 * devicestatus, in that order, including the uppercase IOB/COB that
 * suggested and enacted use. */
static void extract_iob_cob(const cJSON *doc, bool *has_iob, float *iob,
                            bool *has_cob, float *cob)
{
    *has_iob = *has_cob = false;
    *iob = *cob = 0.0f;

    const cJSON *openaps = cJSON_GetObjectItemCaseSensitive(doc, "openaps");
    if (cJSON_IsObject(openaps)) {
        const cJSON *iob_doc = cJSON_GetObjectItemCaseSensitive(openaps, "iob");
        if (cJSON_IsObject(iob_doc)) {
            bool found = false;
            const double value = gc_json_number(iob_doc, "iob", 0.0, &found);
            if (found) {
                *iob = (float)value;
                *has_iob = true;
            }
        }
        const char *const sections[] = {"suggested", "enacted"};
        for (int i = 0; i < 2; i++) {
            const cJSON *section =
                cJSON_GetObjectItemCaseSensitive(openaps, sections[i]);
            if (!cJSON_IsObject(section)) {
                continue;
            }
            if (!*has_iob) {
                bool found = false;
                const double value = gc_json_number(section, "IOB", 0.0, &found);
                if (found) {
                    *iob = (float)value;
                    *has_iob = true;
                }
            }
            if (!*has_cob) {
                bool found = false;
                const double value = gc_json_number(section, "COB", 0.0, &found);
                if (found) {
                    *cob = (float)value;
                    *has_cob = true;
                }
            }
        }
    }

    const cJSON *loop = cJSON_GetObjectItemCaseSensitive(doc, "loop");
    if (cJSON_IsObject(loop)) {
        if (!*has_iob) {
            const cJSON *iob_doc = cJSON_GetObjectItemCaseSensitive(loop, "iob");
            if (cJSON_IsObject(iob_doc)) {
                bool found = false;
                const double value = gc_json_number(iob_doc, "iob", 0.0, &found);
                if (found) {
                    *iob = (float)value;
                    *has_iob = true;
                }
            }
        }
        if (!*has_cob) {
            const cJSON *cob_doc = cJSON_GetObjectItemCaseSensitive(loop, "cob");
            if (cJSON_IsObject(cob_doc)) {
                bool found = false;
                const double value = gc_json_number(cob_doc, "cob", 0.0, &found);
                if (found) {
                    *cob = (float)value;
                    *has_cob = true;
                }
            }
        }
    }
}

/* predict.py's device_series, run at ingestion rather than at draw time.
 * The Pi can afford to hold the whole document and re-read it every frame;
 * this cannot, so what survives is the curve itself. */
static bool device_series(const cJSON *doc, gc_device_pred_t *out)
{
    memset(out, 0, sizeof(*out));
    if (!cJSON_IsObject(doc)) {
        return false;
    }
    const int64_t now = gc_now_ms();

    const cJSON *loop = cJSON_GetObjectItemCaseSensitive(doc, "loop");
    const cJSON *predicted =
        cJSON_IsObject(loop) ? cJSON_GetObjectItemCaseSensitive(loop, "predicted")
                             : NULL;
    if (cJSON_IsObject(predicted)) {
        const cJSON *values =
            cJSON_GetObjectItemCaseSensitive(predicted, "values");
        if (cJSON_IsArray(values) && cJSON_GetArraySize(values) > 0) {
            static const char *const keys[] = {"startDate"};
            out->start_ms = gc_parse_time_ms(predicted, now, keys, 1);
            const cJSON *value = NULL;
            cJSON_ArrayForEach(value, values) {
                if (out->count >= GC_MAX_PRED) {
                    break;
                }
                if (cJSON_IsNumber(value)) {
                    out->values[out->count++] = (float)value->valuedouble;
                }
            }
            out->valid = out->count > 0;
            return out->valid;
        }
    }

    const cJSON *openaps = cJSON_GetObjectItemCaseSensitive(doc, "openaps");
    const cJSON *suggested =
        cJSON_IsObject(openaps)
            ? cJSON_GetObjectItemCaseSensitive(openaps, "suggested")
            : NULL;
    if (!cJSON_IsObject(suggested)) {
        return false;
    }
    const cJSON *pred_bgs =
        cJSON_GetObjectItemCaseSensitive(suggested, "predBGs");
    if (!cJSON_IsObject(pred_bgs)) {
        return false;
    }

    /* oref uploads several scenario curves. The pump's own headline outcome
     * is eventualBG, so the curve shown is the one that ends closest to it,
     * rather than a worst case pinned at the 39 mg/dL clamp. */
    static const char *const CANDIDATES[] = {"COB", "UAM", "IOB", "ZT"};
    bool have_eventual = false;
    const double eventual =
        gc_json_number(suggested, "eventualBG", 0.0, &have_eventual);

    const cJSON *chosen = NULL;
    double best_distance = 0.0;
    for (int i = 0; i < 4; i++) {
        const cJSON *curve =
            cJSON_GetObjectItemCaseSensitive(pred_bgs, CANDIDATES[i]);
        const int size = cJSON_IsArray(curve) ? cJSON_GetArraySize(curve) : 0;
        if (size == 0) {
            continue;
        }
        if (chosen == NULL) {
            chosen = curve;
            if (!have_eventual) {
                break;   /* without eventualBG the first one wins, as in Python */
            }
            const cJSON *last = cJSON_GetArrayItem(curve, size - 1);
            best_distance = fabs((cJSON_IsNumber(last) ? last->valuedouble : 0.0)
                                 - eventual);
            continue;
        }
        const cJSON *last = cJSON_GetArrayItem(curve, size - 1);
        const double distance =
            fabs((cJSON_IsNumber(last) ? last->valuedouble : 0.0) - eventual);
        if (distance < best_distance) {
            chosen = curve;
            best_distance = distance;
        }
    }
    if (chosen == NULL) {
        return false;
    }

    static const char *const keys[] = {"timestamp", "deliverAt"};
    out->start_ms = gc_parse_time_ms(suggested, now, keys, 2);
    const cJSON *value = NULL;
    cJSON_ArrayForEach(value, chosen) {
        if (out->count >= GC_MAX_PRED) {
            break;
        }
        if (cJSON_IsNumber(value)) {
            out->values[out->count++] = (float)value->valuedouble;
        }
    }
    out->valid = out->count > 0;
    return out->valid;
}

static void ingest_devicestatus(poller_t *poller, const cJSON *docs, int *count)
{
    const int64_t now = gc_now_ms();
    /* Only the newest status carrying an IOB or a COB matters — it is what
     * snapshot() reads — so the list is walked and the best kept. */
    const cJSON *doc = NULL;
    const cJSON *newest = NULL;
    int64_t newest_at = 0;
    cJSON_ArrayForEach(doc, docs) {
        if (!cJSON_IsObject(doc)) {
            continue;
        }
        bool has_iob = false, has_cob = false;
        float iob = 0.0f, cob = 0.0f;
        extract_iob_cob(doc, &has_iob, &iob, &has_cob, &cob);
        if (!has_iob && !has_cob) {
            continue;
        }
        const int64_t at = gc_parse_time_ms(doc, now, STATUS_DATE_KEYS, 2);
        if (newest == NULL || at > newest_at) {
            newest = doc;
            newest_at = at;
        }
        (*count)++;
    }
    if (newest == NULL) {
        return;
    }
    bool has_iob = false, has_cob = false;
    float iob = 0.0f, cob = 0.0f;
    extract_iob_cob(newest, &has_iob, &iob, &has_cob, &cob);
    gc_device_pred_t prediction;
    device_series(newest, &prediction);
    gc_store_set_device_status(poller->store, poller->user, newest_at,
                               has_iob, iob, has_cob, cob, &prediction);
}

/* ---------------------------------------------------------- Nightscout -- */

/* nspull.py tries each authentication style and remembers the one the site
 * accepts: a classic API secret goes in a header as its SHA-1, and an
 * access token goes in the query string. Which one a person pasted in is
 * not something they should have to know. */
enum {
    NS_AUTH_SHA1_HEADER = 0,
    NS_AUTH_TOKEN_QUERY,
    NS_AUTH_RAW_HEADER,
    NS_AUTH_STYLES,
};

static cJSON *nightscout_get(poller_t *poller, const char *path, int count,
                             int *out_status)
{
    char base[GC_MAX_URL];
    trim_url(poller->config.url, base, sizeof(base));

    for (int attempt = 0; attempt < NS_AUTH_STYLES; attempt++) {
        const int style = (poller->auth_style + attempt) % NS_AUTH_STYLES;
        /* Sized so the compiler can see it fits: the site's address, the
         * path, the count, and an access token in the query string. */
        char url[GC_MAX_URL + GC_MAX_SECRET + 64];
        char secret_hash[41];
        gc_http_request_t request = {
            .method = HTTP_METHOD_GET,
            .timeout_ms = 20000,
        };

        if (style == NS_AUTH_TOKEN_QUERY) {
            snprintf(url, sizeof(url), "%s%s?count=%d&token=%s", base, path,
                     count, poller->config.api_secret);
        } else {
            snprintf(url, sizeof(url), "%s%s?count=%d", base, path, count);
            if (style == NS_AUTH_SHA1_HEADER) {
                sha1_hex(poller->config.api_secret, secret_hash);
                request.headers[0].name = "api-secret";
                request.headers[0].value = secret_hash;
            } else {
                request.headers[0].name = "api-secret";
                request.headers[0].value = poller->config.api_secret;
            }
            request.header_count = 1;
        }
        request.url = url;

        int status = 0;
        cJSON *json = gc_http_get_json(&request, &status);
        if (out_status != NULL) {
            *out_status = status;
        }
        if (json != NULL) {
            poller->auth_style = style;
            return json;
        }
        if (status != 401 && status != 403) {
            /* Not an authentication problem, so trying another style of it
             * would only make the same request again. */
            return NULL;
        }
    }
    return NULL;
}

static esp_err_t poll_nightscout(poller_t *poller)
{
    int status = 0;
    int entries = 0, treatments = 0, statuses = 0;

    cJSON *json = nightscout_get(poller, "/api/v1/entries/sgv.json",
                                 NS_ENTRY_COUNT, &status);
    if (json == NULL) {
        return (status == 401 || status == 403) ? ESP_ERR_INVALID_STATE
                                                : ESP_FAIL;
    }
    ingest_entries(poller, json, &entries);
    cJSON_Delete(json);

    json = nightscout_get(poller, "/api/v1/treatments.json",
                          NS_TREATMENT_COUNT, NULL);
    if (json != NULL) {
        ingest_treatments(poller, json, &treatments);
        cJSON_Delete(json);
    }

    json = nightscout_get(poller, "/api/v1/devicestatus.json",
                          NS_DEVICESTATUS_COUNT, NULL);
    if (json != NULL) {
        ingest_devicestatus(poller, json, &statuses);
        cJSON_Delete(json);
    }

    /* Therapy settings change rarely, so the profile is not worth a request
     * every minute. */
    if (poller->poll_count % PROFILE_EVERY_N_POLLS == 0) {
        json = nightscout_get(poller, "/api/v1/profile.json", 1, NULL);
        if (json != NULL) {
            const cJSON *doc = cJSON_GetArrayItem(json, 0);
            const cJSON *store = cJSON_GetObjectItemCaseSensitive(doc, "store");
            const char *default_name = gc_json_string(doc, "defaultProfile", NULL);
            const cJSON *profile = NULL;
            if (cJSON_IsObject(store)) {
                if (default_name != NULL) {
                    profile = cJSON_GetObjectItemCaseSensitive(store, default_name);
                }
                if (profile == NULL) {
                    profile = store->child;
                }
            }
            if (cJSON_IsObject(profile)) {
                gc_params_t params = {0};
                const struct {
                    const char *key;
                    float *value;
                    bool *has;
                } wanted[] = {
                    {"sens", &params.isf, &params.has_isf},
                    {"carbratio", &params.cr, &params.has_cr},
                };
                for (size_t i = 0; i < 2; i++) {
                    const cJSON *series =
                        cJSON_GetObjectItemCaseSensitive(profile, wanted[i].key);
                    const cJSON *first =
                        cJSON_IsArray(series) ? cJSON_GetArrayItem(series, 0) : NULL;
                    bool found = false;
                    const double value = gc_json_number(first, "value", 0.0, &found);
                    if (found) {
                        *wanted[i].value = (float)value;
                        *wanted[i].has = true;
                    }
                }
                bool found = false;
                const double dia = gc_json_number(profile, "dia", 0.0, &found);
                if (found) {
                    params.dia_hours = (float)dia;
                    params.has_dia_hours = true;
                }
                gc_store_set_params(poller->store, poller->user, &params);
            }
            cJSON_Delete(json);
        }
    }

    ESP_LOGI(TAG, "[%s] pulled %d readings, %d treatments",
             poller->config.name, entries, treatments);
    gc_synclog_add("nightscout", poller->config.name, true,
                   "pulled %d readings, %d treatments", entries, treatments);
    return ESP_OK;
}

/* ------------------------------------------------------------ Tidepool -- */

static esp_err_t tidepool_login(const char *email, const char *password,
                                char *token, size_t token_length,
                                char *user_id, size_t user_id_length,
                                int *out_status)
{
    char authorization[256];
    basic_auth(email, password, authorization, sizeof(authorization));

    gc_http_request_t request = {
        .url = TIDEPOOL_API_BASE "/auth/login",
        .method = HTTP_METHOD_POST,
        .body = "",
        .headers = {{"Authorization", authorization}},
        .header_count = 1,
        .timeout_ms = 30000,
    };
    gc_http_response_t response;
    const esp_err_t err = gc_http_perform(&request, &response);
    if (out_status != NULL) {
        *out_status = response.status;
    }
    if (err != ESP_OK || response.status < 200 || response.status >= 300) {
        gc_http_free(&response);
        return err != ESP_OK ? err : ESP_ERR_INVALID_STATE;
    }
    cJSON *json = cJSON_Parse(response.body);
    snprintf(token, token_length, "%s", response.session_token);
    gc_http_free(&response);

    const char *id = gc_json_string(json, "userid", NULL);
    if (id != NULL) {
        snprintf(user_id, user_id_length, "%s", id);
    }
    cJSON_Delete(json);
    if (token[0] == '\0' || user_id[0] == '\0') {
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}

/* tidepool.py's direction_from_rate: Tidepool sends no trend arrow, so one
 * is derived from the slope between consecutive readings. */
static const char *direction_from_rate(double rate_per_5min)
{
    if (rate_per_5min > GC_TREND_RATE_DOUBLE) {
        return "DoubleUp";
    }
    if (rate_per_5min > GC_TREND_RATE_SINGLE) {
        return "SingleUp";
    }
    if (rate_per_5min > GC_TREND_RATE_FORTYFIVE) {
        return "FortyFiveUp";
    }
    if (rate_per_5min < -GC_TREND_RATE_DOUBLE) {
        return "DoubleDown";
    }
    if (rate_per_5min < -GC_TREND_RATE_SINGLE) {
        return "SingleDown";
    }
    if (rate_per_5min < -GC_TREND_RATE_FORTYFIVE) {
        return "FortyFiveDown";
    }
    return "Flat";
}

static double to_mgdl(double value, const char *units)
{
    if (units != NULL && strncasecmp(units, "mmol", 4) == 0) {
        return value * GC_TIDEPOOL_MGDL_PER_MMOL;
    }
    return value;
}

/* tidepool.py's transform(), which is where Tidepool's own document shapes
 * become the ones the store speaks. */
static void tidepool_transform(poller_t *poller, const cJSON *docs,
                               int *entries_out, int *treatments_out)
{
    const int64_t now = gc_now_ms();
    static const char *const TIME_KEYS[] = {"time"};

    /* The readings have to be in order before a slope between them means
     * anything, and Tidepool does not promise an order. */
    int cbg_count = 0;
    const cJSON *doc = NULL;
    cJSON_ArrayForEach(doc, docs) {
        if (cJSON_IsObject(doc)
            && strcmp(gc_json_string(doc, "type", ""), "cbg") == 0) {
            cbg_count++;
        }
    }
    typedef struct {
        int64_t ms;
        double mgdl;
        const char *iso;
    } reading_t;
    reading_t *readings =
        cbg_count > 0 ? calloc((size_t)cbg_count, sizeof(reading_t)) : NULL;
    int count = 0;
    if (readings != NULL) {
        cJSON_ArrayForEach(doc, docs) {
            if (!cJSON_IsObject(doc)
                || strcmp(gc_json_string(doc, "type", ""), "cbg") != 0) {
                continue;
            }
            bool found = false;
            const double value = gc_json_number(doc, "value", 0.0, &found);
            if (!found) {
                continue;
            }
            readings[count].ms = gc_parse_time_ms(doc, now, TIME_KEYS, 1);
            readings[count].mgdl = to_mgdl(value, gc_json_string(doc, "units", NULL));
            readings[count].iso = gc_json_string(doc, "time", NULL);
            count++;
        }
        for (int i = 1; i < count; i++) {
            const reading_t key = readings[i];
            int j = i - 1;
            while (j >= 0 && readings[j].ms > key.ms) {
                readings[j + 1] = readings[j];
                j--;
            }
            readings[j + 1] = key;
        }
        for (int i = 0; i < count; i++) {
            gc_entry_t entry = {0};
            entry.date_ms = readings[i].ms;
            /* Rounded, as transform() rounds: the store holds whole mg/dL
             * because that is what a CGM reports. */
            entry.sgv = (float)(double)(long)(readings[i].mgdl + 0.5);
            if (i > 0) {
                const int64_t gap = readings[i].ms - readings[i - 1].ms;
                if (gap > 0 && gap <= GC_TREND_MAX_GAP_MS) {
                    const double rate = (readings[i].mgdl - readings[i - 1].mgdl)
                                        / ((double)gap / (5.0 * 60.0 * 1000.0));
                    snprintf(entry.direction, sizeof(entry.direction), "%s",
                             direction_from_rate(rate));
                }
            }
            gc_store_add_entry(poller->store, poller->user, &entry);
            (*entries_out)++;
        }
        free(readings);
    }

    const cJSON *newest_status = NULL;
    int64_t newest_at = 0;
    bool status_iob_found = false, status_cob_found = false;
    double status_iob = 0.0, status_cob = 0.0;

    cJSON_ArrayForEach(doc, docs) {
        if (!cJSON_IsObject(doc)) {
            continue;
        }
        const char *type = gc_json_string(doc, "type", "");
        const char *id = gc_json_string(doc, "id", NULL);
        if (id == NULL) {
            id = gc_json_string(doc, "guid", NULL);
        }

        if (strcmp(type, "bolus") == 0) {
            bool has_normal = false, has_extended = false;
            const double normal = gc_json_number(doc, "normal", 0.0, &has_normal);
            const double extended =
                gc_json_number(doc, "extended", 0.0, &has_extended);
            const double insulin = (has_normal ? normal : 0.0)
                                   + (has_extended ? extended : 0.0);
            if (insulin > 0.0) {
                gc_treatment_t treatment = {0};
                snprintf(treatment.id, sizeof(treatment.id), "%s",
                         id != NULL ? id : "");
                treatment.created_at_ms = gc_parse_time_ms(doc, now, TIME_KEYS, 1);
                snprintf(treatment.event_type, sizeof(treatment.event_type),
                         "Bolus");
                treatment.insulin = (float)insulin;
                treatment.has_insulin = true;
                gc_store_add_treatment(poller->store, poller->user, &treatment);
                (*treatments_out)++;
            }
        } else if (strcmp(type, "food") == 0) {
            const cJSON *nutrition =
                cJSON_GetObjectItemCaseSensitive(doc, "nutrition");
            const cJSON *carbohydrate =
                cJSON_IsObject(nutrition)
                    ? cJSON_GetObjectItemCaseSensitive(nutrition, "carbohydrate")
                    : NULL;
            bool found = false;
            const double carbs = gc_json_number(carbohydrate, "net", 0.0, &found);
            if (found && carbs != 0.0) {
                gc_treatment_t treatment = {0};
                snprintf(treatment.id, sizeof(treatment.id), "%s",
                         id != NULL ? id : "");
                treatment.created_at_ms = gc_parse_time_ms(doc, now, TIME_KEYS, 1);
                snprintf(treatment.event_type, sizeof(treatment.event_type),
                         "Carb Correction");
                treatment.carbs = (float)carbs;
                treatment.has_carbs = true;
                gc_store_add_treatment(poller->store, poller->user, &treatment);
                (*treatments_out)++;
            }
        } else if (strcmp(type, "dosingDecision") == 0) {
            const cJSON *on_board =
                cJSON_GetObjectItemCaseSensitive(doc, "insulinOnBoard");
            bool has_iob = false;
            const double iob = gc_json_number(on_board, "amount", 0.0, &has_iob);

            bool has_cob = false;
            double cob = 0.0;
            const cJSON *carbs_on_board =
                cJSON_GetObjectItemCaseSensitive(doc, "carbsOnBoard");
            if (cJSON_IsObject(carbs_on_board)) {
                cob = gc_json_number(carbs_on_board, "amount", 0.0, &has_cob);
            }
            if (!has_cob) {
                const cJSON *alternative =
                    cJSON_GetObjectItemCaseSensitive(doc, "carbohydratesOnBoard");
                cob = gc_json_number(alternative, "amount", 0.0, &has_cob);
            }
            if (!has_cob) {
                const cJSON *food = cJSON_GetObjectItemCaseSensitive(doc, "food");
                const cJSON *nutrition =
                    cJSON_IsObject(food)
                        ? cJSON_GetObjectItemCaseSensitive(food, "nutrition")
                        : NULL;
                const cJSON *carbohydrate =
                    cJSON_IsObject(nutrition)
                        ? cJSON_GetObjectItemCaseSensitive(nutrition, "carbohydrate")
                        : NULL;
                cob = gc_json_number(carbohydrate, "net", 0.0, &has_cob);
            }
            if (!has_iob && !has_cob) {
                continue;
            }
            const int64_t at = gc_parse_time_ms(doc, now, TIME_KEYS, 1);
            if (newest_status == NULL || at > newest_at) {
                newest_status = doc;
                newest_at = at;
                status_iob_found = has_iob;
                status_iob = iob;
                status_cob_found = has_cob;
                status_cob = cob;
            }
        }
    }

    if (newest_status != NULL) {
        /* Tidepool carries no prediction curve, so the forecast falls back
         * to our own model — which is what the "~" on screen says. */
        gc_device_pred_t none = {0};
        gc_store_set_device_status(poller->store, poller->user, newest_at,
                                   status_iob_found, (float)status_iob,
                                   status_cob_found, (float)status_cob, &none);
    }
}

static void tidepool_pump_settings(poller_t *poller)
{
    char url[256];
    snprintf(url, sizeof(url),
             TIDEPOOL_API_BASE "/data/%s?type=pumpSettings&latest=true",
             poller->user_id);
    gc_http_request_t request = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER, poller->session_token}},
        .header_count = 1,
    };
    cJSON *json = gc_http_get_json(&request, NULL);
    if (json == NULL) {
        return;
    }
    const cJSON *doc = NULL;
    const cJSON *newest = NULL;
    int64_t newest_at = 0;
    static const char *const TIME_KEYS[] = {"time"};
    cJSON_ArrayForEach(doc, json) {
        if (!cJSON_IsObject(doc)
            || strcmp(gc_json_string(doc, "type", ""), "pumpSettings") != 0) {
            continue;
        }
        const int64_t at = gc_parse_time_ms(doc, 0, TIME_KEYS, 1);
        if (newest == NULL || at > newest_at) {
            newest = doc;
            newest_at = at;
        }
    }
    if (newest != NULL) {
        gc_params_t params = {0};
        /* A schedule is either a list or a named set of lists; the first
         * entry of whichever is what params_from_pumpsettings takes. */
        const struct {
            const char *singular;
            const char *plural;
            float *value;
            bool *has;
            bool convert;
        } wanted[] = {
            {"insulinSensitivity", "insulinSensitivities", &params.isf,
             &params.has_isf, true},
            {"carbRatio", "carbRatios", &params.cr, &params.has_cr, false},
        };
        for (size_t i = 0; i < 2; i++) {
            const cJSON *list =
                cJSON_GetObjectItemCaseSensitive(newest, wanted[i].singular);
            if (!cJSON_IsArray(list)) {
                const cJSON *named =
                    cJSON_GetObjectItemCaseSensitive(newest, wanted[i].plural);
                list = cJSON_IsObject(named) ? named->child : NULL;
            }
            const cJSON *first =
                cJSON_IsArray(list) ? cJSON_GetArrayItem(list, 0) : NULL;
            bool found = false;
            double amount = gc_json_number(first, "amount", 0.0, &found);
            if (!found || amount == 0.0) {
                continue;
            }
            /* Tidepool normalises glucose to mmol/L, so a sensitivity under
             * about 20 is in those units and has to come back. */
            if (wanted[i].convert && amount < 20.0) {
                amount *= GC_TIDEPOOL_MGDL_PER_MMOL;
            }
            *wanted[i].value = (float)amount;
            *wanted[i].has = true;
        }
        gc_store_set_params(poller->store, poller->user, &params);
    }
    cJSON_Delete(json);
}

static esp_err_t poll_tidepool(poller_t *poller)
{
    if (poller->session_token[0] == '\0') {
        int status = 0;
        const esp_err_t err = tidepool_login(
            poller->config.email, poller->config.password,
            poller->session_token, sizeof(poller->session_token),
            poller->user_id, sizeof(poller->user_id), &status);
        if (err != ESP_OK) {
            return (status == 401 || status == 403) ? ESP_ERR_INVALID_STATE
                                                    : ESP_FAIL;
        }
    }

    char since[32];
    gc_iso8601_hours_ago(since, sizeof(since), TIDEPOOL_FETCH_WINDOW_HOURS);
    char url[320];
    snprintf(url, sizeof(url),
             TIDEPOOL_API_BASE
             "/data/%s?type=cbg,bolus,food,dosingDecision&startDate=%s",
             poller->user_id, since);

    gc_http_request_t request = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER, poller->session_token}},
        .header_count = 1,
        .timeout_ms = 30000,
    };
    int status = 0;
    cJSON *json = gc_http_get_json(&request, &status);
    if (json == NULL) {
        if (status == 401 || status == 403) {
            /* The session expired. Dropping it makes the next poll log in
             * again, which is cheaper than treating this as a failure. */
            poller->session_token[0] = '\0';
            return ESP_ERR_INVALID_STATE;
        }
        return ESP_FAIL;
    }

    int entries = 0, treatments = 0;
    tidepool_transform(poller, json, &entries, &treatments);
    cJSON_Delete(json);

    if (poller->poll_count % PROFILE_EVERY_N_POLLS == 0) {
        tidepool_pump_settings(poller);
    }
    ESP_LOGI(TAG, "[%s] pulled %d readings, %d treatments",
             poller->config.name, entries, treatments);
    gc_synclog_add("tidepool", poller->config.name, true,
                   "pulled %d readings, %d treatments", entries, treatments);
    return ESP_OK;
}

/* ----------------------------------------------------------- GlucoCore -- */

static esp_err_t poll_glucocore(poller_t *poller)
{
    if (s_device_token[0] == '\0') {
        return ESP_ERR_INVALID_STATE;
    }
    char since[32];
    gc_iso8601_hours_ago(since, sizeof(since), GLUCOCORE_FETCH_WINDOW_HOURS);
    char url[320];
    snprintf(url, sizeof(url),
             GC_GLUCOCORE_BASE
             "/data/%s?type=cbg,bolus,food,dosingDecision&startDate=%s",
             poller->config.patient_id, since);

    gc_http_request_t request = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER, s_device_token}},
        .header_count = 1,
        .timeout_ms = 30000,
    };
    int status = 0;
    cJSON *json = gc_http_get_json(&request, &status);
    if (json == NULL) {
        if (status == 401 || status == 403) {
            ESP_LOGW(TAG, "[%s] GlucoCore rejected the device token — "
                          "this display needs pairing again",
                     poller->config.name);
            gc_synclog_add("glucocore", poller->config.name, false,
                           "device token rejected — re-pair in GlucoCore");
            return ESP_ERR_INVALID_STATE;
        }
        return ESP_FAIL;
    }

    /* GlucoCore serves Tidepool's document shapes, so the same transform
     * reads both. */
    int entries = 0, treatments = 0;
    tidepool_transform(poller, json, &entries, &treatments);
    cJSON_Delete(json);
    ESP_LOGI(TAG, "[%s] pulled %d readings, %d treatments",
             poller->config.name, entries, treatments);
    gc_synclog_add("glucocore", poller->config.name, true,
                   "pulled %d readings, %d treatments", entries, treatments);
    return ESP_OK;
}

/* -------------------------------------------------------------- pollers -- */

static void poller_task(void *arg)
{
    poller_t *poller = arg;
    int poll_seconds = poller->config.poll_seconds;
    if (poll_seconds < GC_SOURCE_MIN_POLL_SECONDS) {
        poll_seconds = GC_SOURCE_MIN_POLL_SECONDS;
    }
    ESP_LOGI(TAG, "[%s] %s poller started (every %ds)", poller->config.name,
             gc_source_kind_name(poller->config.kind), poll_seconds);

    while (!poller->stop) {
        esp_err_t err = ESP_FAIL;
        switch (poller->config.kind) {
        case GC_SOURCE_NIGHTSCOUT: err = poll_nightscout(poller); break;
        case GC_SOURCE_TIDEPOOL:   err = poll_tidepool(poller);   break;
        case GC_SOURCE_GLUCOCORE:  err = poll_glucocore(poller);  break;
        default: break;
        }
        poller->poll_count++;

        int delay = poll_seconds;
        if (err != ESP_OK) {
            /* Back off hard on a rejected credential: retrying a wrong
             * password every thirty seconds locks the account, and then
             * nobody's readings arrive. */
            delay = (err == ESP_ERR_INVALID_STATE)
                        ? GC_SOURCE_ERROR_BACKOFF_SECONDS
                        : (poll_seconds * 3 > GC_SOURCE_ERROR_BACKOFF_SECONDS
                               ? GC_SOURCE_ERROR_BACKOFF_SECONDS
                               : poll_seconds * 3);
            ESP_LOGW(TAG, "[%s] poll failed (%s); trying again in %ds",
                     poller->config.name, esp_err_to_name(err), delay);
            gc_synclog_add(gc_source_kind_name(poller->config.kind),
                           poller->config.name, false,
                           "poll failed: %s (retry in %ds)",
                           esp_err_to_name(err), delay);
        }
        for (int slept = 0; slept < delay && !poller->stop; slept++) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    poller->task = NULL;
    vTaskDelete(NULL);
}

static bool has_credentials(const gc_user_config_t *user)
{
    switch (user->kind) {
    case GC_SOURCE_GLUCOCORE:
        return user->patient_id[0] != '\0' && s_device_token[0] != '\0';
    case GC_SOURCE_NIGHTSCOUT:
        return user->url[0] != '\0';
    case GC_SOURCE_TIDEPOOL:
        return user->email[0] != '\0' && user->password[0] != '\0';
    default:
        return false;
    }
}

esp_err_t gc_sources_start(const gc_config_t *config, gc_store_t *store)
{
    if (config == NULL || store == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    gc_sources_stop();
    snprintf(s_device_token, sizeof(s_device_token), "%s",
             config->glucocore.device_token);

    s_poller_count = 0;
    for (int i = 0; i < config->user_count && i < GC_MAX_USERS; i++) {
        const gc_user_config_t *user = &config->users[i];
        if (!has_credentials(user)) {
            if (user->kind != GC_SOURCE_NONE) {
                ESP_LOGW(TAG, "[%s] the %s source is missing credentials; "
                              "not polling",
                         user->name, gc_source_kind_name(user->kind));
            }
            continue;
        }
        poller_t *poller = &s_pollers[s_poller_count];
        memset(poller, 0, sizeof(*poller));
        poller->user = i;
        poller->config = *user;
        poller->store = store;

        char name[24];
        snprintf(name, sizeof(name), "gc_src%d", i);
        if (xTaskCreate(poller_task, name, 8192, poller, 4, &poller->task)
            != pdPASS) {
            ESP_LOGE(TAG, "[%s] could not start a poller", user->name);
            continue;
        }
        s_poller_count++;
    }
    return ESP_OK;
}

void gc_sources_stop(void)
{
    for (int i = 0; i < s_poller_count; i++) {
        s_pollers[i].stop = true;
    }
    /* Waited for rather than just asked: the config is about to be
     * replaced, and a poller still running would write a reading into a
     * store slot that now belongs to somebody else. */
    for (int i = 0; i < s_poller_count; i++) {
        for (int waited = 0; s_pollers[i].task != NULL && waited < 100; waited++) {
            vTaskDelay(pdMS_TO_TICKS(50));
        }
    }
    s_poller_count = 0;
}

void gc_sources_poll_now(void)
{
    for (int i = 0; i < s_poller_count; i++) {
        if (s_pollers[i].task != NULL) {
            xTaskAbortDelay(s_pollers[i].task);
        }
    }
}

/* --------------------------------------------------------------- verify -- */

static gc_verify_result_t verdict(bool ok, const char *detail)
{
    gc_verify_result_t result = {.ok = ok};
    snprintf(result.detail, sizeof(result.detail), "%s", detail);
    return result;
}

gc_verify_result_t gc_verify_nightscout(const char *url, const char *secret)
{
    if (url == NULL || url[0] == '\0') {
        return verdict(false, "Enter the address of the Nightscout site.");
    }
    poller_t probe = {0};
    snprintf(probe.config.url, sizeof(probe.config.url), "%s", url);
    snprintf(probe.config.api_secret, sizeof(probe.config.api_secret), "%s",
             secret != NULL ? secret : "");

    int status = 0;
    cJSON *json = nightscout_get(&probe, "/api/v1/entries/sgv.json", 1, &status);
    if (json == NULL) {
        if (status == 401 || status == 403) {
            return verdict(false, "The site did not accept that API secret.");
        }
        if (status == 404) {
            return verdict(false, "That address answered, but not like a "
                                  "Nightscout site.");
        }
        if (status == 0) {
            return verdict(false, "Could not reach that address.");
        }
        return verdict(false, "The site answered with an error.");
    }
    const int count = cJSON_GetArraySize(json);
    cJSON_Delete(json);
    if (count == 0) {
        return verdict(true, "Connected, but the site has no readings yet.");
    }
    return verdict(true, "Connected.");
}

gc_verify_result_t gc_verify_tidepool(const char *email, const char *password)
{
    if (email == NULL || email[0] == '\0' || password == NULL
        || password[0] == '\0') {
        return verdict(false, "Enter the Tidepool email and password.");
    }
    char token[256] = {0};
    char user_id[GC_MAX_ID] = {0};
    int status = 0;
    const esp_err_t err = tidepool_login(email, password, token, sizeof(token),
                                         user_id, sizeof(user_id), &status);
    if (err != ESP_OK) {
        if (status == 401 || status == 403) {
            return verdict(false, "Tidepool did not accept that login.");
        }
        return verdict(false, "Could not reach Tidepool.");
    }
    return verdict(true, "Connected.");
}

/* ------------------------------------------------------------- pairing -- */

esp_err_t gc_glucocore_request_pairing(gc_pair_request_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof(*out));

    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "hardwareId", gc_hardware_id());
    cJSON_AddStringToObject(body, "name", DEVICE_NAME);
    char *text = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (text == NULL) {
        return ESP_ERR_NO_MEM;
    }

    gc_http_request_t request = {
        .url = GC_GLUCOCORE_BASE "/v1/sugar_cubes/requests",
        .method = HTTP_METHOD_POST,
        .body = text,
        .timeout_ms = 30000,
    };
    cJSON *json = gc_http_get_json(&request, NULL);
    cJSON_free(text);
    if (json == NULL) {
        return ESP_FAIL;
    }
    /* GlucoCore wraps its answers in a data envelope. */
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(json, "data");
    const cJSON *payload = cJSON_IsObject(data) ? data : json;

    snprintf(out->request_id, sizeof(out->request_id), "%s",
             gc_json_string(payload, "requestId", ""));
    snprintf(out->secret, sizeof(out->secret), "%s",
             gc_json_string(payload, "secret", ""));
    const char *url = gc_json_string(payload, "url", NULL);
    if (url != NULL) {
        snprintf(out->url, sizeof(out->url), "%s", url);
    } else {
        /* The code on the wall carries the request id and nothing else; the
         * secret that collects the token never leaves this device. */
        snprintf(out->url, sizeof(out->url), "%s/pair/%s", GC_GLUCOCORE_BASE,
                 out->request_id);
    }
    cJSON_Delete(json);
    return out->request_id[0] != '\0' ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}

/* Copies a device token and whatever config came with it into `config`. */
static bool adopt_device(const cJSON *payload, gc_config_t *config)
{
    const char *token = gc_json_string(payload, "token", NULL);
    if (token == NULL) {
        token = gc_json_string(payload, "deviceToken", NULL);
    }
    if (token == NULL) {
        return false;
    }
    snprintf(config->glucocore.device_token,
             sizeof(config->glucocore.device_token), "%s", token);
    snprintf(config->glucocore.hardware_id,
             sizeof(config->glucocore.hardware_id), "%s", gc_hardware_id());

    const cJSON *device = cJSON_GetObjectItemCaseSensitive(payload, "sugarCube");
    if (!cJSON_IsObject(device)) {
        device = cJSON_GetObjectItemCaseSensitive(payload, "device");
    }
    if (cJSON_IsObject(device)) {
        snprintf(config->glucocore.device_id,
                 sizeof(config->glucocore.device_id), "%s",
                 gc_json_string(device, "id", ""));
    }
    return true;
}

/* sync.py's apply_remote_config: who the display shows, what they are
 * called, their ranges, and the units — all of it follows GlucoCore once a
 * display is paired. */
static void apply_remote_config(const cJSON *remote, gc_config_t *config)
{
    if (!cJSON_IsObject(remote)) {
        return;
    }
    const cJSON *display = cJSON_GetObjectItemCaseSensitive(remote, "display");
    if (cJSON_IsObject(display)) {
        const char *units = gc_json_string(display, "units", NULL);
        if (units != NULL) {
            config->display.mmol = strncasecmp(units, "mmol", 4) == 0;
        }
        const char *timezone = gc_json_string(display, "timezone", NULL);
        if (timezone != NULL && timezone[0] != '\0') {
            snprintf(config->display.timezone,
                     sizeof(config->display.timezone), "%s", timezone);
        }
        const struct {
            const char *key;
            float *value;
        } thresholds[] = {
            {"low", &config->display.low},
            {"high", &config->display.high},
            {"urgent_low", &config->display.urgent_low},
            {"urgent_high", &config->display.urgent_high},
            {"stale_minutes", &config->display.stale_minutes},
        };
        for (size_t i = 0; i < 5; i++) {
            bool found = false;
            const double value =
                gc_json_number(display, thresholds[i].key, 0.0, &found);
            if (found) {
                *thresholds[i].value = (float)value;
            }
        }
    }

    const cJSON *patients =
        cJSON_GetObjectItemCaseSensitive(remote, "patientIds");
    const cJSON *per_patient =
        cJSON_GetObjectItemCaseSensitive(remote, "perPatient");
    if (!cJSON_IsArray(patients) || cJSON_GetArraySize(patients) == 0) {
        return;
    }

    int index = 0;
    const cJSON *id = NULL;
    cJSON_ArrayForEach(id, patients) {
        if (index >= GC_MAX_USERS || !cJSON_IsString(id)) {
            continue;
        }
        gc_user_config_t *user = &config->users[index];
        memset(user, 0, sizeof(*user));
        user->kind = GC_SOURCE_GLUCOCORE;
        snprintf(user->patient_id, sizeof(user->patient_id), "%s",
                 id->valuestring);
        user->poll_seconds = 60;

        const cJSON *mine =
            cJSON_IsObject(per_patient)
                ? cJSON_GetObjectItemCaseSensitive(per_patient, id->valuestring)
                : NULL;

        /* A person's name on the display is GlucoCore's `label` — what to
         * call them on screen, when the account name is not the household
         * name. Without one there is only the patient id, which is not a
         * name anybody would choose to see on a wall.
         *
         * Read from perPatient, not from a patientNames map: the service
         * has never sent one, and reading for it named everybody by their
         * patient id. The Pi had the same bug (see sync.patient_label). */
        const char *label = cJSON_IsObject(mine)
                                ? gc_json_string(mine, "label", NULL)
                                : NULL;
        while (label != NULL && *label == ' ') {
            label++;
        }
        snprintf(user->name, sizeof(user->name), "%s",
                 (label != NULL && *label != '\0') ? label : id->valuestring);

        const cJSON *ranges =
            cJSON_IsObject(mine)
                ? cJSON_GetObjectItemCaseSensitive(mine, "thresholds")
                : NULL;
        if (cJSON_IsObject(ranges)) {
            const struct {
                const char *key;
                float *value;
                bool *has;
            } wanted[] = {
                {"low", &user->low, &user->has_low},
                {"high", &user->high, &user->has_high},
                {"urgent_low", &user->urgent_low, &user->has_urgent_low},
                {"urgent_high", &user->urgent_high, &user->has_urgent_high},
            };
            for (size_t i = 0; i < 4; i++) {
                bool found = false;
                const double value =
                    gc_json_number(ranges, wanted[i].key, 0.0, &found);
                if (found) {
                    *wanted[i].value = (float)value;
                    *wanted[i].has = true;
                }
            }
        }
        index++;
    }
    config->user_count = index;

    bool found = false;
    const double version = gc_json_number(remote, "version", 0.0, &found);
    if (found) {
        config->glucocore.config_version = (int32_t)version;
    }
}

esp_err_t gc_glucocore_collect_pairing(const gc_pair_request_t *request,
                                       gc_config_t *config)
{
    if (request == NULL || config == NULL || request->request_id[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "secret", request->secret);
    char *text = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (text == NULL) {
        return ESP_ERR_NO_MEM;
    }

    char url[GC_MAX_URL + 96];
    snprintf(url, sizeof(url), GC_GLUCOCORE_BASE
             "/v1/sugar_cubes/requests/%s/token", request->request_id);
    gc_http_request_t http = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .body = text,
        .timeout_ms = 30000,
    };
    cJSON *json = gc_http_get_json(&http, NULL);
    cJSON_free(text);
    if (json == NULL) {
        return ESP_FAIL;
    }
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(json, "data");
    const cJSON *payload = cJSON_IsObject(data) ? data : json;

    /* "approved: false" is the answer for a request nobody has approved,
     * one that does not exist, and a wrong secret alike — so this is a
     * "not yet", not a failure to report. */
    const cJSON *approved =
        cJSON_GetObjectItemCaseSensitive(payload, "approved");
    if (cJSON_IsBool(approved) && !cJSON_IsTrue(approved)) {
        cJSON_Delete(json);
        return ESP_ERR_NOT_FINISHED;
    }
    esp_err_t err = ESP_ERR_INVALID_RESPONSE;
    if (adopt_device(payload, config)) {
        apply_remote_config(
            cJSON_GetObjectItemCaseSensitive(payload, "config"), config);
        err = ESP_OK;
    }
    cJSON_Delete(json);
    return err;
}

esp_err_t gc_glucocore_claim(const char *code, gc_config_t *config)
{
    if (code == NULL || code[0] == '\0' || config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "code", code);
    cJSON_AddStringToObject(body, "hardwareId", gc_hardware_id());
    cJSON_AddStringToObject(body, "name", DEVICE_NAME);
    char *text = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (text == NULL) {
        return ESP_ERR_NO_MEM;
    }

    gc_http_request_t request = {
        .url = GC_GLUCOCORE_BASE "/v1/sugar_cubes/claim",
        .method = HTTP_METHOD_POST,
        .body = text,
        .timeout_ms = 60000,
    };
    int status = 0;
    cJSON *json = gc_http_get_json(&request, &status);
    cJSON_free(text);
    if (json == NULL) {
        return (status == 400 || status == 404) ? ESP_ERR_INVALID_ARG : ESP_FAIL;
    }
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(json, "data");
    const cJSON *payload = cJSON_IsObject(data) ? data : json;

    esp_err_t err = ESP_ERR_INVALID_RESPONSE;
    if (adopt_device(payload, config)) {
        apply_remote_config(
            cJSON_GetObjectItemCaseSensitive(payload, "config"), config);
        err = ESP_OK;
    }
    cJSON_Delete(json);
    return err;
}

esp_err_t gc_glucocore_sync_config(gc_config_t *config)
{
    if (config == NULL || config->glucocore.device_token[0] == '\0') {
        return ESP_ERR_INVALID_STATE;
    }
    gc_http_request_t request = {
        .url = GC_GLUCOCORE_BASE "/v1/sugar_cubes/me/config",
        .method = HTTP_METHOD_GET,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER,
                     config->glucocore.device_token}},
        .header_count = 1,
    };
    cJSON *json = gc_http_get_json(&request, NULL);
    if (json == NULL) {
        return ESP_FAIL;
    }
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(json, "data");
    apply_remote_config(cJSON_IsObject(data) ? data : json, config);
    cJSON_Delete(json);
    return ESP_OK;
}

esp_err_t gc_glucocore_heartbeat(const gc_config_t *config,
                                 const char *firmware_version,
                                 const char *ip_address)
{
    if (config == NULL || config->glucocore.device_token[0] == '\0') {
        return ESP_ERR_INVALID_STATE;
    }
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "firmwareVersion",
                            firmware_version != NULL ? firmware_version : "");
    cJSON_AddStringToObject(body, "ipAddress",
                            ip_address != NULL ? ip_address : "");
    cJSON_AddStringToObject(body, "board", GC_BOARD_ID);
    char *text = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (text == NULL) {
        return ESP_ERR_NO_MEM;
    }
    gc_http_request_t request = {
        .url = GC_GLUCOCORE_BASE "/v1/sugar_cubes/me/heartbeat",
        .method = HTTP_METHOD_POST,
        .body = text,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER,
                     config->glucocore.device_token}},
        .header_count = 1,
    };
    gc_http_response_t response;
    const esp_err_t err = gc_http_perform(&request, &response);
    gc_http_free(&response);
    cJSON_free(text);
    return err;
}

/* --------------------------------------------------- staying in step -- */
/*
 * A paired display follows GlucoCore: who it shows, what they are called,
 * their ranges and the units all live there once pairing has happened.
 *
 * The Pi holds a long poll open rather than asking on a timer, so a change
 * made on a phone reaches the wall in seconds instead of at the next
 * minute; this does the same. The heartbeat rides on the same loop, which
 * is what makes the devices screen able to say a display is online.
 */

#define GLUCOCORE_HEARTBEAT_SECONDS 60
#define GLUCOCORE_WAIT_SECONDS 55
#define GLUCOCORE_ERROR_BACKOFF_SECONDS 30

static struct {
    TaskHandle_t task;
    volatile bool stop;
    gc_config_t config;                  /* a copy; the caller owns the live one */
    gc_glucocore_changed_cb on_change;
    /* Long enough for "2.14.0-rc.12" many times over. gc_ota owns the real
     * limit; this does not depend on the updater just to size a string. */
    char version[32];
    volatile bool online;
} s_core;

esp_err_t gc_glucocore_wait_config(gc_config_t *config, int32_t since_version,
                                   int timeout_seconds, bool *changed)
{
    if (config == NULL || config->glucocore.device_token[0] == '\0') {
        return ESP_ERR_INVALID_STATE;
    }
    if (changed != NULL) {
        *changed = false;
    }
    char url[256];
    snprintf(url, sizeof(url),
             GC_GLUCOCORE_BASE
             "/v1/sugar_cubes/me/config/wait?since_version=%" PRId32
             "&timeout=%d",
             since_version, timeout_seconds);

    gc_http_request_t request = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .headers = {{GC_GLUCOCORE_SESSION_HEADER,
                     config->glucocore.device_token}},
        .header_count = 1,
        /* Longer than the poll it is holding open, or the client gives up
         * on a request the server is deliberately sitting on. */
        .timeout_ms = (timeout_seconds + 10) * 1000,
    };
    int status = 0;
    cJSON *json = gc_http_get_json(&request, &status);
    if (json == NULL) {
        return (status == 401 || status == 403) ? ESP_ERR_INVALID_STATE
                                                : ESP_FAIL;
    }
    const cJSON *data = cJSON_GetObjectItemCaseSensitive(json, "data");
    const cJSON *payload = cJSON_IsObject(data) ? data : json;

    if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(payload, "changed"))) {
        bool found = false;
        const double version = gc_json_number(payload, "version", 0.0, &found);
        const cJSON *remote =
            cJSON_GetObjectItemCaseSensitive(payload, "config");
        /* A version we have already taken is not a change, however the
         * request came to return — a reconnect can replay the last one. */
        if (found && (int32_t)version > since_version) {
            apply_remote_config(remote, config);
            config->glucocore.config_version = (int32_t)version;
            if (changed != NULL) {
                *changed = true;
            }
        }
    }
    cJSON_Delete(json);
    return ESP_OK;
}

bool gc_glucocore_online(void)
{
    return s_core.online;
}

static void glucocore_task(void *arg)
{
    (void)arg;

    /* What the display already has is the starting point: without this the
     * first long poll would be told about a change it has already applied,
     * and would restart every poller for nothing. */
    if (gc_glucocore_sync_config(&s_core.config) == ESP_OK) {
        s_core.online = true;
    }

    int64_t last_heartbeat = 0;
    while (!s_core.stop) {
        const int64_t now = gc_now_ms();
        if (now - last_heartbeat >= GLUCOCORE_HEARTBEAT_SECONDS * 1000) {
            const esp_err_t err = gc_glucocore_heartbeat(
                &s_core.config, s_core.version, gc_net_ip());
            s_core.online = (err == ESP_OK);
            if (err != ESP_OK) {
                gc_synclog_add("glucocore", "system", false,
                               "could not reach GlucoCore: %s",
                               esp_err_to_name(err));
            }
            last_heartbeat = now;
        }

        gc_config_t candidate = s_core.config;
        bool changed = false;
        const esp_err_t err = gc_glucocore_wait_config(
            &candidate, candidate.glucocore.config_version,
            GLUCOCORE_WAIT_SECONDS, &changed);

        if (err == ESP_ERR_INVALID_STATE) {
            gc_synclog_add("glucocore", "system", false,
                           "device token rejected — re-pair in GlucoCore");
            s_core.online = false;
            for (int i = 0; i < GC_SOURCE_ERROR_BACKOFF_SECONDS
                            && !s_core.stop; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            continue;
        }
        if (err != ESP_OK) {
            s_core.online = false;
            for (int i = 0; i < GLUCOCORE_ERROR_BACKOFF_SECONDS
                            && !s_core.stop; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            continue;
        }
        s_core.online = true;

        if (changed && !s_core.stop) {
            char reason[160];
            if (!gc_config_valid(&candidate, reason, sizeof(reason))) {
                /* Refused rather than applied: a config that would not
                 * validate is one this display cannot draw, and what it is
                 * showing now at least works. */
                gc_synclog_add("glucocore", "system", false,
                               "refused a config change: %s", reason);
                continue;
            }
            s_core.config = candidate;
            gc_config_save(&candidate);
            gc_synclog_add("glucocore", "system", true,
                           "took config version %" PRId32 " (%d %s)",
                           candidate.glucocore.config_version,
                           candidate.user_count,
                           candidate.user_count == 1 ? "person" : "people");
            if (s_core.on_change != NULL) {
                s_core.on_change(&candidate);
            }
        }
    }
    s_core.task = NULL;
    vTaskDelete(NULL);
}

esp_err_t gc_glucocore_start(const gc_config_t *config,
                             gc_glucocore_changed_cb on_change,
                             const char *firmware_version)
{
    gc_glucocore_stop();
    if (config == NULL || config->glucocore.device_token[0] == '\0') {
        /* Not an error: a display fed by Nightscout or Tidepool has no
         * GlucoCore to stay in step with. */
        return ESP_ERR_INVALID_STATE;
    }
    s_core.config = *config;
    s_core.on_change = on_change;
    s_core.stop = false;
    s_core.online = false;
    snprintf(s_core.version, sizeof(s_core.version), "%s",
             firmware_version != NULL ? firmware_version : "");

    if (xTaskCreate(glucocore_task, "gc_core", 8192, NULL, 4, &s_core.task)
        != pdPASS) {
        ESP_LOGE(TAG, "could not start the GlucoCore task");
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void gc_glucocore_stop(void)
{
    if (s_core.task == NULL) {
        return;
    }
    s_core.stop = true;
    for (int waited = 0; s_core.task != NULL && waited < 140; waited++) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}
