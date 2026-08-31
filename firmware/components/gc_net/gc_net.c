/*
 * Wi-Fi, the setup hotspot and the clock.
 *
 * glucocube/network.py drives NetworkManager through nmcli; this drives
 * esp_wifi directly. The policy above the radio is the same one — the
 * watcher at the bottom of this file is NetworkWatcher, constant for
 * constant — but the machinery underneath is not, and the differences are
 * called out where they happen. The big ones:
 *
 *   - The Pi cannot be a station and an access point at once, so nmcli's
 *     hotspot takes the network away. The S3 can, and does: a join runs
 *     with the setup hotspot still up, so the phone that asked for it is
 *     still there to be told how it went.
 *   - There is no NetworkManager to remember networks for us, so the
 *     credentials are ours to keep, and are written only once a join has
 *     actually worked (network.py saves them the same way, by only ever
 *     creating a profile that connected).
 *   - "Connectivity" here means "the station holds a DHCP lease". The Pi
 *     asks NetworkManager, which distinguishes 'limited' from 'none' and
 *     leaves LAN-only setups alone; a device with an address but no route
 *     to the internet is a case the panel reports through the sources'
 *     own errors rather than by tearing the network down for a hotspot.
 */

#include "gc_net.h"

#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include "dhcpserver/dhcpserver.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "esp_heap_caps.h"

#include "gc_contract.h"

/* mDNS is not part of ESP-IDF — it arrives as the espressif/mdns managed
 * component. The firmware is perfectly usable without it (the device is
 * then reached by the IP the panel shows), so it is optional here and the
 * CMakeLists only requires it when it is actually in the build. */
#if __has_include("mdns.h")
#include "mdns.h"
#define GC_HAVE_MDNS 1
#endif

static const char *TAG = "gc_net";

/* The wizard has a person watching a spinner; twenty seconds is about as
 * long as that is worth before saying something. A join that is merely
 * slow still completes in the background and the panel catches up. */
#define GC_JOIN_TIMEOUT_MS 20000

/* How long the radio waits before trying the saved network again. Short
 * enough that a router reboot is invisible, long enough that a network
 * which is simply gone does not keep the CPU busy. */
#define GC_RECONNECT_MS 5000

/* After a join succeeds from a phone on the setup hotspot, the AP has to
 * stay up long enough for that phone to receive the page saying so. */
#define GC_HOTSPOT_GRACE_MS 30000

/* network.py's _quiet_until, in both its sizes: the watcher stays off the
 * radio for the length of a join, and for a breath afterwards so the
 * caller can settle the hotspot without racing it. */
#define GC_QUIET_JOIN_US (240 * 1000000LL)
#define GC_QUIET_AFTER_US (20 * 1000000LL)

#define GC_SNTP_WAIT_MS 10000

/* Anything before this is a clock that has never been set. Chosen well
 * after the first firmware was built, so it cannot be satisfied by a
 * plausible-looking RTC value that is really a boot counter. */
#define GC_CLOCK_SANE_EPOCH 1735689600  /* 2025-01-01 UTC */

/* Radios in a block of flats routinely see more than the panel will ever
 * list; scan wide, then cut down to the strongest after de-duplication. */
#define GC_SCAN_MAX_RECORDS 40

#define GC_NVS_NAMESPACE "glucocube"
#define GC_NVS_KEY_HOTSPOT_PW "hotspot_pw"
#define GC_NVS_KEY_SSID "wifi_ssid"
#define GC_NVS_KEY_PSK "wifi_psk"

#define GC_BIT_CONNECTED BIT0
#define GC_BIT_FAILED BIT1

static bool s_started;
static esp_netif_t *s_sta_netif;
static esp_netif_t *s_ap_netif;
static EventGroupHandle_t s_events;
static SemaphoreHandle_t s_lock;
static esp_timer_handle_t s_reconnect_timer;
static esp_timer_handle_t s_hotspot_off_timer;
static TaskHandle_t s_watch_task;

static gc_net_state_t s_state = GC_NET_DOWN;
static bool s_hotspot_up;
static bool s_joining;      /* a join owns the radio; watcher hands off */
static bool s_join_waiting; /* ...and is armed on the event group */
static bool s_scan_busy;
static bool s_sntp_ready;
static int64_t s_quiet_until_us;
static int64_t s_scan_at_us;
static int s_fails;

static char s_ip[16];
static char s_ssid[GC_MAX_SSID];
static char s_psk[GC_MAX_PSK];
static char s_hotspot_pw[16];

/* The reason phrases are string literals, so the pointer the UI reads is
 * always a whole message however badly the two threads interleave. Only
 * the unrecognised case needs formatting, and it formats into its own
 * buffer before the pointer moves. */
static const char *s_error = "";
static char s_error_buf[80];

static gc_scan_result_t s_scan[GC_MAX_SCAN_RESULTS];
static int s_scan_count;

static void refresh_scan(void);

/* ------------------------------------------------------------ helpers -- */

static void copy_str(char *dst, size_t capacity, const char *src)
{
    if (src == NULL) {
        dst[0] = '\0';
        return;
    }
    snprintf(dst, capacity, "%s", src);
}

static void lock(void)
{
    if (s_lock != NULL) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
    }
}

static void unlock(void)
{
    if (s_lock != NULL) {
        xSemaphoreGive(s_lock);
    }
}

static int64_t now_us(void)
{
    return esp_timer_get_time();
}

/* ------------------------------------------------------- what went wrong -- */

/* network.py's friendly_error(), against ESP-IDF's disconnect reasons
 * rather than nmcli's strings. Same job: whatever the radio says, the
 * person reads one short line telling them what to change. Wordings are
 * the Python's where the Python has one, so a household with a Pi and a
 * cube gets the same sentence off both screens. */
