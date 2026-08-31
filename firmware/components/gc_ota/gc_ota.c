/*
 * Self-update, from the same GitHub releases the Raspberry Pi image uses.
 *
 * A port of glucocube/updater.py's half that decides *whether* to update:
 * the two channels, the version ordering, and the [force-update] marker. A
 * release offered to one product is offered to the other, on the same day,
 * because both read the same list the same way.
 *
 * What differs is the installing. The Pi swaps a directory of Python and
 * restarts a service; this writes the other OTA slot and reboots into it,
 * and if the new image cannot get itself onto the network and draw a frame
 * the bootloader puts the old one back. That last part is why
 * gc_ota_mark_valid exists and why main.c does not call it until both have
 * happened.
 */

#include "gc_ota.h"

#include "gc_version.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "gc_ota";

/* The version the release workflow stamps in. Without it — a developer
 * build — the device calls itself 0.0.0, which every real release outranks,
 * so a board off the bench offers an update rather than never seeing one.
 *
 * Not "0.0.0-dev": updater.py's grammar only knows the pre-release labels
 * alpha, beta, rc and pre, so a version with any other suffix parses as
 * nothing and never compares as newer *or* older. A bench build stamped
 * that way would silently never update. */
#ifndef GC_VERSION
#define GC_VERSION "0.0.0"
#endif

#define RELEASES_URL "https://api.github.com/repos/" GC_REPO "/releases?per_page=30"

/* GitHub's answer for thirty releases with their assets runs to a few
 * hundred kilobytes. It is read into PSRAM and capped: a reply larger than
 * this is a reply we do not understand, and truncating it is better than
 * exhausting the heap the display is drawing from. */
#define MAX_RESPONSE (512 * 1024)

static gc_ota_state_t s_state;
static gc_channel_t s_channel;
static TaskHandle_t s_task;
static volatile bool s_stop;

const char *gc_ota_current_version(void)
{
    return GC_VERSION;
}

/* --------------------------------------------------------------- fetch -- */

static char *fetch(const char *url, int *out_status)
{
    esp_http_client_config_t config = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 20000,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return NULL;
    }
    char agent[64];
    snprintf(agent, sizeof(agent), "GlucoCube/%s", gc_ota_current_version());
    esp_http_client_set_header(client, "User-Agent", agent);
    esp_http_client_set_header(client, "Accept", "application/vnd.github+json");

    char *body = NULL;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "could not reach GitHub: %s", esp_err_to_name(err));
        goto done;
    }
    esp_http_client_fetch_headers(client);
    if (out_status != NULL) {
        *out_status = esp_http_client_get_status_code(client);
    }

    body = heap_caps_malloc(MAX_RESPONSE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (body == NULL) {
        ESP_LOGE(TAG, "no room to read the release list");
        goto done;
    }
    int total = 0;
    while (total < MAX_RESPONSE - 1) {
        const int got = esp_http_client_read(client, body + total,
                                             MAX_RESPONSE - 1 - total);
        if (got <= 0) {
            break;
        }
        total += got;
    }
    body[total] = '\0';
    if (total >= MAX_RESPONSE - 1) {
        ESP_LOGW(TAG, "the release list was longer than %d bytes and was cut "
                      "short; the newest releases are still in it",
                 MAX_RESPONSE);
    }

done:
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return body;
}

/* The asset this board installs. Both the OTA image and the factory image
 * carry the board's name and end in .bin, and writing the factory image
 * into an OTA slot would brick the device — so the one with the bootloader
 * in it is explicitly excluded rather than merely not chosen. */
static const char *asset_url_for_this_board(const cJSON *release)
{
    const cJSON *assets = cJSON_GetObjectItemCaseSensitive(release, "assets");
    const cJSON *asset = NULL;
    cJSON_ArrayForEach(asset, assets) {
        const cJSON *name = cJSON_GetObjectItemCaseSensitive(asset, "name");
        const cJSON *url =
            cJSON_GetObjectItemCaseSensitive(asset, "browser_download_url");
        if (!cJSON_IsString(name) || !cJSON_IsString(url)) {
            continue;
        }
        const char *text = name->valuestring;
        const size_t length = strlen(text);
        if (length < 4 || strcmp(text + length - 4, ".bin") != 0) {
            continue;
        }
        if (strstr(text, "-factory") != NULL) {
            continue;
        }
        if (strstr(text, GC_BOARD_ID) == NULL) {
            continue;
        }
        return url->valuestring;
    }
    return NULL;
}

