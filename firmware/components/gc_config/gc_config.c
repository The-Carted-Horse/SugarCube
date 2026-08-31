/*
 * The settings, as one JSON document in NVS.
 *
 * The Pi keeps the same document in config.json, and the keys here are
 * spelt the way they are spelt there — "urgent_low", "api_secret",
 * "password_off", a person's pull source under "source" with its "type" —
 * so somebody holding a device's settings next to a Pi's file is reading
 * one vocabulary rather than translating between two. What only a Pi has
 * is simply absent: the push ports and their secrets (nothing uploads to
 * this device), the admin port (port 80, with nothing to choose), the
 * wallpapers and the weather block (the ambient screen is Pi-only). What
 * only this has is the Wi-Fi block, because a Pi leaves that to
 * NetworkManager and this has nowhere else to put it.
 *
 * The rule worth keeping in mind while editing: nothing replaces the
 * stored settings until the replacement has validated, which is what
 * config.py's write_atomic exists for. A Pi that gets this wrong
 * restart-loops; this would instead hang on a wall showing a wizard for
 * settings it had already been given, which is no better.
 */

#include "gc_config.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "gc_config";

#define GC_NVS_NAMESPACE "glucocube"
#define GC_NVS_KEY "config"

/* A source that arrives without one is polled at this, matching what the
 * Pi's settings page fills in. The pollers apply their own floor
 * (GC_SOURCE_MIN_POLL_SECONDS), so nothing here has to. */
#define GC_DEFAULT_POLL_SECONDS 60

/* The generator exports the mg/dL spelling and not the mmol one, so the
 * other half of units.py's pair is written out here. The spellings below
 * are contract.MMOL_SPELLINGS: what somebody hand-editing settings, or a
 * GlucoCore push, is likely to have written. */
static const char *const UNITS_MMOL = "mmol/L";
static const char *const MMOL_SPELLINGS[] = {"mmol", "mmol/l", "mmoll", "mm"};

/* ---------------------------------------------------------- small text -- */

/* Truncates rather than overflows, and says whether it had to. Every
 * string in the config has a fixed home, and a URL or a password that
 * arrives longer than its field is a setting that will not work — so the
 * callers below log which key it was, rather than leaving somebody to
 * wonder why a site that verified in the wizard now fails to poll. */
static bool copy_field(char *dest, size_t capacity, const char *value)
{
    if (dest == NULL || capacity == 0) {
        return false;
    }
    size_t length = value != NULL ? strlen(value) : 0;
    bool fits = length < capacity;
    if (!fits) {
        length = capacity - 1;
    }
    if (length > 0) {
        memcpy(dest, value, length);
    }
    dest[length] = '\0';
    return fits;
}

static bool blank(const char *text)
{
    return text == NULL || text[0] == '\0';
}

/* units.py's normalize(), which is deliberately forgiving about spelling
 * and deliberately falls back to mg/dL: the stored numbers are mg/dL, so
 * a misread setting shows the truth in the wrong unit rather than a
 * reading divided by eighteen for no reason. */
static bool units_are_mmol(const char *units)
{
    if (blank(units)) {
        return false;
    }
    char folded[16];
    size_t out = 0;
    for (size_t i = 0; units[i] != '\0' && out + 1 < sizeof(folded); i++) {
        char c = units[i];
        if (c == ' ') {
            continue;
        }
        if (c >= 'A' && c <= 'Z') {
            c = (char)(c - 'A' + 'a');
        }
        folded[out++] = c;
    }
    folded[out] = '\0';
    for (size_t i = 0; i < sizeof(MMOL_SPELLINGS) / sizeof(MMOL_SPELLINGS[0]); i++) {
        if (strcmp(folded, MMOL_SPELLINGS[i]) == 0) {
            return true;
        }
    }
    return false;
}

/* -------------------------------------------------------------- source -- */

static const char *const source_names[] = {
    [GC_SOURCE_NONE] = "",
    [GC_SOURCE_GLUCOCORE] = "glucocore",
    [GC_SOURCE_NIGHTSCOUT] = "nightscout",
    [GC_SOURCE_TIDEPOOL] = "tidepool",
};

