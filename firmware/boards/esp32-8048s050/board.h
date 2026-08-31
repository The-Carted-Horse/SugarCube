/*
 * Sunton ESP32-8048S050 — 5.0" 800x480 IPS with GT911 capacitive touch,
 * ESP32-S3-WROOM-1-N16R8 (16 MB flash, 8 MB octal PSRAM).
 *
 * Sold as "ESP32-8048S050C" and under several shop names. The pin map below
 * is the one the board's own schematic and the community board definitions
 * agree on.
 *
 * VERIFY BEFORE THE FIRST FLASH ON A NEW BATCH. Sunton has shipped
 * revisions of neighbouring boards (the 4.3" 8048S043 especially) with the
 * touch controller moved between I2C pairs and the backlight moved off
 * GPIO2. If the panel lights but shows noise, the RGB pins are wrong; if
 * the panel is correct but taps do nothing, GC_TOUCH_* is wrong. Nothing
 * else in the firmware needs changing — this file is the whole hardware
 * surface.
 */

#pragma once

#include "boards/common/st7262_gt911_800x480.h"

#define GC_BOARD_NAME "Sunton ESP32-8048S050"

/* ---- RGB panel ---------------------------------------------------------
 * The panel takes 5 red, 6 green and 5 blue lines: the low bits of each
 * channel are tied off on the board, so R0..R4 here are the panel's R3..R7.
 */
#define GC_PANEL_PIN_DE 40
#define GC_PANEL_PIN_VSYNC 41
#define GC_PANEL_PIN_HSYNC 39
#define GC_PANEL_PIN_PCLK 42
#define GC_PANEL_PIN_DISP_EN (-1)

#define GC_PANEL_PIN_R0 45
#define GC_PANEL_PIN_R1 48
#define GC_PANEL_PIN_R2 47
#define GC_PANEL_PIN_R3 21
#define GC_PANEL_PIN_R4 14

#define GC_PANEL_PIN_G0 9
#define GC_PANEL_PIN_G1 46
#define GC_PANEL_PIN_G2 3
#define GC_PANEL_PIN_G3 8
#define GC_PANEL_PIN_G4 16
#define GC_PANEL_PIN_G5 1

#define GC_PANEL_PIN_B0 15
#define GC_PANEL_PIN_B1 7
#define GC_PANEL_PIN_B2 6
#define GC_PANEL_PIN_B3 5
#define GC_PANEL_PIN_B4 4

/* ---- backlight --------------------------------------------------------- */
#define GC_BACKLIGHT_PIN 2
#define GC_BACKLIGHT_ACTIVE_HIGH 1

/* ---- GT911 touch ------------------------------------------------------- */
#define GC_TOUCH_I2C_PORT 0
#define GC_TOUCH_PIN_SDA 19
#define GC_TOUCH_PIN_SCL 20
#define GC_TOUCH_PIN_RST 38
#define GC_TOUCH_PIN_INT (-1)
/*
 * The panel is mounted the same way up as the display, so no transform is
 * needed. The Pi has the same knob (GLUCOCUBE_TOUCH_TRANSFORM) for boards
 * where it is not.
 */
#define GC_TOUCH_SWAP_XY 0
#define GC_TOUCH_MIRROR_X 0
#define GC_TOUCH_MIRROR_Y 0

/* ---- SD card (unused by the firmware, wired on the board) --------------- */
#define GC_SD_PIN_MISO 13
#define GC_SD_PIN_MOSI 11
#define GC_SD_PIN_SCK 12
#define GC_SD_PIN_CS 10

/* ---- other ------------------------------------------------------------- */
/* No user button and no addressable LED on this board. */
#define GC_PIN_BOOT_BUTTON 0
#define GC_PIN_STATUS_LED (-1)
