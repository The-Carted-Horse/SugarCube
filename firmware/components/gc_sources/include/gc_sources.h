/*
 * Where a person's readings come from.
 *
 * One task per configured person, each polling its own service and writing
 * into the shared store — the C shape of glucocube/sources.py, with the
 * same backoff rule: an auth failure backs off hard, because a display
 * that retries a rejected password every thirty seconds locks the account
 * out and then nobody's readings arrive.
 *
 * The device is a peer of the Pi here, not a satellite of one: it speaks
 * GlucoCore, Nightscout and Tidepool itself.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#include "gc_config.h"
#include "gc_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* sources.py's ERROR_BACKOFF_SECONDS. */
#define GC_SOURCE_ERROR_BACKOFF_SECONDS 300
#define GC_SOURCE_MIN_POLL_SECONDS 30

/* Starts one poller per person who has a source with credentials. People
 * without are logged and skipped, exactly as start_pollers does. */
esp_err_t gc_sources_start(const gc_config_t *config, gc_store_t *store);

/* Stops every poller and waits for its task to finish, so the config can
 * be replaced without a poller writing into a store slot that has just
 * been given to somebody else. */
void gc_sources_stop(void);

/* Ask every poller to fetch now rather than waiting out its interval —
 * what the settings page's "check now" does. */
void gc_sources_poll_now(void);

/* ---- one-shot calls, for the setup wizard and the settings page ---- */

typedef struct {
    bool ok;
    char detail[160];    /* what to show the person when it is not ok */
} gc_verify_result_t;

/* Tries the credentials and reports what happened, without storing
 * anything — verify.py's job. Nothing is written until the last step of
 * the wizard, so a wrong password is a message rather than a broken
 * device. */
gc_verify_result_t gc_verify_nightscout(const char *url, const char *secret);
gc_verify_result_t gc_verify_tidepool(const char *email, const char *password);

/* ---- GlucoCore pairing ---- */

typedef struct {
    char request_id[GC_MAX_ID];
    char secret[GC_MAX_SECRET];
    char url[GC_MAX_URL];    /* what the QR code on the screen encodes */
    int64_t expires_ms;
} gc_pair_request_t;

/* Asks GlucoCore to pair this display, with nothing to authenticate the
 * asking. What comes back is a request id — which is what goes on screen
 * as a QR code — and a secret, which does not leave the device. */
esp_err_t gc_glucocore_request_pairing(gc_pair_request_t *out);

/* Collects the token once somebody signed in to GlucoCore has approved
 * the request. Returns ESP_ERR_NOT_FINISHED while it is still pending. */
esp_err_t gc_glucocore_collect_pairing(const gc_pair_request_t *request,
                                       gc_config_t *config);

/* Redeems a six-digit code minted in GlucoCore's Devices screen. The
 * answer carries the device's config, so who to show and their ranges
 * arrive with the token. */
esp_err_t gc_glucocore_claim(const char *code, gc_config_t *config);

/* Pulls the current config from GlucoCore and merges it into `config`,
 * the way sync.py's apply_remote_config does: who the people are, what
 * they are called, their thresholds, and the display's units. Returns
 * ESP_OK and leaves config alone when nothing has changed. */
esp_err_t gc_glucocore_sync_config(gc_config_t *config);

/* Tells GlucoCore the display is alive and what it is showing. */
esp_err_t gc_glucocore_heartbeat(const gc_config_t *config,
                                 const char *firmware_version,
                                 const char *ip_address);

/* Blocks until GlucoCore has a config newer than `since_version`, or the
 * timeout passes. One held-open request rather than a poll, so a change
 * made on a phone reaches the wall in seconds. */
esp_err_t gc_glucocore_wait_config(gc_config_t *config, int32_t since_version,
                                   int timeout_seconds, bool *changed);

/* ---- housekeeping for a paired display ---- */

/* Called when a config change has been taken and saved. The caller owns
 * restarting whatever reads the config. */
typedef void (*gc_glucocore_changed_cb)(const gc_config_t *config);

/* Starts the task that keeps a paired display in step with GlucoCore: it
 * holds a long poll open for config changes, and says the display is alive
 * often enough for the devices screen to show it as online. Does nothing —
 * and says so — on a display that is not paired. */
esp_err_t gc_glucocore_start(const gc_config_t *config,
                             gc_glucocore_changed_cb on_change,
                             const char *firmware_version);
void gc_glucocore_stop(void);

/* True while the last exchange with GlucoCore succeeded, for the settings
 * page to show. */
bool gc_glucocore_online(void);

#ifdef __cplusplus
}
#endif
