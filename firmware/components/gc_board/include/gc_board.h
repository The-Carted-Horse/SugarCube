/*
 * The only file that knows a GPIO number is boards/<profile>/board.h; this
 * is what the rest of the firmware talks to instead.
 *
 * Selected at configure time with -DGC_BOARD=<profile>, which the top-level
 * CMakeLists turns into GC_BOARD_HEADER. Adding a board is adding a header
 * and an entry in contract.ESP32_BOARDS — no driver here changes.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_lcd_panel_ops.h"

#include GC_BOARD_HEADER

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int x;
    int y;
    bool pressed;
} gc_touch_point_t;

/* Brings up the RGB panel, the backlight and the touch controller. Safe to
 * call once, from app_main, before anything draws. */
esp_err_t gc_board_init(void);

/* Which profile this binary was built for — the id from
 * contract.ESP32_BOARDS, which is also what its release asset is named. */
const char *gc_board_id(void);
const char *gc_board_name(void);

int gc_board_width(void);
int gc_board_height(void);

/* Pushes a full RGB565 frame to the panel. The buffer is width*height
 * uint16_t, which at 800x480 is 750 KB and belongs in PSRAM. */
esp_err_t gc_board_present(const uint16_t *framebuffer);

/* A framebuffer the panel can scan out of directly, allocated in PSRAM and
 * aligned for DMA. Freed with gc_board_free_framebuffer. */
uint16_t *gc_board_alloc_framebuffer(void);
void gc_board_free_framebuffer(uint16_t *framebuffer);

/* 0-100. Anything the panel cannot do is clamped, and a board with no
 * backlight control returns ESP_ERR_NOT_SUPPORTED without complaining. */
esp_err_t gc_board_set_backlight(int percent);
int gc_board_get_backlight(void);

/* The current touch, if a finger is down. Reads the controller directly —
 * there is no event queue, because the draw loop polls once a frame and a
 * queue would only let taps pile up behind a slow frame. */
bool gc_board_read_touch(gc_touch_point_t *point);

#ifdef __cplusplus
}
#endif