static const char *friendly_reason(uint8_t reason)
{
    switch (reason) {
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_MIC_FAILURE:
    case WIFI_REASON_INVALID_PMKID:
    case WIFI_REASON_GROUP_KEY_UPDATE_TIMEOUT:
        return "wrong Wi-Fi password";
    case WIFI_REASON_NO_AP_FOUND:
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD:
    case WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD:
        /* The Pi's version of this line also suggests ticking "hidden
         * network", because nmcli refuses to join anything it cannot see.
         * esp_wifi probes for the name it is given, so a hidden network
         * needs no ticking here and the advice would only confuse. */
        return "network not found — check the name, or move the device closer";
    case WIFI_REASON_ASSOC_LEAVE:
    case WIFI_REASON_AUTH_LEAVE:
    case WIFI_REASON_STA_LEAVING:
    case WIFI_REASON_AP_INITIATED:
    case WIFI_REASON_PEER_INITIATED:
        return "the network dropped us";
    case WIFI_REASON_BEACON_TIMEOUT:
    case WIFI_REASON_ASSOC_EXPIRE:
    case WIFI_REASON_AUTH_EXPIRE:
        return "the signal was too weak to hold — move the device closer";
    case WIFI_REASON_ASSOC_TOOMANY:
        return "the router has no room for another device";
    case WIFI_REASON_ASSOC_FAIL:
    case WIFI_REASON_CONNECTION_FAIL:
    case WIFI_REASON_NOT_AUTHED:
    case WIFI_REASON_NOT_ASSOCED:
    case WIFI_REASON_ASSOC_NOT_AUTHED:
        return "the network refused the connection";
    case WIFI_REASON_802_1X_AUTH_FAILED:
        return "this network needs a company login, which the device cannot do";
    case WIFI_REASON_CIPHER_SUITE_REJECTED:
    case WIFI_REASON_PAIRWISE_CIPHER_INVALID:
    case WIFI_REASON_GROUP_CIPHER_INVALID:
    case WIFI_REASON_AKMP_INVALID:
    case WIFI_REASON_UNSUPP_RSN_IE_VERSION:
    case WIFI_REASON_INVALID_RSN_IE_CAP:
        return "this network uses security the device does not support";
    case WIFI_REASON_TIMEOUT:
        return "timed out while connecting";
    default:
        return NULL;
    }
}

static void note_reason(uint8_t reason)
{
    const char *phrase = friendly_reason(reason);
    if (phrase == NULL) {
        /* Better an honest number than a wrong guess: it is the one thing
         * a support conversation can look up. */
        snprintf(s_error_buf, sizeof s_error_buf,
                 "the connection failed (Wi-Fi reason %u)", (unsigned)reason);
        phrase = s_error_buf;
    }
    s_error = phrase;
}

/* -------------------------------------------------------------- storage -- */

/* Eight hex characters, which is __main__.py's secrets.token_hex(4) and
 * the WPA2 minimum exactly. It gets read off a wall-mounted panel and
 * typed into a phone, so length past the minimum costs real effort. */
static void generate_hotspot_password(void)
{
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 8; i++) {
        s_hotspot_pw[i] = digits[esp_random() & 0x0f];
    }
    s_hotspot_pw[8] = '\0';
}

/* The password has to survive a reboot or the QR code on the panel and
 * the one on the settings page would drift apart into two networks. */
