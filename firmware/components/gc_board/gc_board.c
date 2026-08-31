/*
 * The panel, the backlight and the touch controller — the only code in the
 * firmware that touches hardware, and the only code that reads a pin
 * number. Everything specific to a board is in boards/<profile>/board.h,
 * which the top-level CMakeLists points GC_BOARD_HEADER at.
 */

#include "gc_board.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_rgb.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "gc_board";

static esp_lcd_panel_handle_t s_panel;
static i2c_master_bus_handle_t s_i2c_bus;
static i2c_master_dev_handle_t s_touch;
static int s_backlight_percent = GC_BACKLIGHT_DEFAULT_PERCENT;
static bool s_backlight_ready;

/* ------------------------------------------------------------- panel ---- */

static esp_err_t panel_init(void)
{
    /* data_gpio_nums is indexed by bit position in the RGB565 word: blue in
     * the low five bits, then six of green, then five of red. Getting this
     * order wrong lights the panel and shows noise, which is the failure
     * the board header warns about. */
    esp_lcd_rgb_panel_config_t config = {
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .data_width = 16,
        .bits_per_pixel = GC_PANEL_BITS_PER_PIXEL,
        .psram_trans_align = 64,
        .num_fbs = GC_PANEL_NUM_FBS,
        .de_gpio_num = GC_PANEL_PIN_DE,
        .pclk_gpio_num = GC_PANEL_PIN_PCLK,
        .vsync_gpio_num = GC_PANEL_PIN_VSYNC,
        .hsync_gpio_num = GC_PANEL_PIN_HSYNC,
        .disp_gpio_num = GC_PANEL_PIN_DISP_EN,
        .data_gpio_nums = {
            GC_PANEL_PIN_B0, GC_PANEL_PIN_B1, GC_PANEL_PIN_B2,
            GC_PANEL_PIN_B3, GC_PANEL_PIN_B4,
            GC_PANEL_PIN_G0, GC_PANEL_PIN_G1, GC_PANEL_PIN_G2,
            GC_PANEL_PIN_G3, GC_PANEL_PIN_G4, GC_PANEL_PIN_G5,
            GC_PANEL_PIN_R0, GC_PANEL_PIN_R1, GC_PANEL_PIN_R2,
            GC_PANEL_PIN_R3, GC_PANEL_PIN_R4,
        },
        .timings = {
            .pclk_hz = GC_PANEL_PCLK_HZ,
            .h_res = GC_PANEL_WIDTH,
            .v_res = GC_PANEL_HEIGHT,
            .hsync_pulse_width = GC_PANEL_HSYNC_PULSE_WIDTH,
            .hsync_back_porch = GC_PANEL_HSYNC_BACK_PORCH,
            .hsync_front_porch = GC_PANEL_HSYNC_FRONT_PORCH,
            .vsync_pulse_width = GC_PANEL_VSYNC_PULSE_WIDTH,
            .vsync_back_porch = GC_PANEL_VSYNC_BACK_PORCH,
            .vsync_front_porch = GC_PANEL_VSYNC_FRONT_PORCH,
            .flags = {
                .pclk_active_neg = GC_PANEL_PCLK_ACTIVE_NEG,
            },
        },
        .flags = {
            /* 750 KB a frame, twice over: the S3 has 512 KB of internal
             * SRAM in total, so this is not a choice. */
            .fb_in_psram = true,
        },
    };

    esp_err_t err = esp_lcd_new_rgb_panel(&config, &s_panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "the panel would not come up: %s", esp_err_to_name(err));
        return err;
    }
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(s_panel), TAG, "panel reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    return ESP_OK;
}

/* --------------------------------------------------------- backlight ---- */

static esp_err_t backlight_init(void)
{
#if GC_BACKLIGHT_PIN < 0
    return ESP_ERR_NOT_SUPPORTED;
#else
    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = GC_BACKLIGHT_DUTY_BITS,
        .timer_num = LEDC_TIMER_0,
        .freq_hz = GC_BACKLIGHT_PWM_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "backlight timer");

    ledc_channel_config_t channel = {
        .gpio_num = GC_BACKLIGHT_PIN,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_0,
        .timer_sel = LEDC_TIMER_0,
        .duty = 0,
        .hpoint = 0,
    };
    ESP_RETURN_ON_ERROR(ledc_channel_config(&channel), TAG, "backlight channel");
    s_backlight_ready = true;
    return gc_board_set_backlight(GC_BACKLIGHT_DEFAULT_PERCENT);
#endif
}

