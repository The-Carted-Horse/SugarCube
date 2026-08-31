/*
 * The device's own web app: the dashboard, the setup wizard and settings.
 *
 * The same paths the Raspberry Pi serves, so a phone with one bookmarked
 * finds the same page on the other, and /api/dashboard.json carries the
 * same keys webadmin.py's _dashboard_data() builds — everything in it is
 * mg/dL whatever "units" says, because units are how a number is written,
 * not what it is.
 *
 * While the setup hotspot is up this also answers the connectivity probe
 * every phone makes on joining a network, which is what makes the setup
 * page open by itself instead of waiting to be asked for.
 *
 * The pages are plain HTML with inline CSS. There is no CDN to reach for:
 * a device being set up has, by definition, no internet yet.
 */

#include "gc_httpd.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "cJSON.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_random.h"
#include "mbedtls/base64.h"

#include "gc_net.h"
#include "gc_ota.h"
#include "gc_predict.h"
#include "gc_sources.h"
#include "gc_synclog.h"
#include "gc_ui.h"

static const char *TAG = "gc_httpd";

static httpd_handle_t s_server;
static gc_config_t *s_config;
static gc_store_t *s_store;
static gc_config_changed_cb s_on_change;
static bool s_captive;
static char s_key[33];

/* gc_sources has one of these too, behind its own private header. Two lines
 * of clock reading is a smaller thing to repeat than a dependency from the
 * web app on the data layer's internals. */
static int64_t now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

/* ---------------------------------------------------------------- auth -- */

/* A URL that opens settings without a login prompt, so scanning the code on
 * the panel signs the phone in. The key is minted once per boot and lives
 * only in RAM: a code photographed off a wall months ago should not still
 * work. */
static void ensure_key(void)
{
    if (s_key[0] != '\0') {
        return;
    }
    for (int i = 0; i < 4; i++) {
        snprintf(s_key + i * 8, 9, "%08" PRIx32, esp_random());
    }
}

int gc_httpd_signed_url(char *out, size_t capacity, const char *path)
{
    ensure_key();
    const char *ip = gc_net_ip();
    if (ip == NULL || ip[0] == '\0') {
        ip = GC_HOTSPOT_ADDR;
    }
    if (s_config != NULL && s_config->admin_password[0] == '\0') {
        /* Nothing to sign in to, so nothing to carry. */
        return snprintf(out, capacity, "http://%s%s", ip, path);
    }
    return snprintf(out, capacity, "http://%s%s?key=%s", ip, path, s_key);
}

static bool query_has_key(httpd_req_t *request)
{
    ensure_key();
    char query[160];
    if (httpd_req_get_url_query_str(request, query, sizeof(query)) != ESP_OK) {
        return false;
    }
    char value[40];
    if (httpd_query_key_value(query, "key", value, sizeof(value)) != ESP_OK) {
        return false;
    }
    return strcmp(value, s_key) == 0;
}

/* Compared to the end rather than stopping at the first difference: the
 * time a comparison takes should not say how much of a password was right. */
static bool secret_equal(const char *a, const char *b)
{
    const size_t length_a = strlen(a), length_b = strlen(b);
    unsigned char difference = (unsigned char)(length_a ^ length_b);
    for (size_t i = 0; i < length_a && i < length_b; i++) {
        difference |= (unsigned char)(a[i] ^ b[i]);
    }
    return difference == 0;
}

static bool authorized(httpd_req_t *request)
{
    if (s_config == NULL || s_config->admin_password[0] == '\0') {
        return true;   /* no password is a choice; see the Access page */
    }
    if (query_has_key(request)) {
        return true;
    }
    char header[256];
    if (httpd_req_get_hdr_value_str(request, "Authorization", header,
                                    sizeof(header)) != ESP_OK) {
        return false;
    }
    if (strncmp(header, "Basic ", 6) != 0) {
        return false;
    }
    unsigned char decoded[192];
    size_t written = 0;
    if (mbedtls_base64_decode(decoded, sizeof(decoded) - 1, &written,
                              (const unsigned char *)header + 6,
                              strlen(header) - 6) != 0) {
        return false;
    }
    decoded[written] = '\0';
    const char *colon = strchr((const char *)decoded, ':');
    if (colon == NULL) {
        return false;
    }
    return secret_equal(colon + 1, s_config->admin_password);
}

static esp_err_t deny(httpd_req_t *request)
{
    httpd_resp_set_status(request, "401 Unauthorized");
    httpd_resp_set_hdr(request, "WWW-Authenticate",
                       "Basic realm=\"GlucoCube\"");
    return httpd_resp_send(request, "Sign in to see this display.", -1);
}

/* -------------------------------------------------------------- pages -- */

/* One stylesheet for every page, in the product's own palette, generated
 * from the contract so the web app and the panel never drift into two
 * different greens. */
static void send_page_head(httpd_req_t *request, const char *title)
{
    char head[2048];
    const uint32_t bg = gc_color888(GC_THEME_LIGHT, GC_C_BG);
    const uint32_t fg = gc_color888(GC_THEME_LIGHT, GC_C_FG);
    const uint32_t dim = gc_color888(GC_THEME_LIGHT, GC_C_DIM);
    const uint32_t line = gc_color888(GC_THEME_LIGHT, GC_C_LINE);
    const uint32_t band = gc_color888(GC_THEME_LIGHT, GC_C_BAND);
    const uint32_t ok = gc_color888(GC_THEME_LIGHT, GC_C_IN_RANGE);
    const uint32_t dark_bg = gc_color888(GC_THEME_DARK, GC_C_BG);
    const uint32_t dark_fg = gc_color888(GC_THEME_DARK, GC_C_FG);
    const uint32_t dark_dim = gc_color888(GC_THEME_DARK, GC_C_DIM);
    const uint32_t dark_line = gc_color888(GC_THEME_DARK, GC_C_LINE);
    const uint32_t dark_band = gc_color888(GC_THEME_DARK, GC_C_BAND);

    const int length = snprintf(head, sizeof(head),
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>"
        ":root{color-scheme:light dark;--bg:#%06lX;--fg:#%06lX;--dim:#%06lX;"
        "--line:#%06lX;--band:#%06lX;--ok:#%06lX}"
        "@media(prefers-color-scheme:dark){:root{--bg:#%06lX;--fg:#%06lX;"
        "--dim:#%06lX;--line:#%06lX;--band:#%06lX}}"
        "*{box-sizing:border-box}"
        "body{margin:0;padding:2rem 1.1rem 4rem;background:var(--bg);"
        "color:var(--fg);font:16px/1.6 ui-sans-serif,system-ui,sans-serif}"
        "main{max-width:32rem;margin:0 auto}"
        "h1{font-size:1.5rem;letter-spacing:-.02em;margin:0 0 .3rem}"
        "p.lede{color:var(--dim);margin:0 0 2rem}"
        "label{display:block;margin:1.1rem 0 .3rem;font-weight:600}"
        "input,select{width:100%%;padding:.7rem .8rem;font:inherit;"
        "border:1px solid var(--line);border-radius:10px;background:var(--bg);"
        "color:var(--fg)}"
        "button{margin-top:1.6rem;width:100%%;padding:.85rem;font:inherit;"
        "font-weight:600;border:0;border-radius:10px;background:var(--fg);"
        "color:var(--bg);cursor:pointer}"
        "a.row{display:flex;justify-content:space-between;gap:1rem;"
        "padding:.9rem .2rem;border-bottom:1px solid var(--line);"
        "color:inherit;text-decoration:none}"
        "a.row span{color:var(--dim);text-align:right}"
        ".note{border-left:3px solid var(--line);padding-left:1rem;"
        "color:var(--dim);margin:1.5rem 0}"
        ".bad{color:#%06lX}"
        "</style></head><body><main>",
        title, (unsigned long)bg, (unsigned long)fg, (unsigned long)dim,
        (unsigned long)line, (unsigned long)band, (unsigned long)ok,
        (unsigned long)dark_bg, (unsigned long)dark_fg, (unsigned long)dark_dim,
        (unsigned long)dark_line, (unsigned long)dark_band,
        (unsigned long)gc_color888(GC_THEME_LIGHT, GC_C_URGENT));
    httpd_resp_set_type(request, "text/html; charset=utf-8");
    httpd_resp_send_chunk(request, head, length);
}

