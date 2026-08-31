/*
 * See gc_http.h.
 */

#include "gc_http.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

#include "gc_contract.h"

static const char *TAG = "gc_http";

/* Hosted Nightscout sites sit behind Cloudflare often enough that a default
 * client string is rejected outright, so both products send a browser-like
 * one — see the note at the top of nspull.py. */
#define USER_AGENT "Mozilla/5.0 (X11; Linux aarch64) GlucoCube/1.0"

int64_t gc_now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

void gc_iso8601_hours_ago(char *out, size_t length, int hours)
{
    const time_t when = (time_t)(gc_now_ms() / 1000) - (time_t)hours * 3600;
    struct tm tm_when;
    gmtime_r(&when, &tm_when);
    strftime(out, length, "%Y-%m-%dT%H:%M:%S.000Z", &tm_when);
}

/* --------------------------------------------------------------- http -- */

typedef struct {
    char *body;
    size_t length;
    bool truncated;
    char session_token[256];
} collector_t;

static esp_err_t on_event(esp_http_client_event_t *event)
{
    collector_t *collector = event->user_data;
    if (collector == NULL) {
        return ESP_OK;
    }
    switch (event->event_id) {
    case HTTP_EVENT_ON_HEADER:
        if (event->header_key != NULL
            && strcasecmp(event->header_key, GC_GLUCOCORE_SESSION_HEADER) == 0
            && event->header_value != NULL) {
            snprintf(collector->session_token, sizeof(collector->session_token),
                     "%s", event->header_value);
        }
        break;
    case HTTP_EVENT_ON_DATA: {
        if (collector->body == NULL || collector->truncated) {
            break;
        }
        const size_t room = GC_HTTP_MAX_RESPONSE - 1 - collector->length;
        size_t take = (size_t)event->data_len;
        if (take > room) {
            take = room;
            collector->truncated = true;
        }
        memcpy(collector->body + collector->length, event->data, take);
        collector->length += take;
        collector->body[collector->length] = '\0';
        break;
    }
    default:
        break;
    }
    return ESP_OK;
}

esp_err_t gc_http_perform(const gc_http_request_t *request,
                          gc_http_response_t *out)
{
    if (request == NULL || out == NULL || request->url == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof(*out));

    collector_t collector = {0};
    collector.body = heap_caps_malloc(GC_HTTP_MAX_RESPONSE,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (collector.body == NULL) {
        ESP_LOGE(TAG, "no room to read a reply");
        return ESP_ERR_NO_MEM;
    }
    collector.body[0] = '\0';

    esp_http_client_config_t config = {
        .url = request->url,
        .method = request->method,
        .event_handler = on_event,
        .user_data = &collector,
        .timeout_ms = request->timeout_ms > 0 ? request->timeout_ms : 20000,
        /* Mozilla's roots, because the sites a person configures are not
         * ours to pin a certificate for. */
        .crt_bundle_attach = esp_crt_bundle_attach,
        .keep_alive_enable = false,
        .disable_auto_redirect = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        heap_caps_free(collector.body);
        return ESP_FAIL;
    }

    esp_http_client_set_header(client, "User-Agent", USER_AGENT);
    esp_http_client_set_header(client, "Accept", "application/json");
    for (int i = 0; i < request->header_count && i < GC_HTTP_MAX_HEADERS; i++) {
        if (request->headers[i].name != NULL
            && request->headers[i].value != NULL) {
            esp_http_client_set_header(client, request->headers[i].name,
                                       request->headers[i].value);
        }
    }
    if (request->body != NULL) {
        esp_http_client_set_header(client, "Content-Type", "application/json");
        esp_http_client_set_post_field(client, request->body,
                                       (int)strlen(request->body));
    }

    const esp_err_t err = esp_http_client_perform(client);
    out->status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s: %s", request->url, esp_err_to_name(err));
        heap_caps_free(collector.body);
        return err;
    }
    if (collector.truncated) {
        ESP_LOGW(TAG, "%s answered more than %d bytes; the rest was dropped",
                 request->url, GC_HTTP_MAX_RESPONSE);
    }
    out->body = collector.body;
    out->length = collector.length;
    memcpy(out->session_token, collector.session_token,
           sizeof(out->session_token));
    return ESP_OK;
}

void gc_http_free(gc_http_response_t *response)
{
    if (response != NULL && response->body != NULL) {
        heap_caps_free(response->body);
        response->body = NULL;
        response->length = 0;
    }
}