const char *gc_source_kind_name(gc_source_kind_t kind)
{
    /* No name for GC_SOURCE_NONE on purpose: config.json says "this
     * person has a source" by carrying the key at all, and the empty
     * string is what gc_source_label turns into the default badge. */
    size_t index = (size_t)kind;
    if (index >= sizeof(source_names) / sizeof(source_names[0])) {
        return source_names[GC_SOURCE_NONE];
    }
    return source_names[index];
}

gc_source_kind_t gc_source_kind_from_name(const char *name)
{
    if (blank(name)) {
        return GC_SOURCE_NONE;
    }
    for (size_t i = 1; i < sizeof(source_names) / sizeof(source_names[0]); i++) {
        if (strcmp(name, source_names[i]) == 0) {
            return (gc_source_kind_t)i;
        }
    }
    /* An unknown type is a newer GlucoCore talking to older firmware. The
     * person keeps their panel and their name; nothing polls for them
     * until this firmware learns the source, which is better than
     * refusing to load the whole config over one word. */
    ESP_LOGW(TAG, "unknown source type \"%s\" — that person has no readings", name);
    return GC_SOURCE_NONE;
}

/* Whether a poller could actually start for this person — the same check
 * gc_sources_start applies, and webadmin._source_ready mirrors on the Pi.
 * A person with no source at all is not ready here, where the Pi would
 * call them a push user: nothing uploads to this device. */
static bool source_ready(const gc_user_config_t *user)
{
    switch (user->kind) {
    case GC_SOURCE_GLUCOCORE:
        return !blank(user->patient_id);
    case GC_SOURCE_NIGHTSCOUT:
        return !blank(user->url);
    case GC_SOURCE_TIDEPOOL:
        return !blank(user->email) && !blank(user->password);
    case GC_SOURCE_NONE:
    default:
        return false;
    }
}

/* ------------------------------------------------------------ defaults -- */

void gc_config_defaults(gc_config_t *config)
{
    if (config == NULL) {
        return;
    }
    memset(config, 0, sizeof(*config));
    config->display = (gc_display_config_t){
        .mmol = false,
        .timezone = "",
        .low = GC_LOW_DEFAULT,
        .high = GC_HIGH_DEFAULT,
        .urgent_low = GC_URGENT_LOW_DEFAULT,
        .urgent_high = GC_URGENT_HIGH_DEFAULT,
        .stale_minutes = GC_STALE_MINUTES_DEFAULT,
        /* DisplayConfig.brightness is None on the Pi, meaning "leave the
         * panel alone" — there is a panel driver under it with a setting
         * of its own. Here the backlight is a PWM channel this firmware
         * owns, and nobody else will light it, so the default is a real
         * number. The night figure is inert until somebody sets hours,
         * because equal hours mean no night window (backlight.is_night). */
        .backlight_percent = 100,
        .night_backlight_percent = 40,
        .night_from_hour = 0,
        .night_to_hour = 0,
        .time_format = 24,
        .theme = GC_DEFAULT_THEME,
    };
    config->update_channel = GC_CHANNEL_STABLE;
    /* No people. The Pi's create_default writes two placeholders so a
     * fresh card boots into a screen with two empty panels; here an
     * unconfigured device shows the setup screen instead, which says what
     * to do next rather than showing two panels that never fill in. */
    config->user_count = 0;
}

/* ---------------------------------------------------------- serialising -- */

static bool add_source(cJSON *user_json, const gc_user_config_t *user)
{
    cJSON *source = cJSON_AddObjectToObject(user_json, "source");
    if (source == NULL) {
        return false;
    }
    if (cJSON_AddStringToObject(source, "type", gc_source_kind_name(user->kind)) == NULL) {
        return false;
    }
    switch (user->kind) {
    case GC_SOURCE_GLUCOCORE:
        if (cJSON_AddStringToObject(source, "patient_id", user->patient_id) == NULL) {
            return false;
        }
        break;
    case GC_SOURCE_NIGHTSCOUT:
        /* One field for both auth styles, as on the Pi: the poller works
         * out whether what it holds is a classic API secret or an access
         * token, so there is nothing here to get wrong. */
        if (cJSON_AddStringToObject(source, "url", user->url) == NULL ||
            cJSON_AddStringToObject(source, "api_secret", user->api_secret) == NULL) {
            return false;
        }
        break;
    case GC_SOURCE_TIDEPOOL:
        if (cJSON_AddStringToObject(source, "email", user->email) == NULL ||
            cJSON_AddStringToObject(source, "password", user->password) == NULL) {
            return false;
        }
        break;
    case GC_SOURCE_NONE:
    default:
        break;
    }
    return cJSON_AddNumberToObject(source, "poll_seconds", user->poll_seconds) != NULL;
}