static void load_hotspot_password(void)
{
    nvs_handle_t nvs;
    if (nvs_open(GC_NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) {
        generate_hotspot_password();
        ESP_LOGW(TAG, "No NVS for the hotspot password; it will change on reboot");
        return;
    }
    size_t length = sizeof s_hotspot_pw;
    if (nvs_get_str(nvs, GC_NVS_KEY_HOTSPOT_PW, s_hotspot_pw, &length) != ESP_OK
        || strlen(s_hotspot_pw) < 8) {
        generate_hotspot_password();
        if (nvs_set_str(nvs, GC_NVS_KEY_HOTSPOT_PW, s_hotspot_pw) == ESP_OK) {
            nvs_commit(nvs);
        }
    }
    nvs_close(nvs);
}

/* The credentials the radio retries on its own. The config is the record
 * a person edits, so it wins when it has anything to say; these keys are
 * what a device still halfway through the wizard has instead, because its
 * config is not yet valid enough to be saved at all. */
static void load_credentials(const gc_config_t *config)
{
    if (config->wifi.ssid[0] != '\0') {
        copy_str(s_ssid, sizeof s_ssid, config->wifi.ssid);
        copy_str(s_psk, sizeof s_psk, config->wifi.psk);
        return;
    }
    nvs_handle_t nvs;
    if (nvs_open(GC_NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) {
        return;
    }
    size_t length = sizeof s_ssid;
    if (nvs_get_str(nvs, GC_NVS_KEY_SSID, s_ssid, &length) != ESP_OK) {
        s_ssid[0] = '\0';
    }
    length = sizeof s_psk;
    if (nvs_get_str(nvs, GC_NVS_KEY_PSK, s_psk, &length) != ESP_OK) {
        s_psk[0] = '\0';
    }
    nvs_close(nvs);
}

static void save_credentials(const char *ssid, const char *psk)
{
    nvs_handle_t nvs;
    if (nvs_open(GC_NVS_NAMESPACE, NVS_READWRITE, &nvs) == ESP_OK) {
        if (nvs_set_str(nvs, GC_NVS_KEY_SSID, ssid) == ESP_OK
            && nvs_set_str(nvs, GC_NVS_KEY_PSK, psk) == ESP_OK) {
            nvs_commit(nvs);
        }
        nvs_close(nvs);
    }

    /* Also into the config, so the settings page can show which network
     * the device is on. On a device still in the wizard this save is
     * refused — there is no person configured yet, and gc_config_save
     * validates before it writes — which is why the keys above exist. */
    gc_config_t *config = heap_caps_malloc(sizeof *config,
                                           MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (config == NULL) {
        return;
    }
    if (gc_config_load(config) == ESP_OK) {
        copy_str(config->wifi.ssid, sizeof config->wifi.ssid, ssid);
        copy_str(config->wifi.psk, sizeof config->wifi.psk, psk);
        esp_err_t err = gc_config_save(config);
        if (err != ESP_OK) {
            ESP_LOGD(TAG, "Wi-Fi credentials kept outside the config: %s",
                     esp_err_to_name(err));
        }
    }
    heap_caps_free(config);
}

/* ------------------------------------------------------------ the radio -- */

static esp_err_t apply_sta_config(const char *ssid, const char *psk)
{
    wifi_config_t cfg = {0};
    size_t ssid_len = strnlen(ssid, sizeof cfg.sta.ssid);
    memcpy(cfg.sta.ssid, ssid, ssid_len);
    if (psk != NULL) {
        memcpy(cfg.sta.password, psk, strnlen(psk, sizeof cfg.sta.password));
    }
    /* Houses with a mesh or a repeater put the same name on several
     * radios; scan the lot and take the loudest rather than the first. */
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    /* No floor on the security a network offers: refusing to join the
     * elderly router someone actually has is not our call to make. */
    cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.pmf_cfg.required = false;
    return esp_wifi_set_config(WIFI_IF_STA, &cfg);
}

static void schedule_reconnect(void)
{
    /* Not while a join is driving the radio, and not while the hotspot is
     * up: in setup mode the radio belongs to the person doing the setup,
     * which is network.py's rule too ("we're in setup mode; stay until
     * joined"). */
    if (s_joining || s_hotspot_up || s_ssid[0] == '\0' || s_reconnect_timer == NULL) {
        return;
    }
    esp_timer_stop(s_reconnect_timer);
    esp_timer_start_once(s_reconnect_timer, GC_RECONNECT_MS * 1000);
}

static void reconnect_cb(void *arg)
{
    (void)arg;
    if (s_joining || s_hotspot_up || s_ssid[0] == '\0') {
        return;
    }
    s_state = GC_NET_CONNECTING;
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK && err != ESP_ERR_WIFI_CONN) {
        ESP_LOGD(TAG, "Reconnect refused: %s", esp_err_to_name(err));
        schedule_reconnect();
    }
}

static void hotspot_off_cb(void *arg)
{
    (void)arg;
    if (gc_net_is_online()) {
        ESP_LOGI(TAG, "Back on a network; taking the setup hotspot down");
        gc_net_hotspot_stop();
    }
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        char text[sizeof s_ip];
        snprintf(text, sizeof text, IPSTR, IP2STR(&event->ip_info.ip));
        memcpy(s_ip, text, sizeof text);
        s_state = GC_NET_ONLINE;
        s_error = "";
        s_fails = 0;
        ESP_LOGI(TAG, "On '%s' at %s", s_ssid, s_ip);
        xEventGroupSetBits(s_events, GC_BIT_CONNECTED);
        /* A clock set before the network came up was set from nothing;
         * ask again now that there is somewhere to ask. */
        if (s_sntp_ready) {
            esp_netif_sntp_start();
        }
        return;
    }
    if (base != WIFI_EVENT) {
        return;
    }
    switch (id) {
    case WIFI_EVENT_STA_START:
        if (s_ssid[0] != '\0') {
            s_state = GC_NET_CONNECTING;
            esp_wifi_connect();
        }
        break;
    case WIFI_EVENT_STA_DISCONNECTED: {
        const wifi_event_sta_disconnected_t *event = data;
        note_reason(event->reason);
        s_ip[0] = '\0';
        s_state = s_hotspot_up ? GC_NET_HOTSPOT : GC_NET_DOWN;
        if (s_join_waiting) {
            xEventGroupSetBits(s_events, GC_BIT_FAILED);
        } else {
            ESP_LOGI(TAG, "Wi-Fi down: %s", s_error);
            schedule_reconnect();
        }
        break;
    }
    case WIFI_EVENT_AP_START:
        s_hotspot_up = true;
        if (s_state != GC_NET_ONLINE) {
            s_state = GC_NET_HOTSPOT;
        }
        break;
    case WIFI_EVENT_AP_STOP:
        s_hotspot_up = false;
        if (s_state == GC_NET_HOTSPOT) {
            s_state = GC_NET_DOWN;
        }
        break;
    case WIFI_EVENT_AP_STACONNECTED:
        ESP_LOGI(TAG, "A device joined the setup hotspot");
        break;
    default:
        break;
    }
}

/* The hotspot has to answer on GC_HOTSPOT_ADDR: it is the address the
 * Pi's hotspot uses, the one the captive portal redirects to, and the one
 * printed on the panel. ESP-IDF's default is 192.168.4.1, and the netif
 * refuses an address change while its DHCP server is running, so the
 * server is stopped, the whole subnet is moved, and it is started again.
 * Doing it here — before the AP has ever been raised — means the server
 * hands out the right subnet from its very first lease. */
static esp_err_t apply_ap_addressing(void)
{
    esp_netif_ip_info_t info = {0};
    esp_err_t err = esp_netif_str_to_ip4(GC_HOTSPOT_ADDR, &info.ip);
    if (err != ESP_OK) {
        return err;
    }
    info.gw = info.ip;
    err = esp_netif_str_to_ip4("255.255.255.0", &info.netmask);
    if (err != ESP_OK) {
        return err;
    }

    err = esp_netif_dhcps_stop(s_ap_netif);
    if (err != ESP_OK && err != ESP_ERR_ESP_NETIF_DHCP_ALREADY_STOPPED) {
        return err;
    }
    err = esp_netif_set_ip_info(s_ap_netif, &info);
    if (err != ESP_OK) {
        return err;
    }

    /* Leases from .100 to .150 of whatever subnet the contract names, so
     * the range follows GC_HOTSPOT_ADDR instead of being written twice. */
    uint32_t subnet = ntohl(info.ip.addr) & 0xffffff00u;
    dhcps_lease_t lease = {
        .enable = true,
        .start_ip = {.addr = htonl(subnet | 100u)},
        .end_ip = {.addr = htonl(subnet | 150u)},
    };
    err = esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET,
                                 ESP_NETIF_REQUESTED_IP_ADDRESS,
                                 &lease, sizeof lease);
    if (err != ESP_OK) {
        return err;
    }

    /* Offer ourselves as the DNS server, exactly as the Pi's dnsmasq
     * does: a phone that resolves every name to the hotspot is a phone
     * that opens the setup page by itself. Nothing here answers DNS — the
     * captive responder belongs to gc_httpd — but the lease has to point
     * at us before it can. */
    esp_netif_dns_info_t dns = {0};
    dns.ip.type = ESP_IPADDR_TYPE_V4;
    dns.ip.u_addr.ip4 = info.ip;
    esp_netif_set_dns_info(s_ap_netif, ESP_NETIF_DNS_MAIN, &dns);
    uint8_t offer_dns = 1;
    esp_netif_dhcps_option(s_ap_netif, ESP_NETIF_OP_SET,
                           ESP_NETIF_DOMAIN_NAME_SERVER,
                           &offer_dns, sizeof offer_dns);

    err = esp_netif_dhcps_start(s_ap_netif);
    if (err == ESP_ERR_ESP_NETIF_DHCP_ALREADY_STARTED) {
        err = ESP_OK;
    }
    return err;
}

