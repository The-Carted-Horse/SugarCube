/*
 * What the device is set to, held in NVS.
 *
 * The Raspberry Pi keeps config.json on the card and validates it before
 * replacing it, because a bad edit there restart-loops the device
 * (glucocube/config.py). The same rule applies here with less room to
 * recover: NVS is written only after the new settings parse, and the old
 * ones stay live until they do.
 *
 * Field for field this is config.py's Config/DisplayConfig/UserConfig,
 * minus what only a Pi has (ports for push uploaders, the admin port,
 * wallpapers).
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#include "gc_contract.h"
#include "gc_store.h"

#ifdef __cplusplus
extern "C" {
#endif

#define GC_MAX_URL 192
#define GC_MAX_SECRET 128
#define GC_MAX_EMAIL 128
#define GC_MAX_TOKEN 256
#define GC_MAX_TZ 64
#define GC_MAX_SSID 33
#define GC_MAX_PSK 65

typedef enum {
    GC_SOURCE_NONE = 0,
    GC_SOURCE_GLUCOCORE,
    GC_SOURCE_NIGHTSCOUT,
    GC_SOURCE_TIDEPOOL,
} gc_source_kind_t;

/* The lowercase name this source has in config.json and in a badge lookup. */
const char *gc_source_kind_name(gc_source_kind_t kind);
gc_source_kind_t gc_source_kind_from_name(const char *name);

typedef struct {
    char name[GC_MAX_NAME];
    gc_source_kind_t kind;

    /* glucocore */
    char patient_id[GC_MAX_ID];

    /* nightscout */
    char url[GC_MAX_URL];
    char api_secret[GC_MAX_SECRET];   /* or an access token; auto-detected */

    /* tidepool */
    char email[GC_MAX_EMAIL];
    char password[GC_MAX_SECRET];

    int poll_seconds;

    /* Per-person overrides. A threshold not set here inherits the display's. */
    bool has_low, has_high, has_urgent_low, has_urgent_high;
    float low, high, urgent_low, urgent_high;
} gc_user_config_t;

typedef struct {
    bool mmol;                 /* what readings are read in */
    char timezone[GC_MAX_TZ];  /* IANA name; blank leaves the clock on UTC */
    float low, high, urgent_low, urgent_high;
    float stale_minutes;
    int backlight_percent;
    int night_backlight_percent;
    int night_from_hour, night_to_hour;
    int time_format;           /* 12 or 24 */
    gc_theme_t theme;
} gc_display_config_t;

typedef struct {
    char ssid[GC_MAX_SSID];
    char psk[GC_MAX_PSK];
} gc_wifi_config_t;

typedef struct {
    char device_id[GC_MAX_ID];
    char device_token[GC_MAX_TOKEN];
    char hardware_id[GC_MAX_ID];
    int32_t config_version;
} gc_glucocore_config_t;

typedef struct {
    int user_count;
    gc_user_config_t users[GC_MAX_USERS];
    gc_display_config_t display;
    gc_wifi_config_t wifi;
    gc_glucocore_config_t glucocore;
    gc_channel_t update_channel;
    char admin_password[GC_MAX_SECRET];  /* empty means no password */
    bool admin_password_off;             /* empty on purpose, so stop asking */
} gc_config_t;

/* Everything the contract says, and nothing configured. */
void gc_config_defaults(gc_config_t *config);

/* Reads NVS into config, falling back to defaults for anything absent.
 * Never fails in a way that leaves a device unable to boot: a blob that
 * will not parse is logged and ignored, which lands on the setup wizard
 * rather than on a restart loop. */
esp_err_t gc_config_load(gc_config_t *config);

/* Validates, then writes. The live config is only replaced once the new
 * one has parsed, so a bad save leaves the device running what it was. */
esp_err_t gc_config_save(const gc_config_t *config);

/* Rejects what config.py's load() rejects: no people, a blank name, a
 * source missing the credentials it needs, thresholds out of order.
 * Writes a human-readable reason into `reason` when it fails. */
bool gc_config_valid(const gc_config_t *config, char *reason, size_t reason_len);

/* True while the device has nothing to show and should open the wizard. */
bool gc_config_is_unconfigured(const gc_config_t *config);

/* This board's stable id, from the eFuse MAC — what GlucoCore pairs
 * against, and what survives a re-flash. */
const char *gc_hardware_id(void);

/* The thresholds for one person: their own overrides on top of the
 * display's defaults, exactly as config.merged_thresholds does. */
typedef struct {
    float low, high, urgent_low, urgent_high, stale_minutes;
} gc_thresholds_t;

gc_thresholds_t gc_merged_thresholds(const gc_config_t *config, int user);

#ifdef __cplusplus
}
#endif