static bool add_overrides(cJSON *user_json, const gc_user_config_t *user)
{
    if (!user->has_low && !user->has_high &&
        !user->has_urgent_low && !user->has_urgent_high) {
        return true;
    }
    cJSON *thresholds = cJSON_AddObjectToObject(user_json, "thresholds");
    if (thresholds == NULL) {
        return false;
    }
    /* Only the overrides somebody set are written. A key that is absent
     * is what "inherit the display's" looks like in config.json, and
     * writing all four would freeze this person's ranges against a later
     * change to the display's. */
    if (user->has_low && cJSON_AddNumberToObject(thresholds, "low", user->low) == NULL) {
        return false;
    }
    if (user->has_high && cJSON_AddNumberToObject(thresholds, "high", user->high) == NULL) {
        return false;
    }
    if (user->has_urgent_low &&
        cJSON_AddNumberToObject(thresholds, "urgent_low", user->urgent_low) == NULL) {
        return false;
    }
    if (user->has_urgent_high &&
        cJSON_AddNumberToObject(thresholds, "urgent_high", user->urgent_high) == NULL) {
        return false;
    }
    return true;
}

static bool add_users(cJSON *root, const gc_config_t *config)
{
    cJSON *users = cJSON_AddArrayToObject(root, "users");
    if (users == NULL) {
        return false;
    }
    int count = config->user_count;
    if (count > GC_MAX_USERS) {
        count = GC_MAX_USERS;
    }
    for (int i = 0; i < count; i++) {
        const gc_user_config_t *user = &config->users[i];
        cJSON *item = cJSON_CreateObject();
        if (item == NULL) {
            return false;
        }
        if (!cJSON_AddItemToArray(users, item)) {
            cJSON_Delete(item);
            return false;
        }
        if (cJSON_AddStringToObject(item, "name", user->name) == NULL) {
            return false;
        }
        if (user->kind != GC_SOURCE_NONE && !add_source(item, user)) {
            return false;
        }
        if (!add_overrides(item, user)) {
            return false;
        }
    }
    return true;
}

static bool add_display(cJSON *root, const gc_display_config_t *display)
{
    cJSON *object = cJSON_AddObjectToObject(root, "display");
    if (object == NULL) {
        return false;
    }
    return cJSON_AddStringToObject(object, "units",
                                   display->mmol ? UNITS_MMOL : GC_UNITS_MGDL) != NULL &&
           cJSON_AddStringToObject(object, "timezone", display->timezone) != NULL &&
           cJSON_AddNumberToObject(object, "low", display->low) != NULL &&
           cJSON_AddNumberToObject(object, "high", display->high) != NULL &&
           cJSON_AddNumberToObject(object, "urgent_low", display->urgent_low) != NULL &&
           cJSON_AddNumberToObject(object, "urgent_high", display->urgent_high) != NULL &&
           cJSON_AddNumberToObject(object, "stale_minutes", display->stale_minutes) != NULL &&
           /* Spelt "brightness" because that is DisplayConfig's name for
            * it and GlucoCore pushes it under that name. */
           cJSON_AddNumberToObject(object, "brightness", display->backlight_percent) != NULL &&
           cJSON_AddNumberToObject(object, "night_brightness",
                                   display->night_backlight_percent) != NULL &&
           cJSON_AddNumberToObject(object, "night_from_hour", display->night_from_hour) != NULL &&
           cJSON_AddNumberToObject(object, "night_to_hour", display->night_to_hour) != NULL &&
           cJSON_AddNumberToObject(object, "time_format", display->time_format) != NULL &&
           /* The Pi keeps the theme in the store rather than the config,
            * because a tap on its footer changes it and the store is what
            * the display and the web UI already share. This device has
            * one place for settings, so it lives here. */
           cJSON_AddStringToObject(object, "theme", gc_theme_name(display->theme)) != NULL;
}

