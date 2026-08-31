/*
 * GlucoCube, on an ESP32-S3.
 *
 * The same product as the Raspberry Pi image in this repository: it boots
 * straight into the dashboard, pulls each person's glucose from GlucoCore,
 * Nightscout or Tidepool itself, and is set up from a phone by scanning
 * what is on its own screen.
 *
 * The order below is the order it has to be in. The panel comes up before
 * anything else, because a device that has already lit its screen is a
 * device somebody can be told something by — including that it cannot get
 * on the network, which is the failure a headless board has no way to
 * report.
 */

#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "gc_board.h"
#include "gc_config.h"
#include "gc_contract.h"
#include "gc_httpd.h"
#include "gc_net.h"
#include "gc_ota.h"
#include "gc_sources.h"
#include "gc_store.h"
#include "gc_ui.h"

static const char *TAG = "glucocube";

/* The dashboard redraws about once a second, like the Pi's loop; taps are
 * polled far more often than that, because a control that answers a second
 * after it is pressed reads as broken. */
#define DRAW_INTERVAL_MS 1000
#define TOUCH_POLL_MS 30

static gc_config_t s_config;
static gc_store_t *s_store;

static int64_t now_ms(void)
{
    /* Wall-clock milliseconds once SNTP has landed. Before that the device
     * genuinely does not know what time it is, and everything it holds is
     * neither fresh nor stale — gc_ui says so rather than guessing. */
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

/* ------------------------------------------------------------ backlight -- */

/* The overnight dim, from the display config. Equal hours mean the night
 * figure is never used, which is how "leave the panel alone" is written. */
static void apply_backlight(void)
{
    const gc_display_config_t *display = &s_config.display;
    if (display->backlight_percent <= 0) {
        return;
    }
    int percent = display->backlight_percent;

    if (display->night_backlight_percent > 0
        && display->night_from_hour != display->night_to_hour) {
        const time_t now = (time_t)(now_ms() / 1000);
        struct tm tm_now;
        localtime_r(&now, &tm_now);
        const int hour = tm_now.tm_hour;
        const int from = display->night_from_hour;
        const int to = display->night_to_hour;
        /* The night window usually crosses midnight, so it is two ranges
         * rather than one comparison. */
        const bool night = from < to ? (hour >= from && hour < to)
                                     : (hour >= from || hour < to);
        if (night) {
            percent = display->night_backlight_percent;
        }
    }
    if (percent != gc_board_get_backlight()) {
        gc_board_set_backlight(percent);
    }
}

/* --------------------------------------------------------- what to show -- */

static gc_screen_t screen_for_now(void)
{
    /* The hotspot wins: a device that has taken the network away to offer
     * its own has exactly one thing to say, and a stale dashboard behind it
     * would only suggest the readings are still arriving. */
    if (gc_net_hotspot_active()) {
        return GC_SCREEN_HOTSPOT;
    }
    if (gc_config_is_unconfigured(&s_config)) {
        return GC_SCREEN_SETUP;
    }
    return GC_SCREEN_DASHBOARD;
}

static void refresh_screen_urls(void)
{
    char url[256];
    switch (screen_for_now()) {
    case GC_SCREEN_HOTSPOT:
        gc_ui_set_hotspot(GC_HOTSPOT_SSID, gc_net_hotspot_password());
        break;
    case GC_SCREEN_SETUP:
        gc_httpd_signed_url(url, sizeof(url), "/setup");
        gc_ui_set_setup_url(url, "Scan to set this display up");
        break;
    default:
        gc_httpd_signed_url(url, sizeof(url), "/settings");
        break;
    }
}

/* A save from the settings page or the wizard: everything that reads the
 * config has to be told, and the pollers have to be restarted before any of
 * them writes a reading into a slot that now belongs to somebody else. */
static void on_config_changed(const gc_config_t *config)
{
    ESP_LOGI(TAG, "settings saved; restarting the data sources");
    gc_sources_stop();
    s_config = *config;
    for (int i = 0; i < GC_MAX_USERS; i++) {
        gc_store_clear_user(s_store, i);
    }
    gc_ui_set_config(&s_config);
    gc_ui_set_screen(screen_for_now());
    refresh_screen_urls();
    apply_backlight();
    gc_sources_start(&s_config, s_store);
}

/* ---------------------------------------------------------------- main -- */

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* A partition an older build left behind is worth less than a
         * device that boots; the settings are re-entered from the wizard. */
        ESP_LOGW(TAG, "the settings partition needs erasing, doing that now");
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    gc_config_load(&s_config);

    ESP_ERROR_CHECK(gc_board_init());
    ESP_ERROR_CHECK(gc_ui_init(&s_config));
    apply_backlight();

    s_store = gc_store_create();
    if (s_store == NULL) {
        ESP_LOGE(TAG, "no room for the readings");
        abort();
    }

    /* Draw one frame before the network: bringing Wi-Fi up takes seconds,
     * and a black panel for those seconds looks like a device that did not
     * start. */
    gc_ui_set_screen(screen_for_now());
    gc_ui_draw(s_store, now_ms());

    gc_net_init(&s_config);
    gc_net_watch_start();
    gc_net_time_sync(s_config.display.timezone);

    gc_httpd_start(&s_config, s_store, on_config_changed);
    gc_httpd_set_captive(gc_net_hotspot_active());
    refresh_screen_urls();

    gc_sources_start(&s_config, s_store);
    gc_ota_start(s_config.update_channel);

    ESP_LOGI(TAG, "%s %s on %s", "GlucoCube", gc_ota_current_version(),
             gc_board_name());

    int64_t next_draw = 0;
    bool proved = false;
    gc_screen_t last_screen = gc_ui_screen();

    while (true) {
        const int64_t uptime_ms = (int64_t)(esp_timer_get_time() / 1000);

        gc_touch_point_t touch;
        bool redraw = false;
        if (gc_board_read_touch(&touch) && touch.pressed) {
            redraw = gc_ui_handle_touch(touch.x, touch.y);
            if (redraw && gc_ui_screen() == GC_SCREEN_DASHBOARD) {
                /* A tap on SETTINGS pops the code that signs a phone in. */
                char url[256];
                gc_httpd_signed_url(url, sizeof(url), "/settings");
                gc_ui_show_settings_qr(url);
            }
        }

        const gc_screen_t screen = screen_for_now();
        if (screen != last_screen) {
            gc_ui_set_screen(screen);
            gc_httpd_set_captive(screen == GC_SCREEN_HOTSPOT);
            refresh_screen_urls();
            last_screen = screen;
            redraw = true;
        }

        if (redraw || uptime_ms >= next_draw) {
            const gc_ota_state_t update = gc_ota_state();
            gc_ui_set_pending_update(update.available ? update.latest : "");
            apply_backlight();
            gc_ui_draw(s_store, now_ms());
            next_draw = uptime_ms + DRAW_INTERVAL_MS;

            /* An update that cannot get onto the network and draw a frame
             * is rolled back at the next reset. Once both have happened
             * there is nothing left to prove, and the previous image can
             * be released. */
            if (!proved && gc_net_is_online()) {
                gc_ota_mark_valid();
                proved = true;
            }
        }

        vTaskDelay(pdMS_TO_TICKS(TOUCH_POLL_MS));
    }
}
