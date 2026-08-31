"""The numbers both GlucoCube devices have to agree on.

GlucoCube ships as two products from one repository: a Raspberry Pi SD-card
image, and firmware for an ESP32-S3 board (``firmware/``). They render the
same dashboard, apply the same thresholds and run the same forecast, so
every constant that decides what a person sees lives here rather than being
typed out twice in two languages.

``firmware/tools/gen_contract.py`` reads this module and writes
``firmware/components/gc_contract/include/gc_contract.h``; a test asserts
the generated header is in step with this file, so the two devices cannot
drift apart without CI saying so.

Nothing here imports anything. It is data.
"""

# --------------------------------------------------------------- palette ----
#
# RGB, 0-255. The display draws in these directly; the web UI has its own
# CSS copy of the same values, and the firmware gets them as RGB565.

PALETTES = {
    "dark": {
        "bg": (10, 12, 15),
        "band": (20, 25, 30),
        "line": (38, 45, 52),
        "fg": (233, 237, 241),
        "dim": (122, 132, 142),
        "faint": (84, 93, 102),
        "trace": (157, 165, 174),
        "stale": (96, 104, 112),
        "in_range": (95, 222, 150),
        "high": (233, 185, 73),
        "low": (244, 92, 84),
        "urgent": (255, 69, 58),
    },
    "light": {
        "bg": (246, 247, 245),
        "band": (233, 235, 230),
        "line": (209, 212, 207),
        "fg": (24, 28, 32),
        "dim": (102, 110, 118),
        "faint": (148, 155, 162),
        "trace": (122, 130, 138),
        "stale": (170, 176, 182),
        "in_range": (16, 148, 72),
        "high": (176, 116, 8),
        "low": (204, 44, 36),
        "urgent": (224, 0, 0),
    },
}

# The order the roles appear in the generated C enum. Adding a role means
# adding it to both palettes and to the end of this tuple.
PALETTE_ROLES = (
    "bg", "band", "line", "fg", "dim", "faint",
    "trace", "stale", "in_range", "high", "low", "urgent",
)

DEFAULT_THEME = "dark"


# ------------------------------------------------------------ thresholds ----

THRESHOLD_DEFAULTS = {
    "low": 70.0,
    "high": 180.0,
    "urgent_low": 55.0,
    "urgent_high": 250.0,
}
STALE_MINUTES_DEFAULT = 12.0

# A reading younger than this keeps the freshness dot green; older but not
# yet stale turns it amber, and past stale_minutes it goes red.
FRESH_MINUTES = 7.0

UNITS_MGDL = "mg/dL"
UNITS_MMOL = "mmol/L"

# The divisor every CGM app uses. The exact molar figure is 18.0182, and
# nobody displays it: matching what the pump app on the same shelf says
# matters more than the third decimal place. Both products divide by the
# same number or they disagree at the first decimal.
MGDL_PER_MMOL = 18.0

# What people write when they mean mmol/L, including what a config file
# edited by hand is likely to contain.
MMOL_SPELLINGS = ("mmol", "mmol/l", "mmoll", "mm")


# -------------------------------------------------------------- forecast ----

HORIZONS = (30, 60, 90, 120)           # minutes ahead the forecast reports
STEP_MS = 5 * 60 * 1000                # AID predictions are a 5-minute series
MAX_PREDICTION_AGE_MS = 15 * 60 * 1000  # older than this and we do not forecast

# oref0-style fallback model (see oref.py). These are oref0's own numbers.
MIN_5M_CARBIMPACT = 8.0                # mg/dL per 5m assumed while COB > 0
UAM_DECAY_STEPS = 12                   # deviation decays over 60 minutes
CLAMP_LO = 39.0
CLAMP_HI = 401.0
STEP_MIN = 5.0
DEVIATION_WINDOW_MS = 45 * 60 * 1000   # how far back deviations are measured
DEVIATION_MIN_GAP_MIN = 2.0
DEVIATION_MAX_GAP_MIN = 12.0
IOB_SCALE_MIN = 0.25                   # clamps on pump-IOB rescaling
IOB_SCALE_MAX = 4.0
SYNTHETIC_BOLUS_AGE_MIN = 60.0         # stand-in bolus when none are visible
SYNTHETIC_BOLUS_MIN_FRAC = 0.05
UAM_DEVIATION_THRESHOLD = 2.0          # |avg deviation| above which UAM wins

# Therapy settings, and the range each is believed in. Profile endpoints
# carry placeholder junk often enough that implausible values are dropped
# rather than used (Trio uploads sens=720, carbratio=200).
THERAPY_DEFAULTS = {
    "isf": 50.0,        # mg/dL per U
    "cr": 10.0,         # g per U
    "dia_hours": 6.0,
    "peak_min": 75.0,   # rapid-acting, oref0 exponential model
}
THERAPY_RANGES = {
    "isf": (10.0, 400.0),
    "cr": (2.0, 50.0),
    "dia_hours": (3.0, 10.0),
    "peak_min": (30.0, 120.0),
}