/* ---------------------------------------------------------------- mDNS -- */

static void start_mdns(void)
{
#ifdef GC_HAVE_MDNS
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS did not start: %s", esp_err_to_name(err));
        return;
    }
    mdns_hostname_set(GC_MDNS_HOSTNAME);
    mdns_instance_name_set("GlucoCube");
    /* Port 80 rather than gc_httpd's GC_HTTPD_PORT: advertising the web
     * server should not make the network layer depend on the web layer. */
    mdns_service_add(NULL, "_http", "_tcp", 80, NULL, 0);
    ESP_LOGI(TAG, "http://%s.local/ is up", GC_MDNS_HOSTNAME);
#else
    ESP_LOGW(TAG, "Built without the mDNS component: http://%s.local/ will not "
                  "resolve, so the panel's IP address is the way in",
             GC_MDNS_HOSTNAME);
#endif
}

/* ------------------------------------------------------------- the clock -- */

/* newlib wants a POSIX TZ string; the config carries an IANA zone name,
 * because that is what config.py stores and what people recognise. There
 * is no zoneinfo database on the device to translate between them, so
 * this table does it for the zones a household is likely to be in. A zone
 * that is not here falls back to UTC rather than to a silently wrong
 * offset — and anyone in one of them can type a POSIX rule
 * ("AEST-10AEDT,M10.1.0,M4.1.0/3") into the timezone box instead, which
 * is passed through untouched. The rules below are the 2026 ones; a
 * country that changes its mind needs a firmware update, which is the
 * honest cost of not carrying a 400 KB database.
 */