static char *config_to_json(const gc_config_t *config)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    bool ok = add_users(root, config) && add_display(root, &config->display);

    if (ok) {
        cJSON *wifi = cJSON_AddObjectToObject(root, "wifi");
        ok = wifi != NULL &&
             cJSON_AddStringToObject(wifi, "ssid", config->wifi.ssid) != NULL &&
             cJSON_AddStringToObject(wifi, "psk", config->wifi.psk) != NULL;
    }
    if (ok) {
        cJSON *core = cJSON_AddObjectToObject(root, "glucocore");
        ok = core != NULL &&
             cJSON_AddStringToObject(core, "device_id", config->glucocore.device_id) != NULL &&
             cJSON_AddStringToObject(core, "device_token", config->glucocore.device_token) != NULL &&
             cJSON_AddStringToObject(core, "hardware_id", config->glucocore.hardware_id) != NULL &&
             /* sync.py keeps this in the store under
              * __glucocore_config_version; there is no store here that
              * outlives a reboot, so the last applied version rides along
              * with the pairing it belongs to. */
             cJSON_AddNumberToObject(core, "config_version",
                                     config->glucocore.config_version) != NULL;
    }
    if (ok) {
        cJSON *updates = cJSON_AddObjectToObject(root, "updates");
        ok = updates != NULL &&
             cJSON_AddStringToObject(updates, "channel",
                                     gc_channel_name(config->update_channel)) != NULL;
    }
    if (ok) {
        cJSON *admin = cJSON_AddObjectToObject(root, "admin");
        ok = admin != NULL &&
             cJSON_AddStringToObject(admin, "password", config->admin_password) != NULL &&
             cJSON_AddBoolToObject(admin, "password_off", config->admin_password_off) != NULL;
    }
    if (!ok) {
        cJSON_Delete(root);
        return NULL;
    }
    /* Unformatted: this is read by machines and by whoever dumps NVS, and
     * the indentation would cost a third of the blob. */
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return text;
}

/* ------------------------------------------------------------- parsing -- */

static bool json_string(const cJSON *object, const char *key, char *dest, size_t capacity)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(item) || item->valuestring == NULL) {
        return false;
    }
    if (!copy_field(dest, capacity, item->valuestring)) {
        ESP_LOGW(TAG, "\"%s\" was longer than this device can hold and was cut short", key);
    }
    return true;
}

static bool json_float(const cJSON *object, const char *key, float *dest)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    *dest = (float)item->valuedouble;
    return true;
}

static bool json_int(const cJSON *object, const char *key, int *dest)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    *dest = item->valueint;
    return true;
}

static bool json_bool(const cJSON *object, const char *key, bool *dest)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsBool(item)) {
        return false;
    }
    *dest = cJSON_IsTrue(item);
    return true;
}

static void parse_source(const cJSON *source, gc_user_config_t *user)
{
    char type[32] = "";
    json_string(source, "type", type, sizeof(type));
    user->kind = gc_source_kind_from_name(type);

    json_string(source, "patient_id", user->patient_id, sizeof(user->patient_id));
    json_string(source, "url", user->url, sizeof(user->url));
    /* nspull.py reads "api_secret" or "token", because older files and
     * some hosted sites use the second name. Same here, and what comes
     * back out is always spelt "api_secret". */
    if (!json_string(source, "api_secret", user->api_secret, sizeof(user->api_secret))) {
        json_string(source, "token", user->api_secret, sizeof(user->api_secret));
    }
    json_string(source, "email", user->email, sizeof(user->email));
    json_string(source, "password", user->password, sizeof(user->password));
    if (!json_int(source, "poll_seconds", &user->poll_seconds) || user->poll_seconds <= 0) {
        user->poll_seconds = GC_DEFAULT_POLL_SECONDS;
    }
}

static void parse_user(const cJSON *item, gc_user_config_t *user)
{
    memset(user, 0, sizeof(*user));
    user->poll_seconds = GC_DEFAULT_POLL_SECONDS;
    json_string(item, "name", user->name, sizeof(user->name));

    const cJSON *source = cJSON_GetObjectItemCaseSensitive(item, "source");
    if (cJSON_IsObject(source)) {
        parse_source(source, user);
    }
    const cJSON *thresholds = cJSON_GetObjectItemCaseSensitive(item, "thresholds");
    if (cJSON_IsObject(thresholds)) {
        user->has_low = json_float(thresholds, "low", &user->low);
        user->has_high = json_float(thresholds, "high", &user->high);
        user->has_urgent_low = json_float(thresholds, "urgent_low", &user->urgent_low);
        user->has_urgent_high = json_float(thresholds, "urgent_high", &user->urgent_high);
    }
}

