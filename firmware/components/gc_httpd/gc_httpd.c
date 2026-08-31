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

#include "cJSON.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_random.h"
#include "mbedtls/base64.h"

#include "gc_net.h"
#include "gc_ota.h"
#include "gc_predict.h"
#include "gc_sources.h"
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
    char body[2048];
    int length = snprintf(body, sizeof(body),
        "<h1>Settings</h1><p class=\"lede\">%s, %s.</p>"
        "<a class=\"row\" href=\"/settings/network\">Network"
        "<span>%s</span></a>"
        "<a class=\"row\" href=\"/settings/ranges\">Ranges"
        "<span>%.0f&ndash;%.0f %s</span></a>"
        "<a class=\"row\" href=\"/settings/updates\">Updates"
        "<span>%s%s</span></a>"
        "<a class=\"row\" href=\"/\">Dashboard<span>&rsaquo;</span></a>",
        GC_BOARD_ID, gc_ota_current_version(),
        gc_net_is_online() ? gc_net_ip() : "not connected",
        (double)s_config->display.low, (double)s_config->display.high,
        s_config->display.mmol ? "mmol/L" : "mg/dL",
        update.current,
        update.available ? " &middot; update available" : " &middot; up to date");

    if (s_config->user_count == 0) {
        length += snprintf(body + length, sizeof(body) - length,
            "<div class=\"note\">This display is not showing anybody yet. "
            "Pair it with GlucoCore from <a href=\"/settings/pairing\">"
            "Pairing</a>.</div>");
    } else {
        for (int i = 0; i < s_config->user_count
                        && length < (int)sizeof(body) - 160; i++) {
            length += snprintf(body + length, sizeof(body) - length,
                "<a class=\"row\" href=\"/settings/pairing\">%s<span>%s</span></a>",
                s_config->users[i].name,
                gc_source_label(gc_source_kind_name(s_config->users[i].kind)));
        }
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
        "follows GlucoCore from then on.</div>",
        s_config->glucocore.device_token[0] != '\0'
            ? "This display is paired. Entering a new code re-pairs it."
            : "This display is not paired with anybody yet.");
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
        "<div class=\"note\">Installing writes the spare half of the flash "
        "and restarts into it. If the new firmware cannot get onto the "
        "network and draw a frame, the device puts this one back by "
        "itself.</div>",
        update.current, gc_channel_label(s_config->update_channel),
        update.available
            ? "<form method=\"post\" action=\"/settings/updates\">"
              "<button type=\"submit\">Install the update</button></form>"
            : "<p>This display is up to date.</p>");
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
