/*
 * SugarCube ESP32-S3 — the purpose-built GlucoCube board.
 *
 * Same silicon and same panel as the Sunton profile: ESP32-S3-WROOM-1-N16R8
 * (16 MB flash, 8 MB octal PSRAM) driving an 800x480 ST7262 RGB565 LCD with
 * GT911 capacitive touch.
 *
 * ---------------------------------------------------------------------------
 * THE PIN MAP BELOW IS A BRING-UP PLACEHOLDER, copied from the Sunton
 * ESP32-8048S050 so that this profile builds and can be flashed to a Sunton
 * board while the real hardware is in fabrication. Replace each number from
 * the SugarCube schematic before the first run on real boards.
 *
 * Nothing outside this file knows a GPIO number, so correcting the map is
 * the whole hardware change: no driver, no layout and no build step depends
 * on which pins were chosen.
 * ---------------------------------------------------------------------------
 */

#pragma once

#include "boards/common/st7262_gt911_800x480.h"

#define GC_BOARD_NAME "SugarCube ESP32-S3"

/* Left over from bring-up on borrowed hardware. Delete this line once the
 * numbers below come from the SugarCube schematic — gc_board.c logs a
 * warning at boot for as long as it is defined, so a placeholder pin map
 * cannot quietly ship. */
#define GC_BOARD_PINS_PROVISIONAL 1

/* ---- RGB panel --------------------------------------------------------- */
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
#define GC_TOUCH_SWAP_XY 0
#define GC_TOUCH_MIRROR_X 0
#define GC_TOUCH_MIRROR_Y 0

/* ---- other ------------------------------------------------------------- */
#define GC_PIN_BOOT_BUTTON 0
#define GC_PIN_STATUS_LED (-1)