static void parse_display(const cJSON *object, gc_display_config_t *display)
{
    char units[16] = "";
    if (json_string(object, "units", units, sizeof(units))) {
        display->mmol = units_are_mmol(units);
    }
    json_string(object, "timezone", display->timezone, sizeof(display->timezone));
    json_float(object, "low", &display->low);
    json_float(object, "high", &display->high);
    json_float(object, "urgent_low", &display->urgent_low);
    json_float(object, "urgent_high", &display->urgent_high);
    json_float(object, "stale_minutes", &display->stale_minutes);
    json_int(object, "brightness", &display->backlight_percent);
    json_int(object, "night_brightness", &display->night_backlight_percent);
    json_int(object, "night_from_hour", &display->night_from_hour);
    json_int(object, "night_to_hour", &display->night_to_hour);
    if (json_int(object, "time_format", &display->time_format) &&
        display->time_format != 12 && display->time_format != 24) {
        /* Coerced rather than rejected, the way config.normalize_layout
         * treats a layout it does not know: a clock this firmware cannot
         * draw should read 24-hour, not stop the device loading. */
        ESP_LOGW(TAG, "time_format %d is neither 12 nor 24 — reading the clock as 24",
                 display->time_format);
        display->time_format = 24;
    }
    char theme[16] = "";
    if (json_string(object, "theme", theme, sizeof(theme))) {
        display->theme = gc_theme_from_name(theme);
    }
}

/* Fills config from the document, leaving anything the document does not
 * mention at the default it arrived with. Nothing in here rejects: a key
 * of the wrong type is a key that is not there. */
static void parse_config(const cJSON *root, gc_config_t *config)
{
    const cJSON *users = cJSON_GetObjectItemCaseSensitive(root, "users");
    if (cJSON_IsArray(users)) {
        int count = 0;
        const cJSON *item = NULL;
        cJSON_ArrayForEach(item, users) {
            if (!cJSON_IsObject(item)) {
                continue;
            }
            if (count >= GC_MAX_USERS) {
                ESP_LOGW(TAG, "more than %d people configured — showing the first %d",
                         GC_MAX_USERS, GC_MAX_USERS);
                break;
            }
            parse_user(item, &config->users[count]);
            count++;
        }
        config->user_count = count;
    }

    const cJSON *display = cJSON_GetObjectItemCaseSensitive(root, "display");
    if (cJSON_IsObject(display)) {
        parse_display(display, &config->display);
    }
    const cJSON *wifi = cJSON_GetObjectItemCaseSensitive(root, "wifi");
    if (cJSON_IsObject(wifi)) {
        json_string(wifi, "ssid", config->wifi.ssid, sizeof(config->wifi.ssid));
        json_string(wifi, "psk", config->wifi.psk, sizeof(config->wifi.psk));
    }
    const cJSON *core = cJSON_GetObjectItemCaseSensitive(root, "glucocore");
    if (cJSON_IsObject(core)) {
        json_string(core, "device_id", config->glucocore.device_id,
                    sizeof(config->glucocore.device_id));
        json_string(core, "device_token", config->glucocore.device_token,
                    sizeof(config->glucocore.device_token));
        json_string(core, "hardware_id", config->glucocore.hardware_id,
                    sizeof(config->glucocore.hardware_id));
        int version = 0;
        if (json_int(core, "config_version", &version)) {
            config->glucocore.config_version = (int32_t)version;
        }
    }
    const cJSON *updates = cJSON_GetObjectItemCaseSensitive(root, "updates");
    if (cJSON_IsObject(updates)) {
        char channel[16] = "";
        if (json_string(updates, "channel", channel, sizeof(channel))) {
            config->update_channel = gc_channel_from_name(channel);
        }
    }
    const cJSON *admin = cJSON_GetObjectItemCaseSensitive(root, "admin");
    if (cJSON_IsObject(admin)) {
        json_string(admin, "password", config->admin_password,
                    sizeof(config->admin_password));
        bool off = false;
        json_bool(admin, "password_off", &off);
        /* A password and "no password on purpose" cannot both be true;
         * the password wins, so a stale flag is inert. config.load's rule. */
        config->admin_password_off = off && blank(config->admin_password);
    }
}