static esp_err_t send_page_end(httpd_req_t *request)
{
    httpd_resp_send_chunk(request, "</main></body></html>", HTTPD_RESP_USE_STRLEN);
    return httpd_resp_send_chunk(request, NULL, 0);
}

/* --------------------------------------------------------- the payload -- */

static void add_number_or_null(cJSON *object, const char *key, bool has,
                               double value)
{
    /* Absent and zero are different things to anything reading this — an
     * IOB of nothing is not the same as a pump that did not say. */
    if (has) {
        cJSON_AddNumberToObject(object, key, value);
    } else {
        cJSON_AddNullToObject(object, key);
    }
}

static cJSON *dashboard_json(void)
{
    const int64_t now = now_ms();
    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "now", (double)now);
    cJSON_AddStringToObject(root, "units",
                            s_config->display.mmol ? "mmol/L" : GC_UNITS_MGDL);

    const gc_ota_state_t update = gc_ota_state();
    cJSON *update_json = cJSON_AddObjectToObject(root, "update");
    cJSON_AddStringToObject(update_json, "current", update.current);
    if (update.latest[0] != '\0') {
        cJSON_AddStringToObject(update_json, "latest", update.latest);
    } else {
        cJSON_AddNullToObject(update_json, "latest");
    }
    cJSON_AddBoolToObject(update_json, "available", update.available);

    cJSON *thresholds = cJSON_AddObjectToObject(root, "thresholds");
    cJSON_AddNumberToObject(thresholds, "low", s_config->display.low);
    cJSON_AddNumberToObject(thresholds, "high", s_config->display.high);
    cJSON_AddNumberToObject(thresholds, "urgent_low",
                            s_config->display.urgent_low);
    cJSON_AddNumberToObject(thresholds, "urgent_high",
                            s_config->display.urgent_high);
    cJSON_AddNumberToObject(thresholds, "stale_minutes",
                            s_config->display.stale_minutes);

    cJSON *users = cJSON_AddArrayToObject(root, "users");
    for (int i = 0; i < s_config->user_count; i++) {
        gc_snapshot_t snap;
        if (!gc_store_snapshot(s_store, i, now, &snap)) {
            continue;
        }
        const gc_thresholds_t merged = gc_merged_thresholds(s_config, i);
        cJSON *user = cJSON_CreateObject();
        cJSON_AddStringToObject(user, "name", s_config->users[i].name);
        cJSON_AddStringToObject(
            user, "source_label",
            gc_source_label(gc_source_kind_name(s_config->users[i].kind)));

        cJSON *user_thresholds = cJSON_AddObjectToObject(user, "thresholds");
        cJSON_AddNumberToObject(user_thresholds, "low", merged.low);
        cJSON_AddNumberToObject(user_thresholds, "high", merged.high);
        cJSON_AddNumberToObject(user_thresholds, "urgent_low",
                                merged.urgent_low);
        cJSON_AddNumberToObject(user_thresholds, "urgent_high",
                                merged.urgent_high);
        cJSON_AddNumberToObject(user_thresholds, "stale_minutes",
                                merged.stale_minutes);

        add_number_or_null(user, "sgv", snap.has_sgv, snap.sgv);
        add_number_or_null(user, "sgv_date", snap.has_sgv,
                           (double)snap.sgv_date);
        if (snap.direction[0] != '\0') {
            cJSON_AddStringToObject(user, "direction", snap.direction);
        } else {
            cJSON_AddNullToObject(user, "direction");
        }
        add_number_or_null(user, "delta", snap.has_delta, snap.delta);
        add_number_or_null(user, "iob", snap.has_iob, snap.iob);
        add_number_or_null(user, "cob", snap.has_cob, snap.cob);
        add_number_or_null(user, "last_carbs", snap.has_last_carbs,
                           snap.last_carbs);
        add_number_or_null(user, "last_carbs_date", snap.has_last_carbs,
                           (double)snap.last_carbs_date);
        add_number_or_null(user, "last_bolus", snap.has_last_bolus,
                           snap.last_bolus);
        add_number_or_null(user, "last_bolus_date", snap.has_last_bolus,
                           (double)snap.last_bolus_date);

        /* History and the forecast series are both [[ms, value], ...], as
         * the Pi sends them. */
        cJSON *history = cJSON_AddArrayToObject(user, "history");
        for (int p = 0; p < snap.history_count; p++) {
            cJSON *point = cJSON_CreateArray();
            cJSON_AddItemToArray(point,
                                 cJSON_CreateNumber((double)snap.history[p].ms));
            cJSON_AddItemToArray(point,
                                 cJSON_CreateNumber(snap.history[p].value));
            cJSON_AddItemToArray(history, point);
        }

        gc_forecast_t forecast;
        if (gc_predict(&snap, now, &forecast)) {
            cJSON *node = cJSON_AddObjectToObject(user, "forecast");
            cJSON *horizons = cJSON_AddObjectToObject(node, "horizons");
            for (int h = 0; h < GC_HORIZON_COUNT; h++) {
                if (!forecast.horizon_valid[h]) {
                    continue;
                }
                char key[8];
                snprintf(key, sizeof(key), "%d", gc_horizons[h]);
                cJSON_AddNumberToObject(horizons, key, forecast.horizons[h]);
            }
            cJSON *series = cJSON_AddArrayToObject(node, "series");
            for (int p = 0; p < forecast.series_count; p++) {
                cJSON *point = cJSON_CreateArray();
                cJSON_AddItemToArray(
                    point, cJSON_CreateNumber((double)forecast.series[p].ms));
                cJSON_AddItemToArray(
                    point, cJSON_CreateNumber(forecast.series[p].value));
                cJSON_AddItemToArray(series, point);
            }
            cJSON_AddStringToObject(node, "source",
                                    forecast.estimated ? "est" : "device");
        } else {
            cJSON_AddNullToObject(user, "forecast");
        }
        cJSON_AddItemToArray(users, user);
    }
    return root;
}

static esp_err_t send_json(httpd_req_t *request, cJSON *json)
{
    char *text = cJSON_PrintUnformatted(json);
    cJSON_Delete(json);
    if (text == NULL) {
        httpd_resp_set_status(request, "500 Internal Server Error");
        return httpd_resp_send(request, "{}", 2);
    }
    httpd_resp_set_type(request, "application/json");
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    const esp_err_t err = httpd_resp_send(request, text, HTTPD_RESP_USE_STRLEN);
    cJSON_free(text);
    return err;
}

/* ------------------------------------------------------------ handlers -- */

static esp_err_t handle_dashboard_json(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    return send_json(request, dashboard_json());
}