# oref0's exponential model is only defined while the activity peak sits
# before the midpoint of the insulin duration: at peak == duration / 2 its
# tau term divides by zero. A 3-hour DIA with a 90-minute peak is inside
# both ranges above and is exactly that case, so the peak is clamped rather
# than trusted. 0.45 keeps the curve well clear of the singularity while
# leaving every ordinary profile (75 minutes against a 6-hour DIA) untouched.
PEAK_MAX_FRACTION_OF_DIA = 0.45


# ----------------------------------------------------------------- chart ----

CHART_HISTORY_MINUTES = 180            # the measured past on the x axis
CHART_FORECAST_MINUTES = 120           # the forecast ahead of it
CHART_PAD_BELOW = 18.0                 # mg/dL of headroom under the lowest point
CHART_PAD_ABOVE = 24.0
CHART_GAP_SPLIT_MS = 15 * 60 * 1000    # a sensor outage breaks the trace
CONE_BASE_SPREAD = 4.0                 # mg/dL at t=0
CONE_RATE_DEVICE = 0.17                # mg/dL per minute, pump's own curve
CONE_RATE_ESTIMATE = 0.26              # ours is less certain, so wider
CONE_ALPHA = 30                        # 0-255 fill over the background
NOW_HALO_ALPHA = 46

# x-axis ticks, as (minutes from now, label).
CHART_TICKS = (
    (-180, "-3H"), (-120, "-2H"), (-60, "-1H"),
    (0, "NOW"), (60, "+1H"), (120, "+2H"),
)


# ------------------------------------------------------------ trend arrow ----
#
# Nightscout direction string -> (rotation in degrees, how many arrowheads).

DIRECTION_ANGLES = {
    "DoubleUp": (-90, 2),
    "SingleUp": (-90, 1),
    "FortyFiveUp": (-45, 1),
    "Flat": (0, 1),
    "FortyFiveDown": (45, 1),
    "SingleDown": (90, 1),
    "DoubleDown": (90, 2),
}


# ---------------------------------------------------------------- badges ----

SOURCE_LABELS = {
    "tidepool": "TWIIST",
    "nightscout": "NS",
    "glucocore": "CORE",
}
SOURCE_LABEL_DEFAULT = "TRIO"


# ---------------------------------------------------------------- layout ----
#
# Everything is a fraction of the panel's own height (h) or width (w), so
# one set of numbers lays the dashboard out on the Pi's 7" panel, on the
# ESP32's 5" panel, and in a dev window of any size. Names ending in _PX
# are pixel floors, not fractions.

LAYOUT = {
    # Panel frame
    "panel_pad_w": 0.075,
    "panel_divider_inset_landscape": 0.05,   # of body height, top
    "panel_divider_inset_landscape_bottom": 0.03,
    "panel_divider_inset_portrait": 0.03,    # of full width, both sides

    # Header: name, freshness dot, source badge
    "header_top_h": 0.055,
    "name_px_h": 0.052,
    "badge_px_h": 0.032,
    "badge_top_h": 0.012,
    "badge_dot_gap_h": 0.035,
    "badge_dot_r_h": 0.011,
    "badge_dot_r_px": 3,

    # The glucose number and its trend column
    "num_px_h": 0.33,
    "num_top_h": 0.125,
    "num_cap_ratio": 0.70,               # Space Grotesk capHeight = 700/1000 em
    "num_left_nudge": 0.05,              # of num_px, leftwards
    "num_max_w_reserve_w": 0.26,         # width kept for the trend column
    "num_px_min": 12,
    "trend_gap_w": 0.04,
    "arrow_size_h": 0.06,
    "arrow_y_cap": 0.16,                 # of cap height, below num_top
    "delta_px_h": 0.08,
    "delta_y_cap": 0.36,
    "unit_px_h": 0.027,
    "unit_y_cap": 0.80,

    # FORECAST 2H row
    "forecast_y_h": 0.48,
    "forecast_label_px_h": 0.031,
    "forecast_value_px_h": 0.055,
    "forecast_eta_gap_w": 0.025,
    "forecast_rule_gap_w": 0.03,

    # Chart
    "chart_top_h": 0.53,
    "chart_height_h": 0.20,
    "chart_axis_gap_h": 0.022,
    "chart_axis_px_h": 0.026,
    "chart_band_label_px_h": 0.15,       # of chart height
    "chart_band_label_px_min": 10,
    "chart_band_label_inset": 5,
    "chart_band_thin_ratio": 2.4,        # skip the low label under this
    "chart_now_dash_on": 4,
    "chart_now_dash_period": 9,
    "chart_dot_r_h": 0.022,              # of chart height
    "chart_dot_r_px": 1.6,
    "chart_now_r_h": 0.075,
    "chart_now_r_px": 4,

    # Stats row: IOB, COB, CARBS, BOLUS
    "stats_label_y_h": 0.815,
    "stats_label_px_h": 0.028,
    "stats_value_px_h": 0.082,
    "stats_value_gap_h": 0.048,
    "stats_sub_px_h": 0.024,
    "stats_sub_gap_h": 0.1,
    "stats_unit_ratio": 0.5,             # unit suffix, of the value size
    "stats_unit_gap": 0.08,

    # The urgent border
    "urgent_inset": 6,
    "urgent_width": 3,
    "urgent_radius": 12,

    # Footer
    "footer_h": 0.072,                   # of full height
    "footer_h_px": 26,
    "footer_pad_w": 0.028,               # of full width
    "footer_px_h": 0.30,                 # of footer height
    "footer_px_min": 11,
    "footer_icon_r_h": 0.14,
    "footer_icon_r_px": 6,
    "footer_mark_px_ratio": 0.92,
    "footer_mark_px_min": 9,
    "footer_mark_size_ratio": 1.35,
    "footer_glyph_advance": 0.85,        # mono glyph + tracking, of px
    "footer_update_gap": 2.2,            # of px, after the clock

    # Touch targets, packed in from the right of the footer
    "toggle_w_px": 120,
    "toggle_w_ratio": 11,                # of footer px
    "toggle_h_px": 44,
    "toggle_h_ratio": 2,                 # of footer height
    "qr_w_px": 104,
    "qr_w_ratio": 10,
    "qr_min_left_w": 0.34,               # QR gives way inside this much footer
    "qr_glyph_cell_h": 0.5,              # of footer height, across 7 cells
    "qr_glyph_cell_px": 2,

    # Label letterspacing, as a fraction of the type size
    "label_tracking": 0.22,
}

