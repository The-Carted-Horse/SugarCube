/*
 * The device's own web app: the dashboard, the setup wizard and settings.
 *
 * The same paths the Pi serves, so a phone that has one bookmarked finds
 * the same page on the other:
 *
 *     /                     the dashboard
 *     /setup                guided setup, one question per screen
 *     /settings             the hub, and a page per thing
 *     /api/dashboard.json   what the dashboard draws from
 *     /api/health.json      is it back up yet, for the post-save wait
 *     /screen.png           what the panel is showing right now
 *
 * While the setup hotspot is up it also answers the connectivity probes
 * every phone makes on joining a network (GC_CAPTIVE_PROBE_* in the
 * contract), which is what makes the setup page open by itself instead of
 * waiting to be asked for.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#include "gc_config.h"
#include "gc_store.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Port 80. There is nothing else on this device to want it. */
#define GC_HTTPD_PORT 80

/* Called when a save has been accepted and applied, so the caller can
 * restart the pollers and redraw. */
typedef void (*gc_config_changed_cb)(const gc_config_t *config);

esp_err_t gc_httpd_start(gc_config_t *config, gc_store_t *store,
                         gc_config_changed_cb on_change);
void gc_httpd_stop(void);

/* Answer the captive-portal probes with a redirect to the setup page.
 * Inert unless the hotspot is actually up, matching captive.py. */
void gc_httpd_set_captive(bool captive);

/* A URL that opens the settings page already signed in — what the QR code
 * on the panel encodes. Returns the length written. */
int gc_httpd_signed_url(char *out, size_t capacity, const char *path);

#ifdef __cplusplus
}
#endif