static esp_err_t handle_health_json(httpd_req_t *request)
{
    /* Deliberately open and deliberately tiny: this is what the page a
     * save just redirected away from polls to know the device is back. */
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "ok", true);
    cJSON_AddStringToObject(json, "version", gc_ota_current_version());
    return send_json(request, json);
}

static esp_err_t handle_screen_png(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    /* The Pi writes a PNG of every frame for the settings page's live view.
     * Encoding 750 KB of RGB565 every few seconds costs more than the page
     * is worth here, so this says so rather than serving a broken image. */
    httpd_resp_set_status(request, "503 Service Unavailable");
    httpd_resp_set_type(request, "application/json");
    return httpd_resp_send(request,
                           "{\"error\":\"this display does not photograph its "
                           "own screen; the dashboard above is the same data\"}",
                           HTTPD_RESP_USE_STRLEN);
}

static esp_err_t handle_dashboard(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "GlucoCube");
    httpd_resp_send_chunk(request,
        "<h1>GlucoCube</h1><p class=\"lede\">What the display is showing.</p>"
        "<div id=\"people\"></div>"
        "<p><a class=\"row\" href=\"/settings\">Settings<span>&rsaquo;</span></a></p>"
        "<script>"
        "function fmt(v,mmol){if(v===null||v===undefined)return'--';"
        "return mmol?(v/18).toFixed(1):Math.round(v);}"
        "async function tick(){"
        "const r=await fetch('/api/dashboard.json',{cache:'no-store'});"
        "if(!r.ok)return;const d=await r.json();const mmol=d.units!=='mg/dL';"
        "document.getElementById('people').innerHTML=d.users.map(u=>{"
        "const age=u.sgv_date?Math.round((d.now-u.sgv_date)/60000):null;"
        "const f=u.forecast&&u.forecast.horizons&&u.forecast.horizons['120'];"
        "return `<a class=\"row\" href=\"/settings\"><b>${u.name}</b>"
        "<span>${fmt(u.sgv,mmol)} ${u.direction||''} &middot; "
        "${age===null?'--':age+'m ago'}${f?' &middot; 2h '+fmt(f,mmol):''}"
        "</span></a>`;}).join('');}"
        "tick();setInterval(tick,30000);"
        "</script>", HTTPD_RESP_USE_STRLEN);
    return send_page_end(request);
}

/* ---- the setup wizard ---- */

/* Deliberately short: Wi-Fi, where in the world it is, and who it shows.
 * Everything else has a sensible default and a settings page. */
