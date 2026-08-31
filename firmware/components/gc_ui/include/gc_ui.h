/*
 * The dashboard, drawn.
 *
 * Same layout as the Raspberry Pi's split screen: one panel per person,
 * each with a header badge, the reading in large type with its trend arrow
 * and delta, a FORECAST row, the 3-hour history and 2-hour forecast chart,
 * and a stats row; then a footer across the bottom with the clock, the
 * settings QR control and the day/night toggle.
 *
 * Every position and size comes from GC_L_* in gc_contract.h, which is
 * generated from the same contract.LAYOUT the Pi lays itself out with, so
 * the two screens are the same drawing at the same resolution rather than
 * two drawings that resemble each other.
 *
 * Ambient mode — one person at a time over a photograph — is Pi-only for
 * now; see firmware/README.md.
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

/* What the screen is showing. The wizard screens exist because a device
 * with no network cannot serve a settings page to be configured from, so
 * the panel itself has to say what to do next. */
typedef enum {
    GC_SCREEN_DASHBOARD,
    GC_SCREEN_SETUP,      /* configured for nobody yet: scan to begin */
    GC_SCREEN_HOTSPOT,    /* no network: join GlucoCube-Setup */
    GC_SCREEN_PAIRING,    /* waiting for somebody to approve the request */
} gc_screen_t;

esp_err_t gc_ui_init(const gc_config_t *config);
void gc_ui_deinit(void);

/* Re-reads the config after a save — thresholds, units, theme, who the
 * people are. */
void gc_ui_set_config(const gc_config_t *config);

void gc_ui_set_screen(gc_screen_t screen);
gc_screen_t gc_ui_screen(void);

/* The QR code and caption the setup and hotspot screens show. */
void gc_ui_set_setup_url(const char *url, const char *caption);
void gc_ui_set_hotspot(const char *ssid, const char *password);

/* Draws one frame and pushes it to the panel. Called about once a second
 * by the draw task, and immediately after a tap. */
esp_err_t gc_ui_draw(gc_store_t *store, int64_t now_ms);

/* Feeds a touch in. Returns true when it hit something, which is the
 * draw loop's cue to redraw now rather than at the next tick. */
bool gc_ui_handle_touch(int x, int y);

/* The theme, which a tap on the footer or a save from the settings page
 * can both change. */
void gc_ui_set_theme(gc_theme_t theme);
gc_theme_t gc_ui_theme(void);

/* Puts the settings QR up, as tapping SETTINGS does. It times out after
 * GC_QR_OPEN_SECONDS: this hangs on a wall, and a dashboard covered by a
 * QR code all afternoon helps nobody. */
void gc_ui_show_settings_qr(const char *url);

/* An update the device has found, shown in the footer. */
void gc_ui_set_pending_update(const char *version);

/* Renders the current frame as a PNG into a caller-supplied buffer, for
 * the settings page's live view of the screen. Returns the number of
 * bytes written, or 0 if it did not fit. */
size_t gc_ui_screenshot_png(uint8_t *out, size_t capacity);

#ifdef __cplusplus
}
#endif