cJSON *gc_http_get_json(const gc_http_request_t *request, int *out_status)
{
    gc_http_response_t response;
    const esp_err_t err = gc_http_perform(request, &response);
    if (out_status != NULL) {
        *out_status = response.status;
    }
    if (err != ESP_OK) {
        gc_http_free(&response);
        return NULL;
    }
    if (response.status < 200 || response.status >= 300) {
        ESP_LOGW(TAG, "%s answered %d", request->url, response.status);
        gc_http_free(&response);
        return NULL;
    }
    cJSON *json = cJSON_Parse(response.body);
    gc_http_free(&response);
    if (json == NULL) {
        ESP_LOGW(TAG, "%s did not answer with JSON", request->url);
    }
    return json;
}

/* -------------------------------------------------------------- dates -- */

/* Days from the civil epoch, by Howard Hinnant's algorithm. timegm() is not
 * portable and mktime() applies the local zone, which would shift every
 * timestamp by whatever the display's clock is set to. */
static int64_t days_from_civil(int year, unsigned month, unsigned day)
{
    year -= month <= 2;
    const int era = (year >= 0 ? year : year - 399) / 400;
    const unsigned yoe = (unsigned)(year - era * 400);
    const unsigned doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (int64_t)era * 146097 + (int64_t)doe - 719468;
}

bool gc_parse_iso8601(const char *text, int64_t *out_ms)
{
    if (text == NULL || out_ms == NULL) {
        return false;
    }
    int year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    int consumed = 0;
    if (sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2d%n", &year, &month, &day,
               &hour, &minute, &second, &consumed) != 6) {
        /* Some uploaders send a date with no time at all. */
        if (sscanf(text, "%4d-%2d-%2d%n", &year, &month, &day, &consumed) != 3) {
            return false;
        }
    }
    if (month < 1 || month > 12 || day < 1 || day > 31) {
        return false;
    }

    const char *cursor = text + consumed;
    int millis = 0;
    if (*cursor == '.' || *cursor == ',') {
        cursor++;
        int digits = 0;
        while (*cursor >= '0' && *cursor <= '9') {
            if (digits < 3) {
                millis = millis * 10 + (*cursor - '0');
                digits++;
            }
            cursor++;
        }
        while (digits++ < 3) {
            millis *= 10;
        }
    }

    /* An offset moves the instant; Z and a missing zone are both UTC, which
     * is what every one of these APIs sends. */
    int offset_minutes = 0;
    if (*cursor == '+' || *cursor == '-') {
        const int sign = (*cursor == '-') ? -1 : 1;
        int oh = 0, om = 0;
        if (sscanf(cursor + 1, "%2d:%2d", &oh, &om) == 2
            || sscanf(cursor + 1, "%2d%2d", &oh, &om) == 2) {
            offset_minutes = sign * (oh * 60 + om);
        }
    }

    const int64_t days = days_from_civil(year, (unsigned)month, (unsigned)day);
    int64_t seconds = days * 86400 + hour * 3600 + minute * 60 + second;
    seconds -= (int64_t)offset_minutes * 60;
    *out_ms = seconds * 1000 + millis;
    return true;
}

int64_t gc_parse_time_ms(const cJSON *doc, int64_t fallback_ms,
                         const char *const *keys, int key_count)
{
    for (int i = 0; i < key_count; i++) {
        const cJSON *value = cJSON_GetObjectItemCaseSensitive(doc, keys[i]);
        if (value == NULL || cJSON_IsNull(value)) {
            continue;
        }
        if (cJSON_IsNumber(value)) {
            /* Before about the year 2033 an epoch in seconds is under 1e11
             * and the same instant in milliseconds is over it, which is what
             * tells the two apart. */
            const double number = value->valuedouble;
            return (int64_t)(number > 1e11 ? number : number * 1000.0);
        }
        if (cJSON_IsString(value)) {
            int64_t parsed = 0;
            if (gc_parse_iso8601(value->valuestring, &parsed)) {
                return parsed;
            }
        }
    }
    return fallback_ms;
}

/* --------------------------------------------------------------- json -- */

double gc_json_number(const cJSON *object, const char *key, double fallback,
                      bool *found)
{
    if (found != NULL) {
        *found = false;
    }
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(value)) {
        return fallback;
    }
    if (found != NULL) {
        *found = true;
    }
    return value->valuedouble;
}

const char *gc_json_string(const cJSON *object, const char *key,
                           const char *fallback)
{
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsString(value) ? value->valuestring : fallback;
}