static esp_err_t handle_setup(httpd_req_t *request)
{
    send_page_head(request, "Set up GlucoCube");

    const char *error = gc_net_last_error();
    char body[3072];
    int length = snprintf(body, sizeof(body),
        "<h1>Set up GlucoCube</h1>"
        "<p class=\"lede\">Three questions, then it is a display.</p>");
    if (error != NULL && error[0] != '\0' && !gc_net_is_online()) {
        length += snprintf(body + length, sizeof(body) - length,
            "<p class=\"bad\">Last attempt: %s</p>", error);
    }
    length += snprintf(body + length, sizeof(body) - length,
        "<form method=\"post\" action=\"/setup/wifi\">"
        "<label for=\"ssid\">Wi-Fi network</label>"
        "<input list=\"seen\" id=\"ssid\" name=\"ssid\" autocapitalize=\"off\" "
        "autocorrect=\"off\" required><datalist id=\"seen\">");

    gc_scan_result_t seen[GC_MAX_SCAN_RESULTS];
    const int found = gc_net_scan(seen, GC_MAX_SCAN_RESULTS);
    for (int i = 0; i < found && length < (int)sizeof(body) - 128; i++) {
        length += snprintf(body + length, sizeof(body) - length,
                           "<option value=\"%s\">", seen[i].ssid);
    }
    length += snprintf(body + length, sizeof(body) - length,
        "</datalist>"
        "<label for=\"psk\">Password</label>"
        "<input id=\"psk\" name=\"psk\" type=\"password\" "
        "autocapitalize=\"off\">"
        "<button type=\"submit\">Join this network</button></form>"
        "<div class=\"note\">The display drops this setup network while it "
        "tries, so this page will stop loading. That is expected. If it "
        "works the display shows its new address; if it does not, this "
        "network comes back within a minute or two and says why.</div>");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

/* Reads a urlencoded form field out of a POST body. */
static bool form_field(const char *body, const char *name, char *out,
                       size_t capacity)
{
    out[0] = '\0';
    const size_t name_length = strlen(name);
    const char *cursor = body;
    while (cursor != NULL && *cursor != '\0') {
        if (strncmp(cursor, name, name_length) == 0
            && cursor[name_length] == '=') {
            cursor += name_length + 1;
            size_t written = 0;
            while (*cursor != '\0' && *cursor != '&'
                   && written + 1 < capacity) {
                if (*cursor == '+') {
                    out[written++] = ' ';
                    cursor++;
                } else if (*cursor == '%' && cursor[1] != '\0'
                           && cursor[2] != '\0') {
                    char hex[3] = {cursor[1], cursor[2], '\0'};
                    out[written++] = (char)strtol(hex, NULL, 16);
                    cursor += 3;
                } else {
                    out[written++] = *cursor++;
                }
            }
            out[written] = '\0';
            return true;
        }
        cursor = strchr(cursor, '&');
        if (cursor != NULL) {
            cursor++;
        }
    }
    return false;
}

static esp_err_t read_body(httpd_req_t *request, char *out, size_t capacity)
{
    const int length = request->content_len < (int)capacity - 1
                           ? request->content_len
                           : (int)capacity - 1;
    int received = 0;
    while (received < length) {
        const int got = httpd_req_recv(request, out + received, length - received);
        if (got <= 0) {
            return ESP_FAIL;
        }
        received += got;
    }
    out[received] = '\0';
    return ESP_OK;
}

static esp_err_t handle_setup_wifi(httpd_req_t *request)
{
    char body[512];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    char ssid[GC_MAX_SSID] = {0};
    char psk[GC_MAX_PSK] = {0};
    form_field(body, "ssid", ssid, sizeof(ssid));
    form_field(body, "psk", psk, sizeof(psk));

    if (ssid[0] == '\0') {
        httpd_resp_set_status(request, "303 See Other");
        httpd_resp_set_hdr(request, "Location", "/setup");
        return httpd_resp_send(request, NULL, 0);
    }

    /* Answered before the join, because joining takes the setup network
     * away and this page would otherwise never arrive. */
    send_page_head(request, "Joining");
    httpd_resp_send_chunk(request,
        "<h1>Joining</h1><p class=\"lede\">The display is trying that "
        "network now. This page will stop loading — look at the display: "
        "it shows its new address, or why it could not join.</p>",
        HTTPD_RESP_USE_STRLEN);
    send_page_end(request);

    ESP_LOGI(TAG, "joining '%s' from the setup page", ssid);
    gc_net_join(ssid, psk);
    return ESP_OK;
}

/* ---- settings ---- */

static esp_err_t handle_settings(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Settings");

    const gc_ota_state_t update = gc_ota_state();
    char clock[GC_MAX_TZ] = "UTC — not set";
    if (s_config->display.timezone[0] != '\0') {
        snprintf(clock, sizeof(clock), "%s", s_config->display.timezone);
    }

    char body[2560];
    /* A status report rather than a table of contents: every row leads
     * with what is true now, and only then opens the page that would
     * change it. */
    int length = snprintf(body, sizeof(body),
        "<h1>Settings</h1><p class=\"lede\">%s, %s.</p>"
        "<a class=\"row\" href=\"/settings/people\">People"
        "<span>%d configured</span></a>"
        "<a class=\"row\" href=\"/settings/pairing\">GlucoCore"
        "<span>%s</span></a>"
        "<a class=\"row\" href=\"/settings/ranges\">Ranges"
        "<span>%.0f&ndash;%.0f %s</span></a>"
        "<a class=\"row\" href=\"/settings/network\">Network"
        "<span>%s</span></a>"
        "<a class=\"row\" href=\"/settings/clock\">Clock"
        "<span>%s</span></a>"
        "<a class=\"row\" href=\"/settings/access\">Access"
        "<span>%s</span></a>"
        "<a class=\"row\" href=\"/settings/updates\">Updates"
        "<span>%s &middot; %s</span></a>"
        "<a class=\"row\" href=\"/log\">Sync log<span>&rsaquo;</span></a>"
        "<a class=\"row\" href=\"/\">Dashboard<span>&rsaquo;</span></a>",
        GC_BOARD_ID, gc_ota_current_version(),
        s_config->user_count,
        s_config->glucocore.device_token[0] == '\0'
            ? "not paired"
            : (gc_glucocore_online() ? "paired" : "paired &middot; unreachable"),
        (double)s_config->display.low, (double)s_config->display.high,
        s_config->display.mmol ? "mmol/L" : "mg/dL",
        gc_net_is_online() ? gc_net_ip() : "not connected",
        clock,
        s_config->admin_password[0] != '\0' ? "password" : "no password",
        update.current,
        update.available ? "update available" : "up to date");

    /* Anything that needs attention is one tappable line, at the top of
     * what is wrong rather than buried in the page that fixes it. */
    if (s_config->user_count == 0) {
        length += snprintf(body + length, sizeof(body) - length,
            "<div class=\"note\">This display is not showing anybody yet. "
            "Pair it with GlucoCore from <a href=\"/settings/pairing\">"
            "GlucoCore</a>, or add somebody by hand from "
            "<a href=\"/settings/people\">People</a>.</div>");
    }
    if (s_config->admin_password[0] == '\0'
        && !s_config->admin_password_off) {
        length += snprintf(body + length, sizeof(body) - length,
            "<div class=\"note\">Anyone on this network can open these "
            "settings. <a href=\"/settings/access\">Set a password</a>, or "
            "say that is deliberate.</div>");
    }
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_settings_pairing(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Pairing");
    char body[1536];
    const int length = snprintf(body, sizeof(body),
        "<h1>Pair with GlucoCore</h1>"
        "<p class=\"lede\">%s</p>"
        "<form method=\"post\" action=\"/settings/pairing\">"
        "<label for=\"code\">Pairing code</label>"
        "<input id=\"code\" name=\"code\" inputmode=\"numeric\" "
        "pattern=\"[0-9]*\" maxlength=\"6\" required>"
        "<button type=\"submit\">Pair this display</button></form>"
        "<div class=\"note\">In GlucoCore, open <b>Devices</b> and create a "
        "code: six digits, ten minutes, single use. Pairing decides who this "
        "display shows, what they are called and their ranges — all of it "
        "follows GlucoCore from then on.</div>"
        "%s",
        s_config->glucocore.device_token[0] != '\0'
            ? "This display is paired. Entering a new code re-pairs it."
            : "This display is not paired with anybody yet.",
        s_config->glucocore.device_token[0] != '\0'
            ? "<form method=\"post\" action=\"/settings/glucocore/unpair\" "
              "onsubmit=\"return confirm('Unpair this display?')\">"
              "<button type=\"submit\" style=\"background:var(--band);"
              "color:var(--fg)\">Unpair</button></form>"
              "<div class=\"note\">Unpairing keeps the people it was "
              "showing, with their names and ranges, and clears where their "
              "readings came from — so they can be pointed at Nightscout or "
              "Tidepool instead.</div>"
            : "");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_settings_pairing_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[256];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    char code[16] = {0};
    form_field(body, "code", code, sizeof(code));

    /* Claimed into a copy: what is live keeps running unless the new
     * settings both come back and validate. */
    gc_config_t candidate = *s_config;
    const esp_err_t err = gc_glucocore_claim(code, &candidate);

    send_page_head(request, "Pairing");
    if (err == ESP_OK && gc_config_save(&candidate) == ESP_OK) {
        httpd_resp_send_chunk(request,
            "<h1>Paired</h1><p class=\"lede\">The display is fetching "
            "readings now.</p><p><a class=\"row\" href=\"/\">Dashboard"
            "<span>&rsaquo;</span></a></p>", HTTPD_RESP_USE_STRLEN);
        send_page_end(request);
        if (s_on_change != NULL) {
            s_on_change(&candidate);
        }
        return ESP_OK;
    }
    httpd_resp_send_chunk(request,
        err == ESP_ERR_INVALID_ARG
            ? "<h1>That code did not work</h1><p class=\"lede\">Codes last ten "
              "minutes and can only be used once. Make a new one in GlucoCore "
              "and try again.</p><p><a class=\"row\" href=\"/settings/pairing\">"
              "Back<span>&rsaquo;</span></a></p>"
            : "<h1>Could not reach GlucoCore</h1><p class=\"lede\">The display "
              "is on a network but could not talk to GlucoCore. Try again in a "
              "moment.</p><p><a class=\"row\" href=\"/settings/pairing\">Back"
              "<span>&rsaquo;</span></a></p>",
        HTTPD_RESP_USE_STRLEN);
    return send_page_end(request);
}

static esp_err_t handle_settings_network(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    return handle_setup(request);
}

static esp_err_t handle_settings_ranges(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Ranges");
    char body[1024];
    const int length = snprintf(body, sizeof(body),
        "<h1>Ranges</h1>"
        "<p class=\"lede\">In mg/dL, whatever the display reads in.</p>"
        "<form method=\"post\" action=\"/settings/ranges\">"
        "<label for=\"low\">Low</label>"
        "<input id=\"low\" name=\"low\" type=\"number\" value=\"%.0f\">"
        "<label for=\"high\">High</label>"
        "<input id=\"high\" name=\"high\" type=\"number\" value=\"%.0f\">"
        "<label for=\"units\">Read in</label>"
        "<select id=\"units\" name=\"units\">"
        "<option value=\"mgdl\"%s>mg/dL</option>"
        "<option value=\"mmol\"%s>mmol/L</option></select>"
        "<button type=\"submit\">Save</button></form>",
        (double)s_config->display.low, (double)s_config->display.high,
        s_config->display.mmol ? "" : " selected",
        s_config->display.mmol ? " selected" : "");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_settings_ranges_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[256];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    gc_config_t candidate = *s_config;
    char value[32];
    if (form_field(body, "low", value, sizeof(value)) && value[0] != '\0') {
        candidate.display.low = strtof(value, NULL);
    }
    if (form_field(body, "high", value, sizeof(value)) && value[0] != '\0') {
        candidate.display.high = strtof(value, NULL);
    }
    if (form_field(body, "units", value, sizeof(value))) {
        candidate.display.mmol = strcmp(value, "mmol") == 0;
    }

    char reason[160];
    if (!gc_config_valid(&candidate, reason, sizeof(reason))
        || gc_config_save(&candidate) != ESP_OK) {
        send_page_head(request, "Ranges");
        char page[400];
        const int length = snprintf(page, sizeof(page),
            "<h1>Not saved</h1><p class=\"lede bad\">%s</p>"
            "<p><a class=\"row\" href=\"/settings/ranges\">Back"
            "<span>&rsaquo;</span></a></p>", reason);
        httpd_resp_send_chunk(request, page, length);
        return send_page_end(request);
    }

    httpd_resp_set_status(request, "303 See Other");
    httpd_resp_set_hdr(request, "Location", "/settings");
    httpd_resp_send(request, NULL, 0);
    if (s_on_change != NULL) {
        s_on_change(&candidate);
    }
    return ESP_OK;
}

static esp_err_t handle_settings_updates(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    const gc_ota_state_t update = gc_ota_state();
    send_page_head(request, "Updates");
    char body[1024];
    const int length = snprintf(body, sizeof(body),
        "<h1>Updates</h1><p class=\"lede\">Running %s on the %s channel.</p>"
        "%s"
        "<form method=\"post\" action=\"/update/check\">"
        "<button type=\"submit\" style=\"background:var(--band);"
        "color:var(--fg)\">Check now</button></form>"
        "<form method=\"post\" action=\"/settings/updates/channel\">"
        "<label for=\"channel\">Which releases this display follows</label>"
        "<select id=\"channel\" name=\"channel\">"
        "<option value=\"stable\"%s>Standard &mdash; full releases only</option>"
        "<option value=\"beta\"%s>Beta &mdash; pre-releases as well</option>"
        "</select><button type=\"submit\">Change channel</button></form>"
        "<div class=\"note\">Installing writes the spare half of the flash "
        "and restarts into it. If the new firmware cannot get onto the "
        "network and draw a frame, the device puts this one back by "
        "itself.<br><br>Changing the channel takes effect at the next check: "
        "leaving Beta steps back onto the last full release, which is the "
        "point of it.</div>",
        update.current, gc_channel_label(s_config->update_channel),
        update.available
            ? "<form method=\"post\" action=\"/settings/updates\">"
              "<button type=\"submit\">Install the update</button></form>"
            : "<p>This display is up to date.</p>",
        s_config->update_channel == GC_CHANNEL_STABLE ? " selected" : "",
        s_config->update_channel == GC_CHANNEL_BETA ? " selected" : "");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_settings_updates_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    const gc_ota_state_t update = gc_ota_state();
    if (!update.available) {
        httpd_resp_set_status(request, "303 See Other");
        httpd_resp_set_hdr(request, "Location", "/settings/updates");
        return httpd_resp_send(request, NULL, 0);
    }
    send_page_head(request, "Installing");
    httpd_resp_send_chunk(request,
        "<h1>Installing</h1><p class=\"lede\">The display restarts when it is "
        "done, in a minute or so.</p>", HTTPD_RESP_USE_STRLEN);
    send_page_end(request);
    gc_ota_install(&update);   /* does not return when it works */
    return ESP_OK;
}

/* ---- the sync log ---- */

/* What every source has been doing. On the Pi this is how somebody finds
 * out that a Nightscout secret stopped working without opening a terminal,
 * and it is the same here — more so, because there is no terminal. */
static esp_err_t handle_log(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Sync log");
    httpd_resp_send_chunk(request,
        "<h1>Sync log</h1><p class=\"lede\">Newest first. Cleared by a "
        "restart.</p><div id=\"rows\"></div>"
        "<p><a class=\"row\" href=\"/settings\">Settings"
        "<span>&rsaquo;</span></a></p>"
        "<script>"
        "async function tick(){"
        "const r=await fetch('/api/log.json',{cache:'no-store'});"
        "if(!r.ok)return;const d=await r.json();"
        "document.getElementById('rows').innerHTML=d.entries.length?"
        "d.entries.map(e=>{"
        "const t=e.ms?new Date(e.ms).toLocaleTimeString():'--';"
        "return `<div class=\"row\"><span style=\"text-align:left\">"
        "${e.ok?'':'&#9888; '}<b>${e.user}</b> &middot; ${e.source}<br>"
        "${e.message}</span><span>${t}</span></div>`;}).join('')"
        ":'<p class=\"note\">Nothing yet. A source logs here the first "
        "time it fetches, or the first time it cannot.</p>';}"
        "tick();setInterval(tick,10000);"
        "</script>", HTTPD_RESP_USE_STRLEN);
    return send_page_end(request);
}

static esp_err_t handle_log_json(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    static gc_synclog_entry_t entries[GC_SYNCLOG_ENTRIES];
    const int count = gc_synclog_recent(entries, GC_SYNCLOG_ENTRIES);

    cJSON *root = cJSON_CreateObject();
    cJSON *list = cJSON_AddArrayToObject(root, "entries");
    for (int i = 0; i < count; i++) {
        cJSON *item = cJSON_CreateObject();
        cJSON_AddNumberToObject(item, "ms", (double)entries[i].ms);
        cJSON_AddStringToObject(item, "source", entries[i].source);
        cJSON_AddStringToObject(item, "user", entries[i].user);
        cJSON_AddStringToObject(item, "message", entries[i].message);
        cJSON_AddBoolToObject(item, "ok", entries[i].ok);
        cJSON_AddItemToArray(list, item);
    }
    return send_json(request, root);
}

/* ---- the clock ---- */

static esp_err_t handle_clock(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Clock");

    char now[64] = "not set yet";
    if (gc_net_time_is_set()) {
        const time_t when = (time_t)(now_ms() / 1000);
        struct tm tm_now;
        localtime_r(&when, &tm_now);
        strftime(now, sizeof(now), "%a %d %b %H:%M", &tm_now);
    }
    char body[1024];
    int length = snprintf(body, sizeof(body),
        "<h1>Clock</h1>"
        "<p class=\"lede\">The display reads %s. Your phone says "
        "<span id=\"phone\">&hellip;</span>.</p>"
        "<form method=\"post\" action=\"/settings/clock\">"
        "<label for=\"tz\">Time zone</label>"
        "<input list=\"zones\" id=\"tz\" name=\"timezone\" value=\"%s\" "
        "autocapitalize=\"off\" autocorrect=\"off\" "
        "placeholder=\"Europe/London\"><datalist id=\"zones\">",
        now, s_config->display.timezone);
    for (int i = 0; length < (int)sizeof(body) - 96; i++) {
        const char *zone = gc_net_zone_name(i);
        if (zone == NULL) {
            break;
        }
        length += snprintf(body + length, sizeof(body) - length,
                           "<option value=\"%s\">", zone);
    }
    length += snprintf(body + length, sizeof(body) - length,
        "</datalist>"
        "<label for=\"fmt\">Show the time as</label>"
        "<select id=\"fmt\" name=\"time_format\">"
        "<option value=\"24\"%s>15:04</option>"
        "<option value=\"12\"%s>3:04 pm</option></select>"
        "<button type=\"submit\">Save</button></form>"
        "<div class=\"note\">A zone this firmware does not know falls back "
        "to UTC rather than to a wrong offset. If yours is missing you can "
        "type a POSIX rule instead &mdash; "
        "<code>AEST-10AEDT,M10.1.0,M4.1.0/3</code> &mdash; which is passed "
        "through untouched.</div>"
        "<script>document.getElementById('phone').textContent="
        "Intl.DateTimeFormat().resolvedOptions().timeZone;</script>",
        s_config->display.time_format == 12 ? "" : " selected",
        s_config->display.time_format == 12 ? " selected" : "");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

/* Every save takes the same shape: copy, edit the copy, validate, write,
 * hand it to the caller. What is running now keeps running unless all of
 * that succeeds. */
static esp_err_t save_and_redirect(httpd_req_t *request,
                                   const gc_config_t *candidate,
                                   const char *back)
{
    char reason[160];
    if (!gc_config_valid(candidate, reason, sizeof(reason))
        || gc_config_save(candidate) != ESP_OK) {
        send_page_head(request, "Not saved");
        char page[512];
        const int length = snprintf(page, sizeof(page),
            "<h1>Not saved</h1><p class=\"lede bad\">%s</p>"
            "<p><a class=\"row\" href=\"%s\">Back<span>&rsaquo;</span></a></p>",
            reason, back);
        httpd_resp_send_chunk(request, page, length);
        return send_page_end(request);
    }
    httpd_resp_set_status(request, "303 See Other");
    httpd_resp_set_hdr(request, "Location", "/settings");
    httpd_resp_send(request, NULL, 0);
    if (s_on_change != NULL) {
        s_on_change(candidate);
    }
    return ESP_OK;
}

static esp_err_t handle_clock_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[256];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    gc_config_t candidate = *s_config;
    char value[GC_MAX_TZ];
    if (form_field(body, "timezone", value, sizeof(value))) {
        snprintf(candidate.display.timezone,
                 sizeof(candidate.display.timezone), "%s", value);
    }
    if (form_field(body, "time_format", value, sizeof(value))) {
        candidate.display.time_format = (atoi(value) == 12) ? 12 : 24;
    }
    return save_and_redirect(request, &candidate, "/settings/clock");
}

/* ---- access ---- */

static esp_err_t handle_access(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "Access");
    char body[1280];
    const int length = snprintf(body, sizeof(body),
        "<h1>Access</h1><p class=\"lede\">%s</p>"
        "<form method=\"post\" action=\"/settings/access\">"
        "<label for=\"pw\">Password</label>"
        "<input id=\"pw\" name=\"password\" type=\"password\" "
        "autocomplete=\"new-password\" placeholder=\"leave blank for none\">"
        "<label><input type=\"checkbox\" name=\"off\" value=\"1\"%s "
        "style=\"width:auto;margin-right:.5rem\">No password on purpose"
        "</label>"
        "<button type=\"submit\">Save</button></form>"
        "<div class=\"note\">On a home network you trust, the display is "
        "only reachable from that network, and no password means nothing to "
        "look up on a phone. On a network guests, flatmates or an office "
        "share, keep one. Ticking the box stops this page asking.</div>",
        s_config->admin_password[0] != '\0'
            ? "This display asks for a password."
            : (s_config->admin_password_off
                   ? "No password, on purpose."
                   : "No password yet &mdash; anyone on this network can "
                     "open the settings."),
        s_config->admin_password_off ? " checked" : "");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_access_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[512];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    gc_config_t candidate = *s_config;
    char value[GC_MAX_SECRET];
    if (form_field(body, "password", value, sizeof(value))) {
        snprintf(candidate.admin_password, sizeof(candidate.admin_password),
                 "%s", value);
    }
    /* An unticked checkbox is simply absent from the form. */
    candidate.admin_password_off =
        form_field(body, "off", value, sizeof(value));
    if (candidate.admin_password[0] != '\0') {
        candidate.admin_password_off = false;
    }
    return save_and_redirect(request, &candidate, "/settings/access");
}

/* ---- people ---- */

static const char *SOURCE_CHOICES[] = {"glucocore", "nightscout", "tidepool"};

static esp_err_t handle_people(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    send_page_head(request, "People");
    char body[1536];
    int length = snprintf(body, sizeof(body),
        "<h1>People</h1><p class=\"lede\">Who this display shows.</p>");

    const int64_t now = now_ms();
    for (int i = 0; i < s_config->user_count
                    && length < (int)sizeof(body) - 200; i++) {
        gc_snapshot_t snap;
        char when[24] = "no readings yet";
        if (gc_store_snapshot(s_store, i, now, &snap) && snap.has_sgv) {
            const long minutes = (long)((now - snap.sgv_date) / 60000);
            snprintf(when, sizeof(when), "%ldm ago", minutes);
        }
        length += snprintf(body + length, sizeof(body) - length,
            "<a class=\"row\" href=\"/settings/person?i=%d\">%s"
            "<span>%s &middot; %s</span></a>",
            i, s_config->users[i].name,
            gc_source_label(gc_source_kind_name(s_config->users[i].kind)), when);
    }
    if (s_config->user_count < GC_MAX_USERS) {
        length += snprintf(body + length, sizeof(body) - length,
            "<a class=\"row\" href=\"/settings/person?i=%d\">Add somebody"
            "<span>+</span></a>", s_config->user_count);
    }
    length += snprintf(body + length, sizeof(body) - length,
        "<form method=\"post\" action=\"/settings/people\">"
        "<button type=\"submit\">Fetch now</button></form>");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

static esp_err_t handle_people_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    /* "Fetch now": every poller wakes rather than waiting out its interval.
     * Nothing is saved, so there is nothing to validate. */
    gc_sources_poll_now();
    gc_synclog_add("system", "system", true, "fetch requested from settings");
    httpd_resp_set_status(request, "303 See Other");
    httpd_resp_set_hdr(request, "Location", "/log");
    return httpd_resp_send(request, NULL, 0);
}

static int query_index(httpd_req_t *request)
{
    char query[96];
    if (httpd_req_get_url_query_str(request, query, sizeof(query)) != ESP_OK) {
        return 0;
    }
    char value[8];
    if (httpd_query_key_value(query, "i", value, sizeof(value)) != ESP_OK) {
        return 0;
    }
    const int index = atoi(value);
    return (index >= 0 && index < GC_MAX_USERS) ? index : 0;
}

static esp_err_t handle_person(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    const int index = query_index(request);
    const bool existing = index < s_config->user_count;
    const gc_user_config_t blank = {.kind = GC_SOURCE_NIGHTSCOUT,
                                    .poll_seconds = 60};
    const gc_user_config_t *user = existing ? &s_config->users[index] : &blank;

    send_page_head(request, existing ? "Person" : "Add somebody");
    char body[2560];
    int length = snprintf(body, sizeof(body),
        "<h1>%s</h1>"
        "<form method=\"post\" action=\"/settings/person?i=%d\">"
        "<label for=\"name\">Name on screen</label>"
        "<input id=\"name\" name=\"name\" value=\"%s\" required "
        "maxlength=\"40\">"
        "<label for=\"kind\">Where the readings come from</label>"
        "<select id=\"kind\" name=\"kind\" onchange=\"pick()\">",
        existing ? user->name : "Add somebody", index,
        existing ? user->name : "");
    for (size_t i = 0; i < 3; i++) {
        length += snprintf(body + length, sizeof(body) - length,
            "<option value=\"%s\"%s>%s</option>", SOURCE_CHOICES[i],
            user->kind == gc_source_kind_from_name(SOURCE_CHOICES[i])
                ? " selected" : "",
            gc_source_label(SOURCE_CHOICES[i]));
    }
    length += snprintf(body + length, sizeof(body) - length,
        "</select>"
        "<div id=\"glucocore\"><label for=\"patient\">Patient id</label>"
        "<input id=\"patient\" name=\"patient_id\" value=\"%s\">"
        "<div class=\"note\">Set by pairing. Change it only if GlucoCore "
        "told you to.</div></div>"
        "<div id=\"nightscout\"><label for=\"url\">Site address</label>"
        "<input id=\"url\" name=\"url\" value=\"%s\" "
        "placeholder=\"https://mysite.example.com\" autocapitalize=\"off\">"
        "<label for=\"secret\">API secret or access token</label>"
        "<input id=\"secret\" name=\"api_secret\" value=\"%s\" "
        "autocapitalize=\"off\"></div>"
        "<div id=\"tidepool\"><label for=\"email\">Tidepool email</label>"
        "<input id=\"email\" name=\"email\" type=\"email\" value=\"%s\" "
        "autocapitalize=\"off\">"
        "<label for=\"password\">Tidepool password</label>"
        "<input id=\"password\" name=\"password\" type=\"password\" "
        "value=\"%s\"></div>"
        "<p><button type=\"button\" onclick=\"test()\" "
        "style=\"background:var(--band);color:var(--fg)\">Test the "
        "connection</button></p><p id=\"verdict\"></p>"
        "<button type=\"submit\">Save</button></form>",
        user->patient_id, user->url, user->api_secret, user->email,
        user->password);

    if (existing && s_config->user_count > 1) {
        length += snprintf(body + length, sizeof(body) - length,
            "<form method=\"post\" action=\"/settings/person/remove?i=%d\" "
            "onsubmit=\"return confirm('Take %s off this display?')\">"
            "<button type=\"submit\" style=\"background:var(--band);"
            "color:var(--fg)\">Remove %s</button></form>",
            index, user->name, user->name);
    }
    length += snprintf(body + length, sizeof(body) - length,
        "<script>"
        "function pick(){const k=document.getElementById('kind').value;"
        "for(const id of ['glucocore','nightscout','tidepool'])"
        "document.getElementById(id).style.display=(id===k)?'':'none';}"
        "pick();"
        "async function test(){"
        "const v=document.getElementById('verdict');v.textContent='Testing…';"
        "const f=new FormData(document.forms[0]);"
        "const r=await fetch('/api/source/test',{method:'POST',"
        "body:new URLSearchParams(f)});"
        "const d=await r.json();"
        "v.textContent=d.detail;v.className=d.ok?'':'bad';}"
        "</script>");
    httpd_resp_send_chunk(request, body, length);
    return send_page_end(request);
}

/* Each field is read straight into the struct member it belongs to, at
 * that member's own size, so a long paste is truncated where it is stored
 * rather than in a staging buffer on the way. */
static void read_person_form(const char *body, gc_user_config_t *user)
{
    char kind[24];
    form_field(body, "name", user->name, sizeof(user->name));
    if (form_field(body, "kind", kind, sizeof(kind))) {
        user->kind = gc_source_kind_from_name(kind);
    }
    form_field(body, "patient_id", user->patient_id, sizeof(user->patient_id));
    form_field(body, "url", user->url, sizeof(user->url));
    form_field(body, "api_secret", user->api_secret, sizeof(user->api_secret));
    form_field(body, "email", user->email, sizeof(user->email));
    form_field(body, "password", user->password, sizeof(user->password));
    if (user->poll_seconds <= 0) {
        user->poll_seconds = 60;
    }
}

static esp_err_t handle_person_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    const int index = query_index(request);
    char body[1024];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    gc_config_t candidate = *s_config;
    if (index >= candidate.user_count) {
        if (candidate.user_count >= GC_MAX_USERS) {
            httpd_resp_set_status(request, "303 See Other");
            httpd_resp_set_hdr(request, "Location", "/settings/people");
            return httpd_resp_send(request, NULL, 0);
        }
        memset(&candidate.users[candidate.user_count], 0,
               sizeof(gc_user_config_t));
        candidate.user_count++;
    }
    read_person_form(body, &candidate.users[index]);

    char back[48];
    snprintf(back, sizeof(back), "/settings/person?i=%d", index);
    return save_and_redirect(request, &candidate, back);
}