# The small-caps QR mark drawn in the footer: three finder squares and
# enough specks to read as a QR code at fourteen pixels.
QR_GLYPH = (
    "XXX.XXX",
    "X.X.X.X",
    "XXX.XXX",
    "..X.X..",
    "XXX..XX",
    "X.X.X.X",
    "XXX..X.",
)

QR_OPEN_SECONDS = 120                  # how long the settings QR stays up
TAP_DEBOUNCE_SECONDS = 0.5
TAP_FLASH_SECONDS = 0.35


# ---------------------------------------------------------------- network ----
#
# The setup hotspot is the same network, at the same address, on both
# devices: the captive-portal DNS rule the image installs points at
# HOTSPOT_ADDR, and a phone that has joined one product's hotspot before
# should find the other exactly where it expects.

HOTSPOT_SSID = "GlucoCube-Setup"
HOTSPOT_ADDR = "10.42.0.1"
MDNS_HOSTNAME = "glucocube"

# How the watcher decides a device has fallen off the network. A device
# with no saved network raises the hotspot after a single failure — there
# is nothing to wait for — while one that has been on a network before
# gives it three tries before taking the dashboard away.
NET_CHECK_SECONDS = 30
NET_FAILS_NEEDED = 3
NET_FIRST_CHECK_DELAY = 5
NET_SCAN_REFRESH_SECONDS = 300

# What each platform fetches to decide whether it is behind a captive
# portal. Answering these is what makes a phone open the setup page by
# itself instead of waiting to be told to.
CAPTIVE_PROBE_PATHS = (
    "/generate_204", "/gen_204",                     # Android, Chrome OS
    "/mobile/status.php",                            # older Android
    "/hotspot-detect.html", "/hotspotdetect.html",   # iOS, macOS
    "/library/test/success.html",                    # iOS, older
    "/success.txt", "/canonical.html",               # Firefox, NetworkManager
    "/ncsi.txt", "/connecttest.txt", "/redirect",    # Windows
    "/nmcheck.gnome.org",                            # GNOME
)

# Paths that answer normally even from a captive browser, or the portal
# page would redirect to itself.
CAPTIVE_ALWAYS_SERVE = ("/setup", "/screen.png", "/fonts/", "/api/")


# ------------------------------------------------------------------- QR ----
#
# The codes on screen carry a login with them, so they are read once, at an
# angle, across a room. Error-correction level M and a two-module quiet zone
# are what makes that reliable; the firmware's encoder has to be configured
# the same way or the two products print different codes for one URL.

QR_ERROR_CORRECTION = "M"
QR_BORDER_MODULES = 2


# --------------------------------------------------------------- releases ----

REPO = "The-Carted-Horse/SugarCube"
UPDATE_CHANNELS = ("stable", "beta")
CHANNEL_LABELS = {"stable": "Standard", "beta": "Beta"}
FORCE_MARKER = "[force-update]"
UPDATE_CHECK_HOURS = 6

# Board profiles the firmware builds for. The id is what appears in a
# release asset name (glucocube-esp32-<id>-<version>.bin) and in the
# ESP Web Tools manifest, so it is part of the published interface.
ESP32_BOARDS = (
    {
        "id": "esp32-8048s050",
        "name": "Sunton ESP32-8048S050",
        "chip": "esp32s3",
        "width": 800,
        "height": 480,
        "flash_mb": 16,
        "psram_mb": 8,
    },
    {
        "id": "sugarcube-s3",
        "name": "SugarCube ESP32-S3",
        "chip": "esp32s3",
        "width": 800,
        "height": 480,
        "flash_mb": 16,
        "psram_mb": 8,
    },
)