static const struct {
    const char *iana;
    const char *posix;
} gc_zones[] = {
    {"UTC", "UTC0"},
    {"Etc/UTC", "UTC0"},
    {"GMT", "UTC0"},
    {"Europe/London", "GMT0BST,M3.5.0/1,M10.5.0/2"},
    {"Europe/Dublin", "GMT0IST,M3.5.0/1,M10.5.0/2"},
    {"Europe/Lisbon", "WET0WEST,M3.5.0/1,M10.5.0/2"},
    {"Atlantic/Reykjavik", "GMT0"},
    {"Europe/Paris", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Berlin", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Madrid", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Rome", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Amsterdam", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Brussels", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Vienna", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Zurich", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Stockholm", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Oslo", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Copenhagen", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Warsaw", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Prague", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Budapest", "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"Europe/Athens", "EET-2EEST,M3.5.0/3,M10.5.0/4"},
    {"Europe/Helsinki", "EET-2EEST,M3.5.0/3,M10.5.0/4"},
    {"Europe/Bucharest", "EET-2EEST,M3.5.0/3,M10.5.0/4"},
    {"Europe/Kyiv", "EET-2EEST,M3.5.0/3,M10.5.0/4"},
    {"Europe/Istanbul", "<+03>-3"},
    {"Europe/Moscow", "MSK-3"},
    {"America/St_Johns", "NST3:30NDT,M3.2.0,M11.1.0"},
    {"America/Halifax", "AST4ADT,M3.2.0,M11.1.0"},
    {"America/New_York", "EST5EDT,M3.2.0,M11.1.0"},
    {"America/Toronto", "EST5EDT,M3.2.0,M11.1.0"},
    {"America/Chicago", "CST6CDT,M3.2.0,M11.1.0"},
    {"America/Winnipeg", "CST6CDT,M3.2.0,M11.1.0"},
    {"America/Denver", "MST7MDT,M3.2.0,M11.1.0"},
    {"America/Edmonton", "MST7MDT,M3.2.0,M11.1.0"},
    {"America/Phoenix", "MST7"},
    {"America/Los_Angeles", "PST8PDT,M3.2.0,M11.1.0"},
    {"America/Vancouver", "PST8PDT,M3.2.0,M11.1.0"},
    {"America/Anchorage", "AKST9AKDT,M3.2.0,M11.1.0"},
    {"Pacific/Honolulu", "HST10"},
    {"America/Mexico_City", "CST6"},
    {"America/Bogota", "<-05>5"},
    {"America/Lima", "<-05>5"},
    {"America/Santiago", "<-04>4<-03>,M9.1.6/24,M4.1.6/24"},
    {"America/Sao_Paulo", "<-03>3"},
    {"America/Argentina/Buenos_Aires", "<-03>3"},
    {"Africa/Casablanca", "<+01>-1"},
    {"Africa/Lagos", "WAT-1"},
    {"Africa/Johannesburg", "SAST-2"},
    {"Africa/Cairo", "EET-2EEST,M4.5.5/0,M10.5.4/24"},
    {"Africa/Nairobi", "EAT-3"},
    {"Asia/Jerusalem", "IST-2IDT,M3.4.4/26,M10.5.0"},
    {"Asia/Riyadh", "<+03>-3"},
    {"Asia/Dubai", "<+04>-4"},
    {"Asia/Karachi", "PKT-5"},
    {"Asia/Kolkata", "IST-5:30"},
    {"Asia/Calcutta", "IST-5:30"},
    {"Asia/Dhaka", "<+06>-6"},
    {"Asia/Bangkok", "<+07>-7"},
    {"Asia/Jakarta", "WIB-7"},
    {"Asia/Singapore", "<+08>-8"},
    {"Asia/Hong_Kong", "HKT-8"},
    {"Asia/Shanghai", "CST-8"},
    {"Asia/Taipei", "CST-8"},
    {"Asia/Manila", "PST-8"},
    {"Asia/Seoul", "KST-9"},
    {"Asia/Tokyo", "JST-9"},
    {"Australia/Perth", "AWST-8"},
    {"Australia/Darwin", "ACST-9:30"},
    {"Australia/Adelaide", "ACST-9:30ACDT,M10.1.0,M4.1.0/3"},
    {"Australia/Brisbane", "AEST-10"},
    {"Australia/Sydney", "AEST-10AEDT,M10.1.0,M4.1.0/3"},
    {"Australia/Melbourne", "AEST-10AEDT,M10.1.0,M4.1.0/3"},
    {"Australia/Hobart", "AEST-10AEDT,M10.1.0,M4.1.0/3"},
    {"Pacific/Auckland", "NZST-12NZDT,M9.5.0,M4.1.0/3"},
};

const char *gc_net_zone_name(int index)
{
    const int count = (int)(sizeof gc_zones / sizeof gc_zones[0]);
    return (index >= 0 && index < count) ? gc_zones[index].iana : NULL;
}

static void apply_timezone(const char *timezone)
{
    const char *posix = "UTC0";
    if (timezone != NULL && timezone[0] != '\0') {
        posix = NULL;
        for (size_t i = 0; i < sizeof gc_zones / sizeof gc_zones[0]; i++) {
            if (strcasecmp(timezone, gc_zones[i].iana) == 0) {
                posix = gc_zones[i].posix;
                break;
            }
        }
        if (posix == NULL) {
            /* An IANA name always carries a region and a slash; anything
             * else is a POSIX rule someone typed on purpose. */
            if (strchr(timezone, '/') == NULL) {
                posix = timezone;
            } else {
                ESP_LOGW(TAG, "Zone '%s' is not one of the %u this build knows; "
                              "staying on UTC. A POSIX TZ rule works instead.",
                         timezone, (unsigned)(sizeof gc_zones / sizeof gc_zones[0]));
                posix = "UTC0";
            }
        }
    }
    setenv("TZ", posix, 1);
    tzset();
}

/* --------------------------------------------------------------- scans -- */

static int compare_records(const wifi_ap_record_t *a, const wifi_ap_record_t *b)
{
    return b->rssi - a->rssi;
}

static void sort_records(wifi_ap_record_t *records, int count)
{
    /* Insertion sort: a scan is a few dozen rows, and the cost of moving
     * 80-byte records around is smaller than the cost of being clever. */
    for (int i = 1; i < count; i++) {
        wifi_ap_record_t key = records[i];
        int j = i - 1;
        while (j >= 0 && compare_records(&records[j], &key) > 0) {
            records[j + 1] = records[j];
            j--;
        }
        records[j + 1] = key;
    }
}

static bool scan_begin(void)
{
    bool go = false;
    lock();
    int64_t age = now_us() - s_scan_at_us;
    bool fresh = s_scan_at_us != 0 && age < (int64_t)GC_NET_SCAN_REFRESH_SECONDS * 1000000LL;
    /* While the hotspot is up the radio is in the middle of somebody's
     * setup session and a scan costs them a few seconds of dropped page.
     * network.py refuses outright there (nmcli cannot scan in AP mode at
     * all); the S3 can, so it does — but only when the list would
     * otherwise be empty, which is the one case where not scanning leaves
     * the settings page with nothing to offer. */
    bool blocked = s_scan_busy || s_joining || fresh
                   || (s_hotspot_up && s_scan_count > 0);
    if (!blocked) {
        s_scan_busy = true;
        go = true;
    }
    unlock();
    return go;
}