static esp_err_t handle_person_remove(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    const int index = query_index(request);
    gc_config_t candidate = *s_config;
    if (index >= candidate.user_count || candidate.user_count <= 1) {
        httpd_resp_set_status(request, "303 See Other");
        httpd_resp_set_hdr(request, "Location", "/settings/people");
        return httpd_resp_send(request, NULL, 0);
    }
    /* Closed up rather than blanked: the panels are drawn from the first
     * user_count entries, and a hole would draw an empty one. */
    for (int i = index; i + 1 < candidate.user_count; i++) {
        candidate.users[i] = candidate.users[i + 1];
    }
    candidate.user_count--;
    memset(&candidate.users[candidate.user_count], 0, sizeof(gc_user_config_t));
    return save_and_redirect(request, &candidate, "/settings/people");
}

/* Tries the credentials on the form without storing them — verify.py's
 * job, and the reason a wrong password is a sentence rather than a display
 * that quietly shows nothing. */
static esp_err_t handle_source_test(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[1024];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "{}", 2);
    }
    char kind[24] = {0};
    form_field(body, "kind", kind, sizeof(kind));

    gc_verify_result_t verdict;
    if (strcmp(kind, "nightscout") == 0) {
        char url[GC_MAX_URL] = {0}, secret[GC_MAX_SECRET] = {0};
        form_field(body, "url", url, sizeof(url));
        form_field(body, "api_secret", secret, sizeof(secret));
        verdict = gc_verify_nightscout(url, secret);
    } else if (strcmp(kind, "tidepool") == 0) {
        char email[GC_MAX_EMAIL] = {0}, password[GC_MAX_SECRET] = {0};
        form_field(body, "email", email, sizeof(email));
        form_field(body, "password", password, sizeof(password));
        verdict = gc_verify_tidepool(email, password);
    } else {
        verdict.ok = s_config->glucocore.device_token[0] != '\0';
        snprintf(verdict.detail, sizeof(verdict.detail), "%s",
                 verdict.ok ? "Paired with GlucoCore."
                            : "This display is not paired with GlucoCore yet.");
    }
    cJSON *json = cJSON_CreateObject();
    cJSON_AddBoolToObject(json, "ok", verdict.ok);
    cJSON_AddStringToObject(json, "detail", verdict.detail);
    return send_json(request, json);
}

