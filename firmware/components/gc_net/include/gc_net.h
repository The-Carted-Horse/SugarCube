/*
 * Getting on a network, and what to do when there is not one.
 *
 * The policy is glucocube/network.py's, constant for constant from the
 * contract: check every GC_NET_CHECK_SECONDS, and a device that has been
 * on a network before gets GC_NET_FAILS_NEEDED tries before the dashboard
 * is taken away for the setup hotspot, while a device that has never been
 * on one raises the hotspot after a single failure. There is nothing for
 * the second kind to wait for.
 *
 * The hotspot is GC_HOTSPOT_SSID at GC_HOTSPOT_ADDR — the same name at the
 * same address as the Pi's, so a phone that has joined one before finds
 * the other where it expects.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#include "gc_config.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GC_MAX_SCAN_RESULTS 24

typedef enum {
    GC_NET_DOWN,        /* no network, and no hotspot up yet */
    GC_NET_CONNECTING,
    GC_NET_ONLINE,
    GC_NET_HOTSPOT,     /* our own setup network is up */
} gc_net_state_t;

typedef struct {
    char ssid[GC_MAX_SSID];
    int rssi;
    bool secured;
} gc_scan_result_t;

esp_err_t gc_net_init(const gc_config_t *config);

gc_net_state_t gc_net_state(void);
bool gc_net_is_online(void);

/* Dotted-quad of whichever interface is up, or "" when neither is. */
const char *gc_net_ip(void);

/* What went wrong with the last join attempt, in words a person can act
 * on — "wrong password", "network not found" — rather than a numeric
 * reason code. network.py's friendly_error(), against the ESP-IDF
 * disconnect reasons rather than nmcli's strings. */
const char *gc_net_last_error(void);

/* Joins a network, saving it only once it works. Blocks until the join
 * succeeds or fails; the caller is the wizard, which has a person waiting
 * on the answer. */
esp_err_t gc_net_join(const char *ssid, const char *psk);

/* The networks last seen. The scan is refreshed no more often than
 * GC_NET_SCAN_REFRESH_SECONDS, because scanning drops the connection
 * briefly and a settings page that is being watched should not. */
int gc_net_scan(gc_scan_result_t *out, int max_results);

/* Raises or drops the setup hotspot. The password is generated once and
 * kept, so the QR code on the display and the one on the settings page
 * are the same network. */
esp_err_t gc_net_hotspot_start(void);
esp_err_t gc_net_hotspot_stop(void);
bool gc_net_hotspot_active(void);
const char *gc_net_hotspot_password(void);

/* Starts the watcher task described at the top of this file. */
esp_err_t gc_net_watch_start(void);

/* Sets the clock from SNTP, and applies the configured zone. Until this
 * lands the device has no idea what time it is, so nothing is stale and
 * nothing is fresh — the UI says so rather than guessing. */
esp_err_t gc_net_time_sync(const char *timezone);
bool gc_net_time_is_set(void);

/* The IANA zone names this firmware knows, for the settings page to offer.
 * Returns NULL past the end. A zone that is not in the list can still be
 * typed as a POSIX rule, which is passed through untouched — see the note
 * on gc_zones in gc_net.c. */
const char *gc_net_zone_name(int index);

#ifdef __cplusplus
}
#endif