/* An attempt that brought nothing back leaves the list alone. A scan can
 * come up empty because the radio was busy elsewhere for its two seconds,
 * and the last real list is worth more to the settings page than an
 * honest but useless blank one — refresh_scan() on the Pi keeps it for
 * the same reason. */
static void scan_finish(void)
{
    lock();
    s_scan_at_us = now_us();
    s_scan_busy = false;
    unlock();
}

static void scan_store(const wifi_ap_record_t *records, int count)
{
    lock();
    int kept = 0;
    for (int i = 0; i < count && kept < GC_MAX_SCAN_RESULTS; i++) {
        const char *ssid = (const char *)records[i].ssid;
        if (ssid[0] == '\0' || strcmp(ssid, GC_HOTSPOT_SSID) == 0) {
            continue;
        }
        /* Records arrive strongest first, so the first sighting of a name
         * is the radio worth showing and later ones are its repeaters. */
        bool seen = false;
        for (int j = 0; j < kept; j++) {
            if (strcmp(s_scan[j].ssid, ssid) == 0) {
                seen = true;
                break;
            }
        }
        if (seen) {
            continue;
        }
        copy_str(s_scan[kept].ssid, sizeof s_scan[kept].ssid, ssid);
        s_scan[kept].rssi = records[i].rssi;
        s_scan[kept].secured = records[i].authmode != WIFI_AUTH_OPEN;
        kept++;
    }
    /* Nothing worth listing leaves the previous list in place, untouched
     * by the loop above, which only ever wrote the entries it counted. */
    if (kept > 0) {
        s_scan_count = kept;
    }
    s_scan_at_us = now_us();
    s_scan_busy = false;
    unlock();
}

static void refresh_scan(void)
{
    if (!scan_begin()) {
        return;
    }

    wifi_scan_config_t config = {
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
    };
    esp_err_t err = esp_wifi_scan_start(&config, true);
    if (err != ESP_OK) {
        /* A failure still counts as an attempt: a settings page that
         * retried a failing scan on every request would hold the radio
         * for as long as somebody watched it. */
        ESP_LOGI(TAG, "Wi-Fi scan failed: %s", esp_err_to_name(err));
        scan_finish();
        return;
    }

    uint16_t found = 0;
    esp_wifi_scan_get_ap_num(&found);
    if (found > GC_SCAN_MAX_RECORDS) {
        found = GC_SCAN_MAX_RECORDS;
    }
    if (found == 0) {
        esp_wifi_clear_ap_list();
        scan_finish();
        return;
    }

    wifi_ap_record_t *records = heap_caps_malloc((size_t)found * sizeof *records,
                                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (records == NULL) {
        /* The driver holds the results until somebody reads or drops
         * them; dropping them is the only option left. */
        esp_wifi_clear_ap_list();
        ESP_LOGW(TAG, "No room to read the scan results");
        scan_finish();
        return;
    }
    err = esp_wifi_scan_get_ap_records(&found, records);
    if (err != ESP_OK) {
        esp_wifi_clear_ap_list();   /* a read that failed still owns the list */
        found = 0;
    }
    sort_records(records, (int)found);
    scan_store(records, (int)found);
    heap_caps_free(records);
    ESP_LOGD(TAG, "Scan found %d networks", s_scan_count);
}

/* --------------------------------------------------------------- public -- */

esp_err_t gc_net_init(const gc_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_started) {
        return ESP_OK;
    }

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* An NVS partition that will not mount has already lost whatever
         * was configured; reformatting it is the only way to a device
         * that can be set up again rather than one that boots into
         * nothing for ever. */
        ESP_LOGW(TAG, "NVS unusable; erasing it and starting over");
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return err;
    }

    s_lock = xSemaphoreCreateMutex();
    s_events = xEventGroupCreate();
    if (s_lock == NULL || s_events == NULL) {
        return ESP_ERR_NO_MEM;
    }

    err = esp_netif_init();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err == ESP_ERR_INVALID_STATE) {
        err = ESP_OK;   /* somebody earlier in app_main already made it */
    }
    if (err != ESP_OK) {
        return err;
    }

    s_sta_netif = esp_netif_create_default_wifi_sta();
    s_ap_netif = esp_netif_create_default_wifi_ap();
    if (s_sta_netif == NULL || s_ap_netif == NULL) {
        return ESP_ERR_NO_MEM;
    }
    /* The name the DHCP lease carries, so a router's device list shows
     * the same word as mDNS does. */
    esp_netif_set_hostname(s_sta_netif, GC_MDNS_HOSTNAME);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init);
    if (err != ESP_OK) {
        return err;
    }
    /* Credentials live in the config and in our own NVS keys, written
     * only after a join has worked. Letting the driver keep a second copy
     * would give the device two answers to "which network is this". */
    esp_wifi_set_storage(WIFI_STORAGE_RAM);

    err = esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                              on_wifi_event, NULL, NULL);
    if (err == ESP_OK) {
        err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                  on_wifi_event, NULL, NULL);
    }
    if (err != ESP_OK) {
        return err;
    }

    const esp_timer_create_args_t reconnect_args = {
        .callback = reconnect_cb,
        .name = "gc_net_retry",
    };
    const esp_timer_create_args_t hotspot_args = {
        .callback = hotspot_off_cb,
        .name = "gc_net_ap_off",
    };
    err = esp_timer_create(&reconnect_args, &s_reconnect_timer);
    if (err == ESP_OK) {
        err = esp_timer_create(&hotspot_args, &s_hotspot_off_timer);
    }
    if (err != ESP_OK) {
        return err;
    }

    load_hotspot_password();
    load_credentials(config);
    err = apply_ap_addressing();
    if (err != ESP_OK) {
        /* Not fatal: the station half still works, and the panel would
         * rather say "no network" than not boot. */
        ESP_LOGE(TAG, "Could not move the hotspot to %s: %s",
                 GC_HOTSPOT_ADDR, esp_err_to_name(err));
    }

    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        return err;
    }
    if (s_ssid[0] != '\0') {
        apply_sta_config(s_ssid, s_psk);
        s_state = GC_NET_CONNECTING;
    }
    err = esp_wifi_start();
    if (err != ESP_OK) {
        return err;
    }

    /* Something has to be believed about the time before the first sync,
     * or every timestamp drawn between here and then is in UTC whatever
     * the config says. */
    apply_timezone(config->display.timezone);
    start_mdns();

    s_started = true;
    ESP_LOGI(TAG, "Network up; %s", s_ssid[0] != '\0'
                                        ? "joining the saved network"
                                        : "no network configured yet");
    return ESP_OK;
}