/* ----------------------------------------------------------------- NVS -- */

/* NVS is usually up before anything calls in here, but a partition that
 * has filled or been written by a different IDF version answers every
 * open with an error, and a device that then refuses to load settings is
 * a device nobody can get back into. Erasing costs the settings; not
 * erasing costs the device. */
static esp_err_t ensure_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS is unusable (%s) — erasing it and starting fresh",
                 esp_err_to_name(err));
        err = nvs_flash_erase();
        if (err == ESP_OK) {
            err = nvs_flash_init();
        }
    }
    return err;
}

/* The blob is a few kilobytes, and it is read while the TLS buffers and
 * the framebuffer are competing for what internal RAM there is, so it
 * comes out of PSRAM where there is any. */
static char *blob_buffer(size_t size)
{
    char *buffer = heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    return buffer != NULL ? buffer : malloc(size);
}

esp_err_t gc_config_load(gc_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    gc_config_defaults(config);

    /* Every path below this line answers ESP_OK. A caller that wrapped
     * this in ESP_ERROR_CHECK would otherwise turn "nothing saved yet"
     * into a boot loop, and "nothing saved yet" is how every device
     * starts. gc_config_is_unconfigured is how a caller asks whether
     * there is anything here. */
    esp_err_t err = ensure_nvs();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "no NVS to read settings from (%s) — using defaults",
                 esp_err_to_name(err));
        return ESP_OK;
    }
    nvs_handle_t handle;
    err = nvs_open(GC_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGI(TAG, "nothing saved yet — starting from defaults");
        return ESP_OK;
    }
    size_t length = 0;
    err = nvs_get_blob(handle, GC_NVS_KEY, NULL, &length);
    if (err != ESP_OK || length == 0) {
        nvs_close(handle);
        ESP_LOGI(TAG, "nothing saved yet — starting from defaults");
        return ESP_OK;
    }
    char *buffer = blob_buffer(length + 1);
    if (buffer == NULL) {
        nvs_close(handle);
        ESP_LOGE(TAG, "no room to read %u bytes of settings — using defaults",
                 (unsigned)length);
        return ESP_OK;
    }
    err = nvs_get_blob(handle, GC_NVS_KEY, buffer, &length);
    nvs_close(handle);
    if (err != ESP_OK) {
        free(buffer);
        ESP_LOGE(TAG, "could not read the settings (%s) — using defaults",
                 esp_err_to_name(err));
        return ESP_OK;
    }
    buffer[length] = '\0';

    cJSON *root = cJSON_ParseWithLength(buffer, length);
    free(buffer);
    if (root == NULL || !cJSON_IsObject(root)) {
        /* The device carries on to the wizard rather than to a restart
         * loop. Whatever is in NVS stays there until something valid
         * replaces it, so a bad blob can still be read off a device that
         * somebody is trying to work out. */
        ESP_LOGE(TAG, "the saved settings will not parse — using defaults");
        cJSON_Delete(root);
        return ESP_OK;
    }
    parse_config(root, config);
    cJSON_Delete(root);

    char reason[160];
    if (!gc_config_valid(config, reason, sizeof(reason))) {
        /* Kept anyway, and only said out loud. These are settings that
         * were valid when something wrote them, so throwing away four
         * configured people over one threshold that no longer passes
         * would lose more than it saves; the settings page shows the same
         * reason and the person can fix the one field. */
        ESP_LOGW(TAG, "the saved settings do not fully check out: %s", reason);
    }
    return ESP_OK;
}