/* ---- unpairing, channels, updates, Wi-Fi ---- */

static esp_err_t handle_unpair(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    gc_config_t candidate = *s_config;
    memset(&candidate.glucocore, 0, sizeof(candidate.glucocore));
    /* The people GlucoCore was feeding stay, with their source cleared:
     * throwing them away would lose their names and ranges too, and the
     * person unpairing may be about to point them at Nightscout. */
    for (int i = 0; i < candidate.user_count; i++) {
        if (candidate.users[i].kind == GC_SOURCE_GLUCOCORE) {
            candidate.users[i].kind = GC_SOURCE_NONE;
            candidate.users[i].patient_id[0] = '\0';
        }
    }
    gc_synclog_add("glucocore", "system", true, "unpaired from GlucoCore");
    return save_and_redirect(request, &candidate, "/settings/pairing");
}

static esp_err_t handle_channel_post(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    char body[128];
    if (read_body(request, body, sizeof(body)) != ESP_OK) {
        httpd_resp_set_status(request, "400 Bad Request");
        return httpd_resp_send(request, "Could not read that form.", -1);
    }
    char value[16];
    gc_config_t candidate = *s_config;
    if (form_field(body, "channel", value, sizeof(value))) {
        candidate.update_channel = gc_channel_from_name(value);
    }
    return save_and_redirect(request, &candidate, "/settings/updates");
}

