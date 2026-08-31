/*
 * What both GlucoCube board profiles have in common: an ESP32-S3-WROOM-1
 * with 16 MB flash and 8 MB octal PSRAM, driving an 800x480 ST7262 RGB565
 * panel through the S3's LCD_CAM peripheral, with a GT911 capacitive touch
 * controller on I2C.
 *
 * A profile includes this and then defines its own pin map; nothing here
 * names a GPIO.
 */

#pragma once

#define GC_PANEL_WIDTH 800
#define GC_PANEL_HEIGHT 480
#define GC_PANEL_BITS_PER_PIXEL 16

/*
 * Panel timing. 800x480 at these porches is a ~16 MHz pixel clock, which is
 * about 60 Hz once the blanking is counted:
 *
 *   (800 + 8 + 8 + 4) x (480 + 8 + 8 + 4) = 820 x 500 = 410,000 px
 *   16,000,000 / 410,000 = 39 Hz
 *
 * The dashboard redraws about once a second, so refresh rate is not what
 * limits it; a lower pixel clock is the safer trade on a board whose panel
 * ribbon is long and unshielded. Raise GC_PANEL_PCLK_HZ if a specific panel
 * shows tearing rather than flicker.
 */
#define GC_PANEL_PCLK_HZ (16 * 1000 * 1000)
#define GC_PANEL_HSYNC_PULSE_WIDTH 4
#define GC_PANEL_HSYNC_BACK_PORCH 8
#define GC_PANEL_HSYNC_FRONT_PORCH 8
#define GC_PANEL_VSYNC_PULSE_WIDTH 4
#define GC_PANEL_VSYNC_BACK_PORCH 8
#define GC_PANEL_VSYNC_FRONT_PORCH 8
#define GC_PANEL_PCLK_ACTIVE_NEG 1

/*
 * Two framebuffers in PSRAM, so a frame is never scanned out half-drawn.
 * 800 x 480 x 2 bytes = 750 KB each, 1.5 MB of the 8 MB.
 */
#define GC_PANEL_NUM_FBS 2

/*
 * The RGB peripheral reads the framebuffer over the PSRAM bus, which it
 * shares with the CPU. A bounce buffer in internal SRAM decouples the two:
 * DMA drains the bounce buffer while the next chunk is fetched, which is
 * what stops the "drifting image" this peripheral is known for. Ten lines
 * is the usual compromise between internal RAM and interrupt load.
 */
#define GC_PANEL_BOUNCE_BUFFER_LINES 10
#define GC_PANEL_BOUNCE_BUFFER_PX \
    (GC_PANEL_WIDTH * GC_PANEL_BOUNCE_BUFFER_LINES)

/* GT911, at its two possible addresses; the driver probes for the live one. */
#define GC_TOUCH_I2C_HZ (400 * 1000)
#define GC_TOUCH_MAX_POINTS 5

/* Backlight PWM: 8-bit duty at 5 kHz is silent and flicker-free. */
#define GC_BACKLIGHT_PWM_HZ 5000
#define GC_BACKLIGHT_DUTY_BITS 8
#define GC_BACKLIGHT_DEFAULT_PERCENT 100