esp_err_t gc_config_save(const gc_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    /* write_atomic's order, and its reason: what is live now keeps
     * running unless the replacement is good. */
    char reason[160];
    if (!gc_config_valid(config, reason, sizeof(reason))) {
        ESP_LOGE(TAG, "refusing to save: %s", reason);
        return ESP_ERR_INVALID_ARG;
    }
    char *text = config_to_json(config);
    if (text == NULL) {
        ESP_LOGE(TAG, "ran out of memory writing the settings out");
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = ensure_nvs();
    nvs_handle_t handle;
    if (err == ESP_OK) {
        err = nvs_open(GC_NVS_NAMESPACE, NVS_READWRITE, &handle);
    }
    if (err != ESP_OK) {
        cJSON_free(text);
        ESP_LOGE(TAG, "could not open NVS to save (%s)", esp_err_to_name(err));
        return err;
    }
    /* Stored without the terminating NUL and read back by length, so the
     * blob is exactly the document. nvs_commit is what makes the swap;
     * until it returns, a device that loses power still comes up on the
     * settings it had. */
    err = nvs_set_blob(handle, GC_NVS_KEY, text, strlen(text));
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    size_t written = strlen(text);
    nvs_close(handle);
    cJSON_free(text);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "could not save the settings (%s)", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "saved %u bytes of settings for %d %s", (unsigned)written,
             config->user_count, config->user_count == 1 ? "person" : "people");
    return ESP_OK;
}

/* ---------------------------------------------------------- validation -- */

static bool fail(char *reason, size_t reason_len, const char *format, ...)
    __attribute__((format(printf, 3, 4)));

static bool fail(char *reason, size_t reason_len, const char *format, ...)
{
    if (reason != NULL && reason_len > 0) {
        va_list args;
        va_start(args, format);
        vsnprintf(reason, reason_len, format, args);
        va_end(args);
    }
    return false;
}

/* webadmin._check_ranges, moved down into the config itself. On the Pi
 * only the settings form applies this; here there is no form between a
 * GlucoCore config push and NVS, so the check belongs where every writer
 * goes through it.
 *
 * One deliberate difference: the Pi allows urgent_low == low and
 * urgent_high == high, and this does not. An urgent edge sitting exactly
 * on the ordinary one leaves the low or high band no width at all, so the
 * colour it stands for can never appear — a range that quietly cannot be
 * drawn is worse than a save that says why. */
static bool ranges_valid(gc_thresholds_t th, const char *whose,
                         char *reason, size_t reason_len)
{
    if (th.low >= th.high) {
        return fail(reason, reason_len, "%s low has to be under the high.", whose);
    }
    if (th.urgent_low >= th.low) {
        return fail(reason, reason_len, "%s urgent low has to be under the low.", whose);
    }
    if (th.urgent_high <= th.high) {
        return fail(reason, reason_len, "%s urgent high has to be over the high.", whose);
    }
    if (th.urgent_low <= 0) {
        return fail(reason, reason_len,
                    "Readings are above zero, so %s urgent low has to be too.", whose);
    }
    return true;
}

bool gc_config_valid(const gc_config_t *config, char *reason, size_t reason_len)
{
    if (reason != NULL && reason_len > 0) {
        reason[0] = '\0';
    }
    if (config == NULL) {
        return fail(reason, reason_len, "There are no settings to check.");
    }
    if (config->user_count <= 0) {
        return fail(reason, reason_len, "At least one person has to be configured.");
    }
    if (config->user_count > GC_MAX_USERS) {
        return fail(reason, reason_len, "This display shows at most %d people.", GC_MAX_USERS);
    }
    if (config->display.stale_minutes <= 0) {
        return fail(reason, reason_len,
                    "Readings go stale after a number of minutes, so it has to be over zero.");
    }
    gc_thresholds_t defaults = {
        .low = config->display.low,
        .high = config->display.high,
        .urgent_low = config->display.urgent_low,
        .urgent_high = config->display.urgent_high,
        .stale_minutes = config->display.stale_minutes,
    };
    if (!ranges_valid(defaults, "The", reason, reason_len)) {
        return false;
    }
    for (int i = 0; i < config->user_count; i++) {
        const gc_user_config_t *user = &config->users[i];
        if (blank(user->name)) {
            return fail(reason, reason_len, "Everyone needs a name (person %d has none).", i + 1);
        }
        switch (user->kind) {
        case GC_SOURCE_NIGHTSCOUT:
            if (blank(user->url)) {
                return fail(reason, reason_len,
                            "%s needs the address of their Nightscout site.", user->name);
            }
            break;
        case GC_SOURCE_TIDEPOOL:
            if (blank(user->email) || blank(user->password)) {
                return fail(reason, reason_len,
                            "%s needs both a Tidepool email and password.", user->name);
            }
            break;
        case GC_SOURCE_GLUCOCORE:
            /* The device token the poller also needs lives on the config
             * rather than on the person, and a display can be given
             * people before it is paired, so its absence is not this
             * person's config being wrong. */
            if (blank(user->patient_id)) {
                return fail(reason, reason_len,
                            "%s comes from GlucoCore but has no patient id.", user->name);
            }
            break;
        case GC_SOURCE_NONE:
        default:
            /* Somebody the wizard has named but not yet given a source.
             * Their panel says it is waiting rather than showing a
             * reading, which is honest, so this saves. */
            break;
        }
        char whose[GC_MAX_NAME + 4];
        snprintf(whose, sizeof(whose), "%s's", user->name);
        if (!ranges_valid(gc_merged_thresholds(config, i), whose, reason, reason_len)) {
            return false;
        }
    }
    return true;
}