gc_net_state_t gc_net_state(void)
{
    return s_state;
}

bool gc_net_is_online(void)
{
    return s_state == GC_NET_ONLINE && s_ip[0] != '\0';
}

const char *gc_net_ip(void)
{
    if (gc_net_is_online()) {
        return s_ip;
    }
    if (s_hotspot_up) {
        return GC_HOTSPOT_ADDR;
    }
    return "";
}

const char *gc_net_last_error(void)
{
    return s_error;
}

esp_err_t gc_net_join(const char *ssid, const char *psk)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (ssid == NULL || ssid[0] == '\0') {
        s_error = "no network was chosen";
        return ESP_ERR_INVALID_ARG;
    }
    if (strlen(ssid) >= GC_MAX_SSID) {
        s_error = "that network name is too long to be a real one";
        return ESP_ERR_INVALID_ARG;
    }
    if (psk == NULL) {
        psk = "";
    }
    if (strlen(psk) >= GC_MAX_PSK || (psk[0] != '\0' && strlen(psk) < 8)) {
        /* network.py's wording for the same refusal. */
        s_error = "password rejected (Wi-Fi passwords must be 8-63 characters)";
        return ESP_ERR_INVALID_ARG;
    }

    /* Single-flight, for network.py's reason: two joins would drive the
     * same radio, and the loser's cleanup would undo the winner's work. */
    lock();
    if (s_joining) {
        unlock();
        s_error = "another join is already in progress";
        return ESP_ERR_INVALID_STATE;
    }
    s_joining = true;
    s_quiet_until_us = now_us() + GC_QUIET_JOIN_US;
    unlock();

    ESP_LOGI(TAG, "Joining '%s'", ssid);
    esp_timer_stop(s_reconnect_timer);

    /* Leave the network we are on first, and let that disconnect land
     * before arming the wait — otherwise the event from our own departure
     * is the one the wait sees, and every join fails instantly. The
     * common case (a device with no network, which is why somebody is on
     * the setup page at all) skips this entirely. */
    wifi_ap_record_t current;
    if (esp_wifi_sta_get_ap_info(&current) == ESP_OK) {
        esp_wifi_disconnect();
        vTaskDelay(pdMS_TO_TICKS(200));
    }

    xEventGroupClearBits(s_events, GC_BIT_CONNECTED | GC_BIT_FAILED);
    s_error = "";
    s_state = GC_NET_CONNECTING;

    esp_err_t err = apply_sta_config(ssid, psk);
    if (err == ESP_OK) {
        s_join_waiting = true;
        err = esp_wifi_connect();
        if (err == ESP_OK) {
            EventBits_t bits = xEventGroupWaitBits(
                s_events, GC_BIT_CONNECTED | GC_BIT_FAILED, pdFALSE, pdFALSE,
                pdMS_TO_TICKS(GC_JOIN_TIMEOUT_MS));
            if (bits & GC_BIT_CONNECTED) {
                err = ESP_OK;
            } else if (bits & GC_BIT_FAILED) {
                err = ESP_FAIL;
            } else {
                err = ESP_ERR_TIMEOUT;
                s_error = "timed out while connecting";
            }
        }
        s_join_waiting = false;
    }

    if (err == ESP_OK) {
        copy_str(s_ssid, sizeof s_ssid, ssid);
        copy_str(s_psk, sizeof s_psk, psk);
        save_credentials(ssid, psk);
        s_error = "";
        ESP_LOGI(TAG, "Joined '%s'", ssid);
        if (s_hotspot_up) {
            /* The phone asking is on the hotspot; it needs the AP to
             * survive long enough to read the answer. The Pi does not
             * have this problem: it reboots into the new network and the
             * phone finds out by the page never coming back. */
            esp_timer_stop(s_hotspot_off_timer);
            esp_timer_start_once(s_hotspot_off_timer, GC_HOTSPOT_GRACE_MS * 1000);
        }
    } else {
        ESP_LOGW(TAG, "Could not join '%s': %s", ssid, s_error);
        esp_wifi_disconnect();
        s_state = s_hotspot_up ? GC_NET_HOTSPOT : GC_NET_DOWN;
        /* Put the network we had back, the way network.py deletes the
         * half-made profile: a rejected password must not cost the device
         * the connection it arrived with. */
        if (s_ssid[0] != '\0') {
            apply_sta_config(s_ssid, s_psk);
        }
    }

    lock();
    s_joining = false;
    s_quiet_until_us = now_us() + GC_QUIET_AFTER_US;
    unlock();
    if (err != ESP_OK) {
        schedule_reconnect();
    }
    return err;
}

int gc_net_scan(gc_scan_result_t *out, int max_results)
{
    if (out == NULL || max_results <= 0 || !s_started) {
        return 0;
    }
    refresh_scan();

    lock();
    int count = s_scan_count < max_results ? s_scan_count : max_results;
    memcpy(out, s_scan, (size_t)count * sizeof *out);
    unlock();
    return count;
}