static esp_err_t handle_update_check(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    gc_ota_state_t found;
    if (gc_ota_check(s_config->update_channel, &found) == ESP_OK) {
        gc_synclog_add("updates", "system", true,
                       found.available ? "%s is available" : "up to date (%s)",
                       found.available ? found.latest : found.current);
    } else {
        gc_synclog_add("updates", "system", false, "could not reach GitHub");
    }
    httpd_resp_set_status(request, "303 See Other");
    httpd_resp_set_hdr(request, "Location", "/settings/updates");
    return httpd_resp_send(request, NULL, 0);
}

static esp_err_t handle_wifi_json(httpd_req_t *request)
{
    if (!authorized(request)) {
        return deny(request);
    }
    gc_scan_result_t seen[GC_MAX_SCAN_RESULTS];
    const int found = gc_net_scan(seen, GC_MAX_SCAN_RESULTS);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddBoolToObject(root, "online", gc_net_is_online());
    cJSON_AddStringToObject(root, "ip", gc_net_ip());
    cJSON_AddStringToObject(root, "error", gc_net_last_error());
    cJSON *list = cJSON_AddArrayToObject(root, "networks");
    for (int i = 0; i < found; i++) {
        cJSON *item = cJSON_CreateObject();
        cJSON_AddStringToObject(item, "ssid", seen[i].ssid);
        cJSON_AddNumberToObject(item, "rssi", seen[i].rssi);
        cJSON_AddBoolToObject(item, "secured", seen[i].secured);
        cJSON_AddItemToArray(list, item);
    }
    return send_json(request, root);
}