esp_err_t gc_board_set_backlight(int percent)
{
    if (!s_backlight_ready) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (percent < 0) {
        percent = 0;
    }
    if (percent > 100) {
        percent = 100;
    }
    s_backlight_percent = percent;

    const uint32_t full = (1u << GC_BACKLIGHT_DUTY_BITS) - 1;
    uint32_t duty = (full * (uint32_t)percent) / 100u;
#if !GC_BACKLIGHT_ACTIVE_HIGH
    duty = full - duty;
#endif
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty),
                        TAG, "backlight duty");
    return ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

int gc_board_get_backlight(void)
{
    return s_backlight_percent;
}

/* ------------------------------------------------------------- touch ---- */
/*
 * GT911, driven directly rather than through a third-party component. It is
 * a handful of I2C reads, and its reset sequencing is board wiring — which
 * is exactly what this file exists to hold.
 */

#define GT911_ADDR_PRIMARY 0x5D
#define GT911_ADDR_ALT 0x14
#define GT911_REG_PRODUCT_ID 0x8140
#define GT911_REG_STATUS 0x814E
#define GT911_REG_POINT1 0x8150
#define GT911_POINT_STRIDE 8

static esp_err_t gt911_read(uint16_t reg, uint8_t *out, size_t len)
{
    if (s_touch == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t address[2] = {(uint8_t)(reg >> 8), (uint8_t)(reg & 0xFF)};
    return i2c_master_transmit_receive(s_touch, address, sizeof(address),
                                       out, len, 200);
}

static esp_err_t gt911_write_byte(uint16_t reg, uint8_t value)
{
    if (s_touch == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t payload[3] = {
        (uint8_t)(reg >> 8), (uint8_t)(reg & 0xFF), value,
    };
    return i2c_master_transmit(s_touch, payload, sizeof(payload), 200);
}

static void gt911_reset(void)
{
    /* The controller latches its I2C address from the INT line as reset is
     * released: INT low means 0x5D, INT high means 0x14. On a board that
     * does not wire INT there is nothing to hold, and the address is
     * whichever the part was made with — so both are probed below. */
#if GC_TOUCH_PIN_RST >= 0
    gpio_config_t pins = {
        .pin_bit_mask = (1ULL << GC_TOUCH_PIN_RST)
#if GC_TOUCH_PIN_INT >= 0
                        | (1ULL << GC_TOUCH_PIN_INT)
#endif
        ,
        .mode = GPIO_MODE_OUTPUT,
    };
    gpio_config(&pins);

    gpio_set_level(GC_TOUCH_PIN_RST, 0);
#if GC_TOUCH_PIN_INT >= 0
    gpio_set_level(GC_TOUCH_PIN_INT, 0);
#endif
    vTaskDelay(pdMS_TO_TICKS(12));
    gpio_set_level(GC_TOUCH_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(60));

#if GC_TOUCH_PIN_INT >= 0
    /* Released as an input afterwards: it is the controller's line to
     * drive once it is running. */
    gpio_config_t interrupt = {
        .pin_bit_mask = 1ULL << GC_TOUCH_PIN_INT,
        .mode = GPIO_MODE_INPUT,
    };
    gpio_config(&interrupt);
#endif
#endif
}

static esp_err_t touch_init(void)
{
    i2c_master_bus_config_t bus = {
        .i2c_port = GC_TOUCH_I2C_PORT,
        .sda_io_num = GC_TOUCH_PIN_SDA,
        .scl_io_num = GC_TOUCH_PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus, &s_i2c_bus), TAG, "i2c bus");

    gt911_reset();

    const uint8_t candidates[] = {GT911_ADDR_PRIMARY, GT911_ADDR_ALT};
    for (size_t i = 0; i < sizeof(candidates); i++) {
        i2c_device_config_t device = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = candidates[i],
            .scl_speed_hz = GC_TOUCH_I2C_HZ,
        };
        if (i2c_master_bus_add_device(s_i2c_bus, &device, &s_touch) != ESP_OK) {
            continue;
        }
        uint8_t product_id[4] = {0};
        if (gt911_read(GT911_REG_PRODUCT_ID, product_id, sizeof(product_id))
            == ESP_OK) {
            ESP_LOGI(TAG, "GT911 at 0x%02X, product %.4s",
                     candidates[i], (const char *)product_id);
            return ESP_OK;
        }
        i2c_master_bus_rm_device(s_touch);
        s_touch = NULL;
    }

    /* A display nobody can tap is still a display. The dashboard is
     * readable and the settings page is reachable from any phone on the
     * network, so this is a warning rather than a failure to boot. */
    ESP_LOGW(TAG, "no GT911 answered on SDA %d / SCL %d — taps will not work",
             GC_TOUCH_PIN_SDA, GC_TOUCH_PIN_SCL);
    return ESP_ERR_NOT_FOUND;
}