esp_err_t gc_ota_check(gc_channel_t channel, gc_ota_state_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(out, 0, sizeof(*out));
    snprintf(out->current, sizeof(out->current), "%s", gc_ota_current_version());
    out->checked_at_ms = (int64_t)(esp_timer_get_time() / 1000);

    int status = 0;
    char *body = fetch(RELEASES_URL, &status);
    if (body == NULL) {
        return ESP_FAIL;
    }
    if (status != 200) {
        ESP_LOGW(TAG, "GitHub answered %d when asked for the releases", status);
        heap_caps_free(body);
        return ESP_FAIL;
    }

    cJSON *releases = cJSON_Parse(body);
    heap_caps_free(body);
    if (!cJSON_IsArray(releases)) {
        ESP_LOGW(TAG, "the release list was not a list");
        cJSON_Delete(releases);
        return ESP_FAIL;
    }

    /* The newest by version order, not by position: GitHub sorts by
     * publication date, and a patch cut after a minor would win that. */
    const cJSON *best = NULL;
    const char *best_tag = NULL;
    const cJSON *release = NULL;
    cJSON_ArrayForEach(release, releases) {
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(release, "draft"))) {
            continue;
        }
        const bool prerelease = cJSON_IsTrue(
            cJSON_GetObjectItemCaseSensitive(release, "prerelease"));
        if (prerelease && channel != GC_CHANNEL_BETA) {
            continue;
        }
        const cJSON *tag = cJSON_GetObjectItemCaseSensitive(release, "tag_name");
        if (!cJSON_IsString(tag)) {
            continue;
        }
        /* Comparing against "0" is how an unreadable tag is dropped: it
         * cannot be newer than anything, including nothing. */
        if (best_tag == NULL) {
            if (gc_ota_version_is_newer(tag->valuestring, "0")) {
                best = release;
                best_tag = tag->valuestring;
            }
        } else if (gc_ota_version_is_newer(tag->valuestring, best_tag)) {
            best = release;
            best_tag = tag->valuestring;
        }
    }

    if (best == NULL) {
        ESP_LOGI(TAG, "no release on the %s channel this device can read",
                 gc_channel_name(channel));
        cJSON_Delete(releases);
        return ESP_OK;
    }

    const cJSON *tag = cJSON_GetObjectItemCaseSensitive(best, "tag_name");
    const char *version = tag->valuestring;
    if (*version == 'v' || *version == 'V') {
        version++;
    }
    snprintf(out->latest, sizeof(out->latest), "%s", version);

    const cJSON *body_json = cJSON_GetObjectItemCaseSensitive(best, "body");
    const cJSON *name_json = cJSON_GetObjectItemCaseSensitive(best, "name");
    /* The marker is matched case-insensitively, as updater.py does by
     * lowercasing the notes first. */
    out->forced =
        (cJSON_IsString(body_json)
         && strcasestr(body_json->valuestring, GC_FORCE_MARKER) != NULL)
        || (cJSON_IsString(name_json)
            && strcasestr(name_json->valuestring, GC_FORCE_MARKER) != NULL);

    out->available = gc_ota_version_is_newer(out->latest, out->current);
    if (out->available) {
        const char *url = asset_url_for_this_board(best);
        if (url == NULL) {
            /* A release that carries no image for this board is not an
             * update this device can take, whatever its version says. */
            ESP_LOGW(TAG, "%s has no image for %s", out->latest, GC_BOARD_ID);
            out->available = false;
            out->forced = false;
        } else {
            snprintf(out->asset_url, sizeof(out->asset_url), "%s", url);
        }
    }

    cJSON_Delete(releases);
    return ESP_OK;
}

/* ------------------------------------------------------------- install -- */

esp_err_t gc_ota_install(const gc_ota_state_t *state)
{
    if (state == NULL || !state->available || state->asset_url[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    ESP_LOGI(TAG, "installing %s from %s", state->latest, state->asset_url);

    esp_http_client_config_t http = {
        .url = state->asset_url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 30000,
        .keep_alive_enable = true,
    };
    esp_https_ota_config_t config = {
        .http_config = &http,
        /* GitHub answers a release download with a redirect to its asset
         * host, so the redirect has to be followed rather than reported. */
        .http_client_init_cb = NULL,
    };

    esp_err_t err = esp_https_ota(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "the update did not install: %s — staying on %s",
                 esp_err_to_name(err), gc_ota_current_version());
        return err;
    }
    ESP_LOGI(TAG, "installed %s; restarting into it", state->latest);
    vTaskDelay(pdMS_TO_TICKS(500));   /* let the log line get out */
    esp_restart();
    return ESP_OK;
}

void gc_ota_mark_valid(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t ota_state;
    if (esp_ota_get_state_partition(running, &ota_state) != ESP_OK) {
        return;
    }
    if (ota_state != ESP_OTA_IMG_PENDING_VERIFY) {
        return;
    }
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        ESP_LOGI(TAG, "this image works; the previous one can go");
    }
}

/* -------------------------------------------------------------- checker -- */

static void checker(void *arg)
{
    (void)arg;
    /* Not immediately: the network has to come up, and the first frame
     * matters more than the check does. */
    vTaskDelay(pdMS_TO_TICKS(60 * 1000));

    while (!s_stop) {
        gc_ota_state_t found;
        if (gc_ota_check(s_channel, &found) == ESP_OK) {
            s_state = found;
            if (found.available) {
                ESP_LOGI(TAG, "%s is available (running %s)",
                         found.latest, found.current);
                /* A release marked [force-update] is one every device
                 * should have; it installs itself rather than waiting to
                 * be pressed. */
                if (found.forced) {
                    ESP_LOGW(TAG, "%s is marked %s — installing it now",
                             found.latest, GC_FORCE_MARKER);
                    gc_ota_install(&found);
                }
            }
        }
        for (int slept = 0; slept < GC_UPDATE_CHECK_HOURS * 3600 && !s_stop;
             slept += 5) {
            vTaskDelay(pdMS_TO_TICKS(5000));
        }
    }
    s_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t gc_ota_start(gc_channel_t channel)
{
    s_channel = channel;
    s_stop = false;
    snprintf(s_state.current, sizeof(s_state.current), "%s",
             gc_ota_current_version());
    if (s_task != NULL) {
        return ESP_OK;
    }
    if (xTaskCreate(checker, "gc_ota", 6144, NULL, 3, &s_task) != pdPASS) {
        ESP_LOGE(TAG, "could not start the update checker");
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void gc_ota_stop(void)
{
    s_stop = true;
}

gc_ota_state_t gc_ota_state(void)
{
    return s_state;
}
