/*
 * The plumbing every data source shares: one HTTPS request, one JSON reply,
 * and the two bits of date handling the Python does in its standard library
 * and this has to do by hand.
 *
 * Private to gc_sources — it is not in include/ because nothing outside the
 * component should be making its own requests.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"
#include "esp_err.h"
#include "esp_http_client.h"

/* A reply larger than this is one we do not understand. Six hours of
 * five-minute readings with their treatments is well under a hundred
 * kilobytes; the cap is what stops a misconfigured site from eating the
 * heap the display draws from. */
#define GC_HTTP_MAX_RESPONSE (256 * 1024)

#define GC_HTTP_MAX_HEADERS 4

typedef struct {
    const char *name;
    const char *value;
} gc_http_header_t;

typedef struct {
    const char *url;
    esp_http_client_method_t method;
    const char *body;            /* JSON, or NULL */
    gc_http_header_t headers[GC_HTTP_MAX_HEADERS];
    int header_count;
    int timeout_ms;
} gc_http_request_t;

typedef struct {
    int status;
    char *body;                  /* PSRAM, owned by the caller */
    size_t length;
    /* Headers a caller asked to keep, for the one case that needs one:
     * Tidepool and GlucoCore hand back a session token in a header. */
    char session_token[256];
} gc_http_response_t;

/* Performs the request and fills `out`. The caller frees out->body with
 * gc_http_free, whatever the outcome. */
esp_err_t gc_http_perform(const gc_http_request_t *request,
                          gc_http_response_t *out);
void gc_http_free(gc_http_response_t *response);

/* The same request, parsed. Returns NULL on any failure, having logged
 * why; the status is still reported through `out_status` so a caller can
 * tell "rejected" from "unreachable" — which is the difference between
 * backing off hard and retrying soon. */
cJSON *gc_http_get_json(const gc_http_request_t *request, int *out_status);

/* ------------------------------------------------------------- dates -- */

/* store.py's parse_time_ms: the first usable key in the document, as
 * milliseconds. Accepts a numeric epoch in seconds or milliseconds — the
 * 1e11 boundary decides which — and ISO-8601 strings. Falls back to
 * `fallback_ms`, which is what the Python's "now" argument amounts to. */
int64_t gc_parse_time_ms(const cJSON *doc, int64_t fallback_ms,
                         const char *const *keys, int key_count);

/* One ISO-8601 timestamp, with or without fractional seconds, with Z or an
 * offset. Returns false for anything it cannot read. */
bool gc_parse_iso8601(const char *text, int64_t *out_ms);

/* Milliseconds since the epoch, from the system clock. */
int64_t gc_now_ms(void);

/* An ISO-8601 timestamp `hours` back from now, which is how every one of
 * these APIs is asked for a window. */
void gc_iso8601_hours_ago(char *out, size_t length, int hours);

/* --------------------------------------------------------------- json -- */

/* A number from a JSON object, or `fallback` when the key is absent or not
 * a number. `found` says which happened, because for IOB the difference
 * between "zero" and "not reported" changes the forecast. */
double gc_json_number(const cJSON *object, const char *key, double fallback,
                      bool *found);
const char *gc_json_string(const cJSON *object, const char *key,
                           const char *fallback);