bool gc_board_read_touch(gc_touch_point_t *point)
{
    if (point == NULL || s_touch == NULL) {
        return false;
    }
    memset(point, 0, sizeof(*point));

    uint8_t status = 0;
    if (gt911_read(GT911_REG_STATUS, &status, 1) != ESP_OK) {
        return false;
    }
    /* Bit 7 says the controller has a coordinate set ready; the low nibble
     * says how many fingers are in it. */
    if ((status & 0x80) == 0) {
        return false;
    }
    const int touches = status & 0x0F;

    uint8_t raw[GT911_POINT_STRIDE];
    bool got = false;
    if (touches > 0
        && gt911_read(GT911_REG_POINT1, raw, sizeof(raw)) == ESP_OK) {
        int x = raw[1] | ((int)raw[2] << 8);
        int y = raw[3] | ((int)raw[4] << 8);

#if GC_TOUCH_SWAP_XY
        int swap = x;
        x = y;
        y = swap;
#endif
#if GC_TOUCH_MIRROR_X
        x = GC_PANEL_WIDTH - 1 - x;
#endif
#if GC_TOUCH_MIRROR_Y
        y = GC_PANEL_HEIGHT - 1 - y;
#endif
        /* A controller reports coordinates from its own grid, which can
         * overhang the panel by a pixel or two at the edges. */
        if (x < 0) {
            x = 0;
        }
        if (y < 0) {
            y = 0;
        }
        if (x >= GC_PANEL_WIDTH) {
            x = GC_PANEL_WIDTH - 1;
        }
        if (y >= GC_PANEL_HEIGHT) {
            y = GC_PANEL_HEIGHT - 1;
        }
        point->x = x;
        point->y = y;
        point->pressed = true;
        got = true;
    }

    /* The status byte has to be cleared or the controller stops reporting
     * anything new — a touchscreen that works for exactly one tap after
     * boot is this line missing. */
    gt911_write_byte(GT911_REG_STATUS, 0);
    return got;
}

/* -------------------------------------------------------------- frame --- */

uint16_t *gc_board_alloc_framebuffer(void)
{
    const size_t bytes = (size_t)GC_PANEL_WIDTH * GC_PANEL_HEIGHT
                         * sizeof(uint16_t);
    uint16_t *buffer = heap_caps_aligned_alloc(
        64, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buffer == NULL) {
        ESP_LOGE(TAG, "no room in PSRAM for a %u KB frame",
                 (unsigned)(bytes / 1024));
        return NULL;
    }
    memset(buffer, 0, bytes);
    return buffer;
}

void gc_board_free_framebuffer(uint16_t *framebuffer)
{
    heap_caps_free(framebuffer);
}

esp_err_t gc_board_present(const uint16_t *framebuffer)
{
    if (s_panel == NULL || framebuffer == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* With two framebuffers the driver writes into the one it is not
     * scanning out and switches at the next vertical blank, so a frame is
     * never shown half-drawn. */
    return esp_lcd_panel_draw_bitmap(s_panel, 0, 0, GC_PANEL_WIDTH,
                                     GC_PANEL_HEIGHT, framebuffer);
}

/* --------------------------------------------------------------- init --- */

esp_err_t gc_board_init(void)
{
#ifdef GC_BOARD_PINS_PROVISIONAL
    ESP_LOGW(TAG, "%s is built with a placeholder pin map — see "
                  "firmware/boards/%s/board.h before trusting this on real "
                  "hardware", GC_BOARD_NAME, GC_BOARD_ID);
#endif
    ESP_RETURN_ON_ERROR(panel_init(), TAG, "panel");

    /* Neither of these is fatal: a board with no backlight control is a
     * board whose panel is simply always on, and a display nobody can tap
     * still shows the numbers. */
    esp_err_t err = backlight_init();
    if (err != ESP_OK && err != ESP_ERR_NOT_SUPPORTED) {
        ESP_LOGW(TAG, "backlight control unavailable: %s", esp_err_to_name(err));
    }
    touch_init();

    ESP_LOGI(TAG, "%s ready: %dx%d", GC_BOARD_NAME,
             GC_PANEL_WIDTH, GC_PANEL_HEIGHT);
    return ESP_OK;
}

const char *gc_board_id(void)
{
    return GC_BOARD_ID;
}

const char *gc_board_name(void)
{
    return GC_BOARD_NAME;
}

int gc_board_width(void)
{
    return GC_PANEL_WIDTH;
}

int gc_board_height(void)
{
    return GC_PANEL_HEIGHT;
}