bool gc_config_is_unconfigured(const gc_config_t *config)
{
    if (config == NULL || config->user_count <= 0) {
        return true;
    }
    int count = config->user_count < GC_MAX_USERS ? config->user_count : GC_MAX_USERS;
    for (int i = 0; i < count; i++) {
        if (source_ready(&config->users[i])) {
            return false;
        }
    }
    /* Named people with nothing feeding them are still nothing to show,
     * so this is the wizard's cue as much as an empty config is. */
    return true;
}

/* --------------------------------------------------------- hardware id -- */

const char *gc_hardware_id(void)
{
    static char id[GC_MAX_ID];
    if (id[0] != '\0') {
        return id;
    }
    /* network.hardware_id()'s "mac-<12 lowercase hex>", so a device
     * registers with GlucoCore under the same shape of id a Pi does.
     * The Pi has to check whether uuid.getnode() invented a random node
     * and fall back to the hostname; the eFuse MAC is burned in at the
     * factory and survives a re-flash, so there is nothing to fall back
     * to and nothing to check. */
    uint8_t mac[6] = {0};
    esp_err_t err = esp_efuse_mac_get_default(mac);
    if (err != ESP_OK) {
        err = esp_read_mac(mac, ESP_MAC_WIFI_STA);
    }
    if (err != ESP_OK) {
        /* Without a MAC there is no stable id to give, and inventing one
         * would pair this display as a different device after every
         * reboot. Saying so is the only honest answer. */
        ESP_LOGE(TAG, "no MAC to derive a hardware id from (%s)", esp_err_to_name(err));
        return "";
    }
    snprintf(id, sizeof(id), "mac-%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return id;
}

/* ---------------------------------------------------------- thresholds -- */

gc_thresholds_t gc_merged_thresholds(const gc_config_t *config, int user)
{
    gc_thresholds_t merged = {
        .low = GC_LOW_DEFAULT,
        .high = GC_HIGH_DEFAULT,
        .urgent_low = GC_URGENT_LOW_DEFAULT,
        .urgent_high = GC_URGENT_HIGH_DEFAULT,
        .stale_minutes = GC_STALE_MINUTES_DEFAULT,
    };
    if (config == NULL) {
        return merged;
    }
    merged.low = config->display.low;
    merged.high = config->display.high;
    merged.urgent_low = config->display.urgent_low;
    merged.urgent_high = config->display.urgent_high;
    /* config.merged_thresholds returns four numbers and the Pi reads
     * stale_minutes off the display beside it (display.py's panel draw).
     * Carrying it here means one call answers everything a panel needs to
     * colour a reading; there is no per-person override for it in either
     * language. */
    merged.stale_minutes = config->display.stale_minutes;

    if (user < 0 || user >= config->user_count || user >= GC_MAX_USERS) {
        return merged;
    }
    const gc_user_config_t *person = &config->users[user];
    /* `if key in merged and value` in the Python: an override of zero is
     * skipped, because zero is what an emptied form field turns into and
     * not a threshold anybody means. */
    if (person->has_low && person->low != 0.0f) {
        merged.low = person->low;
    }
    if (person->has_high && person->high != 0.0f) {
        merged.high = person->high;
    }
    if (person->has_urgent_low && person->urgent_low != 0.0f) {
        merged.urgent_low = person->urgent_low;
    }
    if (person->has_urgent_high && person->urgent_high != 0.0f) {
        merged.urgent_high = person->urgent_high;
    }
    return merged;
}
