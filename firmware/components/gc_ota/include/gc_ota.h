/*
 * Self-update from the same GitHub releases the Raspberry Pi image uses.
 *
 * Same two channels, same version ordering, same [force-update] marker as
 * glucocube/updater.py — a release that is offered to one product is
 * offered to the other. What differs is what gets installed: the Pi swaps
 * a directory of Python, and this writes the other OTA slot and reboots
 * into it, with the bootloader rolling back if the new image cannot get
 * itself onto the network and draw a frame.
 *
 * The asset it looks for is named for the board profile:
 *
 *     glucocube-esp32-<board>-<version>.bin
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#include "gc_contract.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GC_MAX_VERSION 32

typedef struct {
    bool available;
    bool forced;             /* the notes carry [force-update] */
    char current[GC_MAX_VERSION];
    char latest[GC_MAX_VERSION];
    char asset_url[256];
    int64_t checked_at_ms;
} gc_ota_state_t;

/* The version this binary was built as, stamped by the release workflow. */
const char *gc_ota_current_version(void);

/* Version ordering the way updater.py does it — v1.2.3 outranks every
 * v1.2.3-rc.N, and (1, 0) and (1, 0, 0) are the same version. It lives in
 * its own file so a host compiler can check it against the Python. */
#include "gc_version.h"

/* Asks GitHub what the newest release on this channel is. */
esp_err_t gc_ota_check(gc_channel_t channel, gc_ota_state_t *out);

/* Downloads and writes the other OTA slot, then reboots into it. Does not
 * return on success. */
esp_err_t gc_ota_install(const gc_ota_state_t *state);

/* Starts the background checker: every GC_UPDATE_CHECK_HOURS, and a
 * release marked [force-update] installs itself at that check rather than
 * waiting to be pressed. */
esp_err_t gc_ota_start(gc_channel_t channel);
void gc_ota_stop(void);

gc_ota_state_t gc_ota_state(void);

/* Called once the app has proved itself — network up, a frame drawn — so
 * the bootloader stops holding the previous image in reserve. Until this
 * is called, a reset rolls back. */
void gc_ota_mark_valid(void);

#ifdef __cplusplus
}
#endif