esp_err_t gc_net_hotspot_start(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_hotspot_up) {
        return ESP_OK;
    }
    esp_timer_stop(s_hotspot_off_timer);
    esp_timer_stop(s_reconnect_timer);

    wifi_config_t ap = {0};
    size_t ssid_len = strlen(GC_HOTSPOT_SSID);
    memcpy(ap.ap.ssid, GC_HOTSPOT_SSID, ssid_len);
    ap.ap.ssid_len = (uint8_t)ssid_len;
    memcpy(ap.ap.password, s_hotspot_pw, strlen(s_hotspot_pw));
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.pairwise_cipher = WIFI_CIPHER_TYPE_CCMP;
    ap.ap.max_connection = 4;
    ap.ap.beacon_interval = 100;
    /* One radio, one channel: if the station is still associated the AP
     * has to sit on its channel, and asking for another only makes the
     * driver move it back. */
    ap.ap.channel = 6;
    wifi_ap_record_t current;
    if (esp_wifi_sta_get_ap_info(&current) == ESP_OK) {
        ap.ap.channel = current.primary;
    }

    /* The AP interface has to be enabled before it will take a config,
     * so there is a beat where it beacons with nothing set. It is short
     * enough that no phone will have finished a scan in it, and shorter
     * than stopping the whole radio to avoid it would be. */
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_APSTA);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Could not start the setup hotspot: %s", esp_err_to_name(err));
        esp_wifi_set_mode(WIFI_MODE_STA);
        return err;
    }

    /* Believe it now rather than at WIFI_EVENT_AP_START: the caller is
     * about to draw a QR code for a network it has just asked for. */
    s_hotspot_up = true;
    if (s_state != GC_NET_ONLINE) {
        s_state = GC_NET_HOTSPOT;
    }
    ESP_LOGI(TAG, "Setup hotspot '%s' started at %s", GC_HOTSPOT_SSID, GC_HOTSPOT_ADDR);
    return ESP_OK;
}

esp_err_t gc_net_hotspot_stop(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_hotspot_up) {
        return ESP_OK;
    }
    esp_timer_stop(s_hotspot_off_timer);
    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK) {
        return err;
    }
    s_hotspot_up = false;
    if (s_state == GC_NET_HOTSPOT) {
        s_state = GC_NET_DOWN;
    }
    ESP_LOGI(TAG, "Setup hotspot stopped");
    /* The radio is the station's again; go back to the saved network
     * without waiting for the watcher's next tick. */
    if (!gc_net_is_online()) {
        schedule_reconnect();
    }
    return ESP_OK;
}

bool gc_net_hotspot_active(void)
{
    return s_hotspot_up;
}

const char *gc_net_hotspot_password(void)
{
    return s_hotspot_pw;
}

/* -------------------------------------------------------------- watcher -- */

/* NetworkWatcher._tick(), line for line. The only substitution is what
 * "no network" means: a station without a DHCP lease, rather than
 * nmcli's connectivity check. */
static void watch_tick(void)
{
    if (s_joining || now_us() < s_quiet_until_us) {
        s_fails = 0;
        return;
    }
    if (s_hotspot_up) {
        s_fails = 0;    /* in setup mode; stay until somebody joins */
        return;
    }
    if (gc_net_is_online()) {
        s_fails = 0;
        /* Keep a fresh list of neighbours for the next setup round; the
         * rate limit inside refresh_scan() is the same 300 seconds the
         * watcher on the Pi counts for itself. */
        refresh_scan();
        return;
    }

    s_fails++;
    int needed = s_ssid[0] != '\0' ? GC_NET_FAILS_NEEDED : 1;
    if (s_fails < needed) {
        return;
    }
    /* One last listen while the radio still can — once the AP is up the
     * settings page has no other way to offer a list of networks. */
    refresh_scan();
    gc_net_hotspot_start();
}

static void watch_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(GC_NET_FIRST_CHECK_DELAY * 1000));
    for (;;) {
        watch_tick();
        vTaskDelay(pdMS_TO_TICKS(GC_NET_CHECK_SECONDS * 1000));
    }
}

esp_err_t gc_net_watch_start(void)
{
    if (!s_started) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_watch_task != NULL) {
        return ESP_OK;
    }
    /* Below the draw loop: a scan taking a few seconds must never be the
     * reason a frame is late. */
    BaseType_t ok = xTaskCreate(watch_task, "gc_net_watch", 4096, NULL, 4,
                                &s_watch_task);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

/* ---------------------------------------------------------------- clock -- */

esp_err_t gc_net_time_sync(const char *timezone)
{
    apply_timezone(timezone);

    if (!s_sntp_ready) {
        /* One server, because CONFIG_LWIP_SNTP_MAX_SERVERS is one by
         * default and the pool is a rotating set of servers anyway. */
        esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
        esp_err_t err = esp_netif_sntp_init(&config);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            return err;
        }
        s_sntp_ready = true;
    } else {
        esp_netif_sntp_start();     /* ask again; already-running is fine */
    }

    if (gc_net_time_is_set()) {
        return ESP_OK;
    }
    /* SNTP keeps trying in the background whatever this returns; the
     * caller gets told so it can leave the panel saying it does not know
     * the time yet, which is the honest thing to draw. */
    return esp_netif_sntp_sync_wait(pdMS_TO_TICKS(GC_SNTP_WAIT_MS));
}

bool gc_net_time_is_set(void)
{
    return time(NULL) > GC_CLOCK_SANE_EPOCH;
}