/* ---- the captive portal ---- */

/* A phone that has just joined a network fetches a known URL to find out
 * whether it really reaches the internet. Answering that with a redirect is
 * what makes the "sign in to network" sheet appear — and it is the whole
 * reason setup opens by itself instead of needing a second QR code. */
static esp_err_t handle_probe(httpd_req_t *request)
{
    if (!s_captive) {
        httpd_resp_set_status(request, "404 Not Found");
        return httpd_resp_send(request, "Not found", -1);
    }
    char location[64];
    snprintf(location, sizeof(location), "http://%s/setup", GC_HOTSPOT_ADDR);
    httpd_resp_set_status(request, "302 Found");
    httpd_resp_set_hdr(request, "Location", location);
    return httpd_resp_send(request, NULL, 0);
}

static esp_err_t handle_not_found(httpd_req_t *request, httpd_err_code_t error)
{
    (void)error;
    if (s_captive) {
        /* While the hotspot is up, everything a phone asks for is the
         * portal — including the probe paths the contract lists, which
         * arrive here rather than at a registered handler. */
        return handle_probe(request);
    }
    httpd_resp_set_status(request, "404 Not Found");
    httpd_resp_set_type(request, "text/html; charset=utf-8");
    return httpd_resp_send(request,
        "<!doctype html><meta charset=utf-8><h1>Not found</h1>"
        "<p><a href=\"/settings\">Back to settings</a></p>",
        HTTPD_RESP_USE_STRLEN);
}

/* ------------------------------------------------------------ lifetime -- */

static const httpd_uri_t ROUTES[] = {
    {"/", HTTP_GET, handle_dashboard, NULL},
    {"/api/dashboard.json", HTTP_GET, handle_dashboard_json, NULL},
    {"/api/health.json", HTTP_GET, handle_health_json, NULL},
    {"/screen.png", HTTP_GET, handle_screen_png, NULL},
    {"/setup", HTTP_GET, handle_setup, NULL},
    {"/setup/wifi", HTTP_POST, handle_setup_wifi, NULL},
    {"/settings", HTTP_GET, handle_settings, NULL},
    {"/settings/network", HTTP_GET, handle_settings_network, NULL},
    {"/settings/ranges", HTTP_GET, handle_settings_ranges, NULL},
    {"/settings/ranges", HTTP_POST, handle_settings_ranges_post, NULL},
    {"/settings/pairing", HTTP_GET, handle_settings_pairing, NULL},
    {"/settings/pairing", HTTP_POST, handle_settings_pairing_post, NULL},
    {"/settings/updates", HTTP_GET, handle_settings_updates, NULL},
    {"/settings/updates", HTTP_POST, handle_settings_updates_post, NULL},
    {"/settings/updates/channel", HTTP_POST, handle_channel_post, NULL},
    {"/settings/clock", HTTP_GET, handle_clock, NULL},
    {"/settings/clock", HTTP_POST, handle_clock_post, NULL},
    {"/settings/access", HTTP_GET, handle_access, NULL},
    {"/settings/access", HTTP_POST, handle_access_post, NULL},
    {"/settings/people", HTTP_GET, handle_people, NULL},
    {"/settings/people", HTTP_POST, handle_people_post, NULL},
    {"/settings/person", HTTP_GET, handle_person, NULL},
    {"/settings/person", HTTP_POST, handle_person_post, NULL},
    {"/settings/person/remove", HTTP_POST, handle_person_remove, NULL},
    {"/settings/glucocore/unpair", HTTP_POST, handle_unpair, NULL},
    {"/api/source/test", HTTP_POST, handle_source_test, NULL},
    {"/api/wifi.json", HTTP_GET, handle_wifi_json, NULL},
    {"/api/log.json", HTTP_GET, handle_log_json, NULL},
    {"/log", HTTP_GET, handle_log, NULL},
    {"/update/check", HTTP_POST, handle_update_check, NULL},
};

esp_err_t gc_httpd_start(gc_config_t *config, gc_store_t *store,
                         gc_config_changed_cb on_change)
{
    if (config == NULL || store == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s_config = config;
    s_store = store;
    s_on_change = on_change;
    ensure_key();

    httpd_config_t settings = HTTPD_DEFAULT_CONFIG();
    settings.server_port = GC_HTTPD_PORT;
    settings.max_uri_handlers = sizeof(ROUTES) / sizeof(ROUTES[0])
                                + GC_CAPTIVE_PROBE_COUNT + 2;
    settings.stack_size = 8192;
    settings.lru_purge_enable = true;
    settings.uri_match_fn = httpd_uri_match_wildcard;

    esp_err_t err = httpd_start(&s_server, &settings);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "the web app could not take port %d: %s", GC_HTTPD_PORT,
                 esp_err_to_name(err));
        return err;
    }
    for (size_t i = 0; i < sizeof(ROUTES) / sizeof(ROUTES[0]); i++) {
        httpd_register_uri_handler(s_server, &ROUTES[i]);
    }
    for (int i = 0; i < GC_CAPTIVE_PROBE_COUNT; i++) {
        const httpd_uri_t probe = {
            .uri = gc_captive_probe_paths[i],
            .method = HTTP_GET,
            .handler = handle_probe,
        };
        httpd_register_uri_handler(s_server, &probe);
    }
    httpd_register_err_handler(s_server, HTTPD_404_NOT_FOUND, handle_not_found);

    ESP_LOGI(TAG, "web app listening on port %d", GC_HTTPD_PORT);
    return ESP_OK;
}

void gc_httpd_stop(void)
{
    if (s_server != NULL) {
        httpd_stop(s_server);
        s_server = NULL;
    }
}

void gc_httpd_set_captive(bool captive)
{
    s_captive = captive;
}
