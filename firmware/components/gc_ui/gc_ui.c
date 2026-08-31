/*
 * The dashboard, drawn into an RGB565 frame the panel scans out.
 *
 * This is glucocube/display.py's split screen in C. Read the two side by
 * side: the same regions in the same order, from the same GC_L_* fractions,
 * so that at 800x480 the ESP32 and the Raspberry Pi put the same pixel in
 * the same place rather than merely resembling each other.
 *
 * LVGL is used for what it is good at here — antialiased primitives, alpha
 * blending, and rasterising the two bundled typefaces at whatever size the
 * layout fractions work out to. It is not used as a widget toolkit: there
 * is one canvas, and every frame is drawn into it from scratch, which is
 * how the Python does it and what makes the two comparable at all. No
 * lv_timer_handler runs; nothing here is event-driven.
 *
 * Type sizes are computed at runtime rather than baked, because they come
 * from the panel's height and the number of people sharing it — a
 * pre-generated bitmap font would have to guess both.
 */

#include "gc_ui.h"

#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lvgl.h"
#include "qrcodegen.h"

#include "gc_board.h"
#include "gc_predict.h"

static const char *TAG = "gc_ui";

/* The bundled typefaces, embedded from glucocube/fonts/ — the same files
 * the Pi renders with, carried once in the repository rather than copied
 * into the firmware. */
extern const uint8_t space_grotesk_bold_start[] asm("_binary_SpaceGrotesk_Bold_ttf_start");
extern const uint8_t space_grotesk_bold_end[] asm("_binary_SpaceGrotesk_Bold_ttf_end");
extern const uint8_t space_grotesk_medium_start[] asm("_binary_SpaceGrotesk_Medium_ttf_start");
extern const uint8_t space_grotesk_medium_end[] asm("_binary_SpaceGrotesk_Medium_ttf_end");
extern const uint8_t jetbrains_regular_start[] asm("_binary_JetBrainsMono_Regular_ttf_start");
extern const uint8_t jetbrains_regular_end[] asm("_binary_JetBrainsMono_Regular_ttf_end");
extern const uint8_t jetbrains_medium_start[] asm("_binary_JetBrainsMono_Medium_ttf_start");
extern const uint8_t jetbrains_medium_end[] asm("_binary_JetBrainsMono_Medium_ttf_end");

typedef enum {
    FONT_NUM,        /* Space Grotesk Bold — the reading, the stat values */
    FONT_NUM_MED,    /* Space Grotesk Medium — the delta */
    FONT_MONO,       /* JetBrains Mono — labels, badges, the footer */
    FONT_MONO_MED,
    FONT_KIND_COUNT,
} font_kind_t;

/* One face per (kind, size). display.py caches the same way and for the
 * same reason: rasterising 147px digits is not something to do twice. */
#define FONT_CACHE_SIZE 24

typedef struct {
    font_kind_t kind;
    int px;
    lv_font_t *font;
} font_entry_t;

typedef struct {
    int x, y, w, h;
} rect_t;

static struct {
    bool ready;
    uint16_t *frame;
    lv_obj_t *canvas;
    lv_display_t *display;

    gc_config_t config;
    gc_theme_t theme;
    gc_screen_t screen;

    font_entry_t fonts[FONT_CACHE_SIZE];
    int font_count;

    /* Touch targets, recomputed every frame from the footer geometry —
     * display.py derives them the same way, because a tap is tested before
     * the frame is drawn and must not depend on text measured during the
     * previous one. */
    rect_t qr_rect;
    rect_t toggle_rect;

    int64_t qr_open_until_ms;
    int64_t tap_flash_ms;
    rect_t flash_rect;
    int64_t last_toggle_ms;

    char settings_url[256];
    char setup_url[256];
    char setup_caption[96];
    char hotspot_ssid[GC_MAX_SSID];
    char hotspot_password[64];
    /* Long enough for "2.14.0-rc.12" several times over; gc_ota owns the
     * real limit, and this does not depend on it just to size a string. */
    char pending_update[32];

    lv_layer_t layer;
} S;

/* Milliseconds since boot. Every deadline in this file is an elapsed time
 * — how long the QR code stays up, how long a tap stays lit — and elapsed
 * times cannot be measured on a clock that SNTP is going to move. */
static int64_t uptime_ms(void)
{
    return (int64_t)(esp_timer_get_time() / 1000);
}

/* ------------------------------------------------------------ colours -- */

static lv_color_t colour(gc_color_t role)
{
    const uint32_t rgb = gc_color888(S.theme, role);
    return lv_color_make((uint8_t)(rgb >> 16), (uint8_t)(rgb >> 8), (uint8_t)rgb);
}

/* The colour a reading is drawn in — glucose_color() in display.py, and the
 * one piece of logic here that decides what a person concludes at a glance,
 * so the order of the comparisons matters: urgent wins over merely low. */
static gc_color_t glucose_role(bool has_sgv, float sgv, bool stale,
                               const gc_thresholds_t *th)
{
    if (!has_sgv || stale) {
        return GC_C_STALE;
    }
    if (sgv <= th->urgent_low || sgv >= th->urgent_high) {
        return GC_C_URGENT;
    }
    if (sgv < th->low) {
        return GC_C_LOW;
    }
    if (sgv > th->high) {
        return GC_C_HIGH;
    }
    return GC_C_IN_RANGE;
}

/* --------------------------------------------------------------- type -- */

static const lv_font_t *font_for(font_kind_t kind, int px)
{
    if (px < 6) {
        px = 6;
    }
    for (int i = 0; i < S.font_count; i++) {
        if (S.fonts[i].kind == kind && S.fonts[i].px == px) {
            return S.fonts[i].font;
        }
    }
    const uint8_t *data = NULL;
    size_t size = 0;
    switch (kind) {
    case FONT_NUM:
        data = space_grotesk_bold_start;
        size = (size_t)(space_grotesk_bold_end - space_grotesk_bold_start);
        break;
    case FONT_NUM_MED:
        data = space_grotesk_medium_start;
        size = (size_t)(space_grotesk_medium_end - space_grotesk_medium_start);
        break;
    case FONT_MONO_MED:
        data = jetbrains_medium_start;
        size = (size_t)(jetbrains_medium_end - jetbrains_medium_start);
        break;
    case FONT_MONO:
    default:
        data = jetbrains_regular_start;
        size = (size_t)(jetbrains_regular_end - jetbrains_regular_start);
        break;
    }

    lv_font_t *font = lv_tiny_ttf_create_data(data, size, px);
    if (font == NULL) {
        ESP_LOGE(TAG, "could not rasterise a %dpx face", px);
        return NULL;
    }
    if (S.font_count >= FONT_CACHE_SIZE) {
        /* The cache is sized for the faces one layout needs. Overflowing it
         * means the layout changed, not that a face is unusual, so drop the
         * oldest rather than growing without bound. */
        lv_tiny_ttf_destroy(S.fonts[0].font);
        memmove(&S.fonts[0], &S.fonts[1],
                sizeof(font_entry_t) * (FONT_CACHE_SIZE - 1));
        S.font_count = FONT_CACHE_SIZE - 1;
    }
    S.fonts[S.font_count].kind = kind;
    S.fonts[S.font_count].px = px;
    S.fonts[S.font_count].font = font;
    S.font_count++;
    return font;
}

typedef enum {
    ALIGN_TOPLEFT,
    ALIGN_TOPRIGHT,
    ALIGN_MIDLEFT,
    ALIGN_MIDRIGHT,
    ALIGN_MIDTOP,
    ALIGN_BOTTOMRIGHT,
} anchor_t;

/* Draws text anchored the way pygame's Rect keywords anchor it, and reports
 * the rectangle it covered — callers lay the next thing out against it,
 * exactly as the Python does. */
static rect_t draw_text(const char *text, int px, font_kind_t kind,
                        gc_color_t role, int x, int y, anchor_t anchor,
                        int tracking_px)
{
    rect_t out = {x, y, 0, 0};
    const lv_font_t *font = font_for(kind, px);
    if (font == NULL || text == NULL || *text == '\0') {
        return out;
    }
    lv_point_t size;
    lv_text_get_size(&size, text, font, tracking_px, 0, LV_COORD_MAX,
                     LV_TEXT_FLAG_NONE);
    out.w = size.x;
    out.h = size.y;

    switch (anchor) {
    case ALIGN_TOPLEFT:                                   break;
    case ALIGN_TOPRIGHT:    out.x = x - size.x;           break;
    case ALIGN_MIDLEFT:     out.y = y - size.y / 2;       break;
    case ALIGN_MIDRIGHT:    out.x = x - size.x;
                            out.y = y - size.y / 2;       break;
    case ALIGN_MIDTOP:      out.x = x - size.x / 2;       break;
    case ALIGN_BOTTOMRIGHT: out.x = x - size.x;
                            out.y = y - size.y;           break;
    }

    lv_draw_label_dsc_t dsc;
    lv_draw_label_dsc_init(&dsc);
    dsc.text = text;
    dsc.font = font;
    dsc.color = colour(role);
    dsc.letter_space = tracking_px;
    dsc.align = LV_TEXT_ALIGN_LEFT;

    lv_area_t area = {out.x, out.y, out.x + size.x, out.y + size.y};
    lv_draw_label(&S.layer, &dsc, &area);
    return out;
}

/* The design's small-caps labels: uppercase, letterspaced by a fraction of
 * the type size. LVGL's letter_space does what display.py's per-glyph blit
 * loop does, so this is one call rather than that loop. */
static rect_t draw_label(const char *text, int px, gc_color_t role,
                         int x, int y, anchor_t anchor, font_kind_t kind)
{
    char upper[160];
    size_t i = 0;
    for (; text != NULL && text[i] != '\0' && i + 1 < sizeof(upper); i++) {
        char c = text[i];
        upper[i] = (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
    }
    upper[i] = '\0';
    return draw_text(upper, px, kind, role, x, y, anchor,
                     (int)(px * GC_L_LABEL_TRACKING));
}

/* ---------------------------------------------------------- primitives -- */

static void fill_rect(int x, int y, int w, int h, gc_color_t role,
                      lv_opa_t opa, int radius)
{
    if (w <= 0 || h <= 0) {
        return;
    }
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_color = colour(role);
    dsc.bg_opa = opa;
    dsc.radius = radius;
    dsc.border_width = 0;
    lv_area_t area = {x, y, x + w - 1, y + h - 1};
    lv_draw_rect(&S.layer, &dsc, &area);
}

static void stroke_rect(int x, int y, int w, int h, gc_color_t role,
                        int width, int radius)
{
    if (w <= 0 || h <= 0) {
        return;
    }
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_opa = LV_OPA_TRANSP;
    dsc.border_color = colour(role);
    dsc.border_opa = LV_OPA_COVER;
    dsc.border_width = width;
    dsc.radius = radius;
    lv_area_t area = {x, y, x + w - 1, y + h - 1};
    lv_draw_rect(&S.layer, &dsc, &area);
}

static void draw_line(float x0, float y0, float x1, float y1,
                      gc_color_t role, int width, lv_opa_t opa)
{
    lv_draw_line_dsc_t dsc;
    lv_draw_line_dsc_init(&dsc);
    dsc.color = colour(role);
    dsc.width = width;
    dsc.opa = opa;
    dsc.round_start = 1;
    dsc.round_end = 1;
    dsc.p1.x = x0;
    dsc.p1.y = y0;
    dsc.p2.x = x1;
    dsc.p2.y = y1;
    lv_draw_line(&S.layer, &dsc);
}

static void fill_circle(float cx, float cy, float r, gc_color_t role,
                        lv_opa_t opa)
{
    if (r < 0.5f) {
        r = 0.5f;
    }
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_color = colour(role);
    dsc.bg_opa = opa;
    dsc.radius = LV_RADIUS_CIRCLE;
    dsc.border_width = 0;
    lv_area_t area = {
        (int32_t)(cx - r), (int32_t)(cy - r),
        (int32_t)(cx + r), (int32_t)(cy + r),
    };
    lv_draw_rect(&S.layer, &dsc, &area);
}

static void fill_triangle(float ax, float ay, float bx, float by,
                          float cx, float cy, gc_color_t role)
{
    lv_draw_triangle_dsc_t dsc;
    lv_draw_triangle_dsc_init(&dsc);
    dsc.bg_color = colour(role);
    dsc.bg_opa = LV_OPA_COVER;
    dsc.p[0].x = ax;
    dsc.p[0].y = ay;
    dsc.p[1].x = bx;
    dsc.p[1].y = by;
    dsc.p[2].x = cx;
    dsc.p[2].y = cy;
    lv_draw_triangle(&S.layer, &dsc);
}

/* ----------------------------------------------------------- the arrow -- */

static void draw_arrow(float cx, float cy, float size, const char *direction,
                       gc_color_t role)
{
    const gc_direction_t *info = gc_direction_lookup(direction);
    if (info == NULL) {
        return;
    }
    const float rad = (float)(info->angle_deg * M_PI / 180.0);
    const float cos_a = cosf(rad), sin_a = sinf(rad);
    const float half = size / 2.0f;

    /* A double arrow is two of the same arrow, offset perpendicular to the
     * direction it points. */
    const float perp_x = -sin_a, perp_y = cos_a;
    float offsets[2] = {0.0f, 0.0f};
    int count = 1;
    if (info->heads == 2) {
        offsets[0] = -size * 0.32f;
        offsets[1] = size * 0.32f;
        count = 2;
    }

    for (int i = 0; i < count; i++) {
        const float ox = cx + perp_x * offsets[i];
        const float oy = cy + perp_y * offsets[i];
#define ROT_X(px, py) (ox + (px) * cos_a - (py) * sin_a)
#define ROT_Y(px, py) (oy + (px) * sin_a + (py) * cos_a)
        const int shaft = (int)(size * 0.14f) < 2 ? 2 : (int)(size * 0.14f);
        draw_line(ROT_X(-half, 0), ROT_Y(-half, 0),
                  ROT_X(half * 0.45f, 0), ROT_Y(half * 0.45f, 0),
                  role, shaft, LV_OPA_COVER);
        fill_triangle(ROT_X(half, 0), ROT_Y(half, 0),
                      ROT_X(half * 0.25f, -half * 0.55f),
                      ROT_Y(half * 0.25f, -half * 0.55f),
                      ROT_X(half * 0.25f, half * 0.55f),
                      ROT_Y(half * 0.25f, half * 0.55f), role);
#undef ROT_X
#undef ROT_Y
    }
}

/* An isometric cube: a hexagon with three edges to the near corner. Drawn
 * rather than blitted, like the sun and moon, so it stays crisp at any size
 * and adds no asset to carry around. */
static void draw_logo(float cx, float cy, float size, gc_color_t role)
{
    const float radius = size / 2.0f;
    float px[6], py[6];
    for (int i = 0; i < 6; i++) {
        const float angle = (float)((60.0 * i - 90.0) * M_PI / 180.0);
        px[i] = cx + cosf(angle) * radius;
        py[i] = cy + sinf(angle) * radius;
    }
    const int width = (int)(size * 0.09f) < 1 ? 1 : (int)(size * 0.09f);
    for (int i = 0; i < 6; i++) {
        draw_line(px[i], py[i], px[(i + 1) % 6], py[(i + 1) % 6],
                  role, width, LV_OPA_COVER);
    }
    /* Vertices 1, 3 and 5 are the three edges meeting at the near corner —
     * the lines that turn a hexagon into a cube. */
    for (int i = 1; i < 6; i += 2) {
        draw_line(cx, cy, px[i], py[i], role, width, LV_OPA_COVER);
    }
}

/* --------------------------------------------------------------- units -- */

/* units.py's fmt(), which is what a person actually reads. Everything
 * stored, compared and forecast is mg/dL; this is the last step before a
 * number reaches the screen. */
static void format_reading(char *out, size_t len, bool has_value, float mgdl)
{
    if (!has_value) {
        snprintf(out, len, "---");
        return;
    }
    if (S.config.display.mmol) {
        snprintf(out, len, "%.1f", (double)(mgdl / GC_MGDL_PER_MMOL));
    } else {
        snprintf(out, len, "%.0f", (double)mgdl);
    }
}

static void format_delta(char *out, size_t len, float mgdl)
{
    if (S.config.display.mmol) {
        snprintf(out, len, "%+.1f", (double)(mgdl / GC_MGDL_PER_MMOL));
    } else {
        snprintf(out, len, "%+.0f", (double)mgdl);
    }
}

static const char *units_label(void)
{
    return S.config.display.mmol ? "MMOL/L" : "MG/DL";
}

/* age_compact() — 'NOW', '4M', '1H07M', '2D'. */
static void format_age(char *out, size_t len, int64_t now_ms, int64_t then_ms,
                       bool known)
{
    if (!known || then_ms <= 0) {
        snprintf(out, len, "--");
        return;
    }
    const long minutes = (long)((now_ms - then_ms) / 60000);
    if (minutes < 1) {
        snprintf(out, len, "NOW");
    } else if (minutes < 60) {
        snprintf(out, len, "%ldM", minutes);
    } else if (minutes < 24 * 60) {
        snprintf(out, len, "%ldH%02ldM", minutes / 60, minutes % 60);
    } else {
        snprintf(out, len, "%ldD", minutes / (24 * 60));
    }
}

/* --------------------------------------------------------------- chart -- */

static void draw_chart(rect_t chart, const gc_snapshot_t *snap, bool stale,
                       const gc_thresholds_t *th, const gc_forecast_t *forecast,
                       int64_t now_ms)
{
    const int64_t t0 = now_ms - (int64_t)GC_CHART_HISTORY_MINUTES * 60000;
    const int64_t t1 = now_ms + (int64_t)GC_CHART_FORECAST_MINUTES * 60000;

    float lo = th->low, hi = th->high;
    for (int i = 0; i < snap->history_count; i++) {
        if (snap->history[i].value < lo) lo = snap->history[i].value;
        if (snap->history[i].value > hi) hi = snap->history[i].value;
    }
    if (forecast != NULL && forecast->valid) {
        for (int i = 0; i < forecast->series_count; i++) {
            if (forecast->series[i].value < lo) lo = forecast->series[i].value;
            if (forecast->series[i].value > hi) hi = forecast->series[i].value;
        }
    }
    lo -= GC_CHART_PAD_BELOW;
    hi += GC_CHART_PAD_ABOVE;
    if (hi - lo < 1.0f) {
        hi = lo + 1.0f;   /* a flat chart is still a chart, not a divide by zero */
    }

#define CHART_X(t) (chart.x + (float)((double)((t) - t0) / (double)(t1 - t0)) * chart.w)
#define CHART_Y(v) (chart.y + chart.h - ((v) - lo) / (hi - lo) * chart.h)

    /* Target-range band, with its bounds written at the right edge. */
    const int band_top = (int)CHART_Y(th->high);
    const int band_bottom = (int)CHART_Y(th->low);
    fill_rect(chart.x, band_top, chart.w, band_bottom - band_top,
              GC_C_BAND, LV_OPA_COVER, 0);

    int label_px = (int)(chart.h * GC_L_CHART_BAND_LABEL_PX_H);
    if (label_px < GC_L_CHART_BAND_LABEL_PX_MIN) {
        label_px = GC_L_CHART_BAND_LABEL_PX_MIN;
    }
    char text[24];
    const int inset = GC_L_CHART_BAND_LABEL_INSET;
    format_reading(text, sizeof(text), true, th->high);
    draw_text(text, label_px, FONT_MONO, GC_C_FAINT,
              chart.x + chart.w - inset, band_top + 2, ALIGN_TOPRIGHT, 0);
    if (band_bottom - band_top > label_px * GC_L_CHART_BAND_THIN_RATIO) {
        /* Skip the lower bound when the band is too thin to hold it. */
        format_reading(text, sizeof(text), true, th->low);
        draw_text(text, label_px, FONT_MONO, GC_C_FAINT,
                  chart.x + chart.w - inset, band_bottom - 2,
                  ALIGN_BOTTOMRIGHT, 0);
    }

    /* Dashed "now" divider between the measured past and the forecast. */
    const float x_now = CHART_X(now_ms);
    for (int y = chart.y; y < chart.y + chart.h;
         y += GC_L_CHART_NOW_DASH_PERIOD) {
        int end = y + GC_L_CHART_NOW_DASH_ON;
        if (end > chart.y + chart.h) {
            end = chart.y + chart.h;
        }
        draw_line(x_now, y, x_now, end, GC_C_LINE, 1, LV_OPA_COVER);
    }

    const gc_color_t now_role =
        glucose_role(snap->has_sgv, snap->sgv, stale, th);

    /* The forecast's confidence cone. The Python fills one polygon; here it
     * is a column per forecast point, which blends to the same shape and
     * avoids building a vertex list for a shape that is always a ribbon. */
    if (forecast != NULL && forecast->valid && forecast->series_count >= 2) {
        const float rate = forecast->estimated ? GC_CONE_RATE_ESTIMATE
                                               : GC_CONE_RATE_DEVICE;
        const gc_color_t cone_role = glucose_role(
            true, forecast->series[forecast->series_count - 1].value, false, th);
        for (int i = 0; i + 1 < forecast->series_count; i++) {
            for (int step = 0; step < 2; step++) {
                const gc_point_t *p = &forecast->series[i + step];
                const float spread =
                    GC_CONE_BASE_SPREAD
                    + (float)((p->ms - now_ms) / 60000.0) * rate;
                float top = CHART_Y(p->value + spread);
                float bottom = CHART_Y(p->value - spread);
                if (top < chart.y) top = chart.y;
                if (bottom > chart.y + chart.h) bottom = chart.y + chart.h;
                const float x = CHART_X(p->ms);
                const float next_x = CHART_X(forecast->series[i + 1].ms);
                int width = (int)(next_x - x);
                if (width < 1) {
                    width = 1;
                }
                if (bottom > top) {
                    fill_rect((int)x, (int)top, width, (int)(bottom - top),
                              cone_role, GC_CONE_ALPHA, 0);
                }
                break;   /* one column per point; the pair above sets width */
            }
        }
    }

    /* History: a smooth neutral line — dots would imply a colour per
     * reading — split on gaps so a sensor outage stays visible. */
    const gc_color_t trace_role = stale ? GC_C_STALE : GC_C_TRACE;
    for (int i = 0; i + 1 < snap->history_count; i++) {
        const gc_point_t *a = &snap->history[i];
        const gc_point_t *b = &snap->history[i + 1];
        if (a->ms < t0) {
            continue;
        }
        if (b->ms - a->ms > GC_CHART_GAP_SPLIT_MS) {
            continue;
        }
        draw_line(CHART_X(a->ms), CHART_Y(a->value),
                  CHART_X(b->ms), CHART_Y(b->value), trace_role, 2,
                  LV_OPA_COVER);
    }

    /* Forecast: dots only, no line — clearly not measured data. */
    float dot_r = chart.h * GC_L_CHART_DOT_R_H;
    if (dot_r < GC_L_CHART_DOT_R_PX) {
        dot_r = GC_L_CHART_DOT_R_PX;
    }
    if (forecast != NULL && forecast->valid) {
        for (int i = 0; i < forecast->series_count; i++) {
            const gc_point_t *p = &forecast->series[i];
            fill_circle(CHART_X(p->ms), CHART_Y(p->value), dot_r,
                        glucose_role(true, p->value, false, th), LV_OPA_COVER);
        }
    }

    /* "Now": a soft halo and a solid dot at the latest reading. */
    if (snap->history_count > 0) {
        const gc_point_t *last = &snap->history[snap->history_count - 1];
        int r = (int)(chart.h * GC_L_CHART_NOW_R_H);
        if (r < GC_L_CHART_NOW_R_PX) {
            r = GC_L_CHART_NOW_R_PX;
        }
        const int64_t at = last->ms < now_ms ? last->ms : now_ms;
        const float cx = CHART_X(at);
        const float cy = CHART_Y(last->value);
        fill_circle(cx, cy, (float)(r * 2), now_role, GC_NOW_HALO_ALPHA);
        fill_circle(cx, cy, (float)r, now_role, LV_OPA_COVER);
    }

#undef CHART_X
#undef CHART_Y
}

/* --------------------------------------------------------------- panel -- */

static void draw_panel(rect_t rect, int user_index, const gc_snapshot_t *snap,
                       int64_t now_ms)
{
    const gc_user_config_t *user = &S.config.users[user_index];
    const gc_thresholds_t th = gc_merged_thresholds(&S.config, user_index);
    const int h = rect.h, w = rect.w;
    const int pad = (int)(w * GC_L_PANEL_PAD_W);
    const int left = rect.x + pad;
    const int right = rect.x + w - pad;

    const bool stale =
        !snap->has_sgv
        || (now_ms - snap->sgv_date) > (int64_t)(th.stale_minutes * 60000.0f);
    const gc_color_t role = glucose_role(snap->has_sgv, snap->sgv, stale, &th);

    /* Header: the name on the left; a freshness dot, the source and the
     * reading's age on the right. */
    const int top = rect.y + (int)(h * GC_L_HEADER_TOP_H);
    draw_label(user->name, (int)(h * GC_L_NAME_PX_H), GC_C_FG,
               left, top, ALIGN_TOPLEFT, FONT_MONO_MED);

    const double age_min =
        snap->has_sgv ? (double)(now_ms - snap->sgv_date) / 60000.0 : -1.0;
    gc_color_t dot_role = GC_C_LOW;
    if (age_min >= 0.0 && age_min <= GC_FRESH_MINUTES) {
        dot_role = GC_C_IN_RANGE;
    } else if (age_min >= 0.0 && age_min <= th.stale_minutes) {
        dot_role = GC_C_HIGH;
    }
    char age[16];
    format_age(age, sizeof(age), now_ms, snap->sgv_date, snap->has_sgv);
    char badge[64];
    snprintf(badge, sizeof(badge), "%s · %s",
             gc_source_label(gc_source_kind_name(user->kind)), age);
    const rect_t badge_rect =
        draw_label(badge, (int)(h * GC_L_BADGE_PX_H), GC_C_DIM,
                   right, top + (int)(h * GC_L_BADGE_TOP_H), ALIGN_TOPRIGHT,
                   FONT_MONO);
    int dot_r = (int)(h * GC_L_BADGE_DOT_R_H);
    if (dot_r < GC_L_BADGE_DOT_R_PX) {
        dot_r = GC_L_BADGE_DOT_R_PX;
    }
    fill_circle((float)(badge_rect.x - (int)(h * GC_L_BADGE_DOT_GAP_H)),
                (float)(badge_rect.y + badge_rect.h / 2), (float)dot_r,
                dot_role, LV_OPA_COVER);

    /* The reading, left-aligned, with the trend column beside it. */
    char reading[24];
    format_reading(reading, sizeof(reading), snap->has_sgv, snap->sgv);
    int num_px = (int)(h * GC_L_NUM_PX_H);
    const lv_font_t *num_font = font_for(FONT_NUM, num_px);
    lv_point_t num_size = {0, 0};
    if (num_font != NULL) {
        lv_text_get_size(&num_size, reading, num_font, 0, 0, LV_COORD_MAX,
                         LV_TEXT_FLAG_NONE);
    }
    /* A narrow panel shrinks the number rather than letting it run into the
     * trend column. */
    const int max_num_w = (right - left) - (int)(w * GC_L_NUM_MAX_W_RESERVE_W);
    if (num_size.x > max_num_w && num_size.x > 0) {
        num_px = (int)((float)num_px * max_num_w / num_size.x);
        if (num_px < GC_L_NUM_PX_MIN) {
            num_px = GC_L_NUM_PX_MIN;
        }
        num_font = font_for(FONT_NUM, num_px);
        if (num_font != NULL) {
            lv_text_get_size(&num_size, reading, num_font, 0, 0, LV_COORD_MAX,
                             LV_TEXT_FLAG_NONE);
        }
    }

    /* Space Grotesk carries a lot of internal leading, so the digits are
     * placed by their cap top rather than by the font's line box — the same
     * correction display.py makes, for the same reason. */
    const int cap = (int)(num_px * GC_L_NUM_CAP_RATIO);
    const int num_top = rect.y + (int)(h * GC_L_NUM_TOP_H);
    int lead = 0;
    if (num_font != NULL) {
        const int ascent = num_font->line_height - num_font->base_line;
        lead = ascent - cap;
    }
    draw_text(reading, num_px, FONT_NUM, role,
              left - (int)(num_px * GC_L_NUM_LEFT_NUDGE), num_top - lead,
              ALIGN_TOPLEFT, 0);

    const int col_x = left + num_size.x + (int)(w * GC_L_TREND_GAP_W);
    if (!stale) {
        const int arrow_size = (int)(h * GC_L_ARROW_SIZE_H);
        draw_arrow((float)(col_x + arrow_size),
                   (float)num_top + cap * GC_L_ARROW_Y_CAP,
                   (float)arrow_size, snap->direction, role);
    }
    if (!stale && snap->has_delta) {
        char delta[16];
        format_delta(delta, sizeof(delta), snap->delta);
        draw_text(delta, (int)(h * GC_L_DELTA_PX_H), FONT_NUM_MED, GC_C_FG,
                  col_x, num_top + (int)(cap * GC_L_DELTA_Y_CAP),
                  ALIGN_TOPLEFT, 0);
    }
    draw_label(units_label(), (int)(h * GC_L_UNIT_PX_H), GC_C_FAINT,
               col_x, num_top + (int)(cap * GC_L_UNIT_Y_CAP), ALIGN_TOPLEFT,
               FONT_MONO);

    /* FORECAST 2H: label — rule — value and the time it arrives. */
    gc_forecast_t forecast;
    const bool have_forecast = !stale && gc_predict(snap, now_ms, &forecast);
    const int fy = rect.y + (int)(h * GC_L_FORECAST_Y_H);
    const int label_px = (int)(h * GC_L_FORECAST_LABEL_PX_H);
    char heading[24];
    snprintf(heading, sizeof(heading), "FORECAST %dH", GC_HORIZON_FAR / 60);
    const rect_t heading_rect =
        draw_label(heading, label_px, GC_C_DIM, left, fy, ALIGN_MIDLEFT,
                   FONT_MONO);

    const int rule_gap = (int)(w * GC_L_FORECAST_RULE_GAP_W);
    int rule_end = right;
    float far_value = 0.0f;
    if (have_forecast && gc_forecast_at(&forecast, GC_HORIZON_FAR, &far_value)) {
        const time_t eta = (time_t)((now_ms + (int64_t)GC_HORIZON_FAR * 60000)
                                    / 1000);
        struct tm tm_eta;
        localtime_r(&eta, &tm_eta);
        char when[16];
        strftime(when, sizeof(when), "%H:%M", &tm_eta);
        const rect_t eta_rect = draw_label(when, label_px, GC_C_FAINT,
                                           right, fy, ALIGN_MIDRIGHT, FONT_MONO);
        char value[24];
        char shown[26];
        format_reading(value, sizeof(value), true, far_value);
        /* An estimate of ours is marked; the pump's own curve is not. */
        snprintf(shown, sizeof(shown), "%s%s",
                 forecast.estimated ? "~" : "", value);
        const rect_t value_rect = draw_text(
            shown, (int)(h * GC_L_FORECAST_VALUE_PX_H), FONT_NUM,
            glucose_role(true, far_value, false, &th),
            eta_rect.x - (int)(w * GC_L_FORECAST_ETA_GAP_W), fy, ALIGN_MIDRIGHT,
            0);
        rule_end = value_rect.x - rule_gap;
    }
    draw_line((float)(heading_rect.x + heading_rect.w + rule_gap), (float)fy,
              (float)rule_end, (float)fy, GC_C_LINE, 1, LV_OPA_COVER);

    /* The chart, and its time axis. */
    const rect_t chart = {
        left, rect.y + (int)(h * GC_L_CHART_TOP_H),
        right - left, (int)(h * GC_L_CHART_HEIGHT_H),
    };
    draw_chart(chart, snap, stale, &th,
               have_forecast ? &forecast : NULL, now_ms);

    const int axis_y = chart.y + chart.h + (int)(h * GC_L_CHART_AXIS_GAP_H);
    const int span = GC_CHART_HISTORY_MINUTES + GC_CHART_FORECAST_MINUTES;
    for (int i = 0; i < GC_TICK_COUNT; i++) {
        const float x = chart.x
                        + (float)(gc_ticks[i].minutes + GC_CHART_HISTORY_MINUTES)
                              / span * chart.w;
        draw_label(gc_ticks[i].label, (int)(h * GC_L_CHART_AXIS_PX_H),
                   GC_C_FAINT, (int)x, axis_y, ALIGN_MIDTOP, FONT_MONO);
    }

    /* IOB, COB, last carbs, last bolus. */
    struct {
        const char *label;
        char value[16];
        const char *unit;
        char sub[24];
        bool has_sub;
    } stats[4];
    memset(stats, 0, sizeof(stats));

    stats[0].label = "IOB";
    stats[0].unit = snap->has_iob ? "U" : "";
    snprintf(stats[0].value, sizeof(stats[0].value), snap->has_iob ? "%.1f" : "--",
             (double)snap->iob);
    stats[1].label = "COB";
    stats[1].unit = snap->has_cob ? "G" : "";
    snprintf(stats[1].value, sizeof(stats[1].value), snap->has_cob ? "%.0f" : "--",
             (double)snap->cob);
    stats[2].label = "CARBS";
    stats[2].unit = snap->has_last_carbs ? "G" : "";
    snprintf(stats[2].value, sizeof(stats[2].value),
             snap->has_last_carbs ? "%.0f" : "--", (double)snap->last_carbs);
    if (snap->has_last_carbs) {
        char since[16];
        format_age(since, sizeof(since), now_ms, snap->last_carbs_date, true);
        snprintf(stats[2].sub, sizeof(stats[2].sub), "%s AGO", since);
        stats[2].has_sub = true;
    }
    stats[3].label = "BOLUS";
    stats[3].unit = snap->has_last_bolus ? "U" : "";
    snprintf(stats[3].value, sizeof(stats[3].value),
             snap->has_last_bolus ? "%.2f" : "--", (double)snap->last_bolus);
    if (snap->has_last_bolus) {
        char since[16];
        format_age(since, sizeof(since), now_ms, snap->last_bolus_date, true);
        snprintf(stats[3].sub, sizeof(stats[3].sub), "%s AGO", since);
        stats[3].has_sub = true;
    }

    const int labels_y = rect.y + (int)(h * GC_L_STATS_LABEL_Y_H);
    const float col_w = (float)(right - left) / 4.0f;
    const int value_px = (int)(h * GC_L_STATS_VALUE_PX_H);
    for (int i = 0; i < 4; i++) {
        const int x = left + (int)(i * col_w);
        draw_label(stats[i].label, (int)(h * GC_L_STATS_LABEL_PX_H), GC_C_DIM,
                   x, labels_y, ALIGN_TOPLEFT, FONT_MONO);
        const int value_y = labels_y + (int)(h * GC_L_STATS_VALUE_GAP_H);
        const rect_t value_rect = draw_text(stats[i].value, value_px, FONT_NUM,
                                            GC_C_FG, x, value_y, ALIGN_TOPLEFT, 0);
        if (stats[i].unit != NULL && stats[i].unit[0] != '\0') {
            /* The unit suffix sits on the value's baseline, a size down and
             * a shade back — it is a unit, not a second number. */
            const int unit_px = (int)(value_px * GC_L_STATS_UNIT_RATIO);
            const lv_font_t *vf = font_for(FONT_NUM, value_px);
            const lv_font_t *uf = font_for(FONT_NUM, unit_px);
            int baseline_offset = 0;
            if (vf != NULL && uf != NULL) {
                baseline_offset = (vf->line_height - vf->base_line)
                                  - (uf->line_height - uf->base_line);
            }
            draw_text(stats[i].unit, unit_px, FONT_NUM, GC_C_DIM,
                      x + value_rect.w + (int)(value_px * GC_L_STATS_UNIT_GAP),
                      value_y + baseline_offset, ALIGN_TOPLEFT, 0);
        }
        if (stats[i].has_sub) {
            draw_label(stats[i].sub, (int)(h * GC_L_STATS_SUB_PX_H), GC_C_FAINT,
                       x, value_y + (int)(h * GC_L_STATS_SUB_GAP_H),
                       ALIGN_TOPLEFT, FONT_MONO);
        }
    }

    /* An urgent reading gets a border, to catch the eye across a room. */
    if (role == GC_C_URGENT) {
        const int inset = GC_L_URGENT_INSET;
        stroke_rect(rect.x + inset / 2, rect.y + inset / 2,
                    rect.w - inset, rect.h - inset, GC_C_URGENT,
                    GC_L_URGENT_WIDTH, GC_L_URGENT_RADIUS);
    }
}

/* -------------------------------------------------------------- footer -- */

static rect_t footer_rect(void)
{
    const int full_h = gc_board_height();
    int h = (int)(full_h * GC_L_FOOTER_H);
    if (h < GC_L_FOOTER_H_PX) {
        h = GC_L_FOOTER_H_PX;
    }
    rect_t r = {0, full_h - h, gc_board_width(), h};
    return r;
}

static int footer_px(rect_t footer)
{
    int px = (int)(footer.h * GC_L_FOOTER_PX_H);
    return px < GC_L_FOOTER_PX_MIN ? GC_L_FOOTER_PX_MIN : px;
}

/* The two touch targets, packed in from the right. The QR control gives way
 * on a screen too narrow to hold it and the clock: settings are still
 * reachable from any phone on the network, and a control overlapping the
 * date is worse than no control.
 *
 * Both targets are deliberately taller than the footer and overhang the
 * bottom of the panel, which is how they clear a fingertip on a 34px strip.
 * SDL on the Pi passes coordinates through unclamped; a touch controller
 * clamps to the panel, so what is actually reachable here is the top half —
 * 51px, still over the 44px a finger needs, which
 * tests/test_contract.py asserts rather than leaves to chance. */
static void compute_controls(rect_t footer, rect_t *qr, rect_t *toggle)
{
    const int px = footer_px(footer);

    int tw = px * GC_L_TOGGLE_W_RATIO;
    if (tw < GC_L_TOGGLE_W_PX) {
        tw = GC_L_TOGGLE_W_PX;
    }
    int thh = footer.h * GC_L_TOGGLE_H_RATIO;
    if (thh < GC_L_TOGGLE_H_PX) {
        thh = GC_L_TOGGLE_H_PX;
    }
    toggle->w = tw;
    toggle->h = thh;
    toggle->x = footer.x + footer.w - tw;
    toggle->y = footer.y + footer.h / 2 - thh / 2;
    if (toggle->y < 0) {
        toggle->y = 0;
    }

    int qw = px * GC_L_QR_W_RATIO;
    if (qw < GC_L_QR_W_PX) {
        qw = GC_L_QR_W_PX;
    }
    qr->w = qw;
    qr->h = thh;
    qr->x = toggle->x - qw;
    qr->y = toggle->y;
    if (qr->x < footer.x + (int)(footer.w * GC_L_QR_MIN_LEFT_W)) {
        qr->w = qr->h = 0;
    }
}

static void draw_qr_button(rect_t footer, rect_t qr, int px)
{
    int cell = (int)(footer.h * GC_L_QR_GLYPH_CELL_H) / GC_QR_GLYPH_ROWS;
    if (cell < GC_L_QR_GLYPH_CELL_PX) {
        cell = GC_L_QR_GLYPH_CELL_PX;
    }
    const int size = cell * GC_QR_GLYPH_ROWS;
    const int box_right = qr.x + qr.w - (int)(px * 0.7f);
    const int box_left = box_right - size;
    const int box_top = footer.y + footer.h / 2 - size / 2;

    for (int y = 0; y < GC_QR_GLYPH_ROWS; y++) {
        for (int x = 0; x < GC_QR_GLYPH_COLS; x++) {
            if (gc_qr_glyph[y][x] == 'X') {
                fill_rect(box_left + x * cell, box_top + y * cell, cell, cell,
                          GC_C_DIM, LV_OPA_COVER, 0);
            }
        }
    }
    const int label_right = box_left - (int)(px * 0.8f);
    if (label_right - (int)(8 * px * GC_L_FOOTER_GLYPH_ADVANCE) >= qr.x) {
        draw_label("SETTINGS", px, GC_C_DIM, label_right,
                   footer.y + footer.h / 2, ALIGN_MIDRIGHT, FONT_MONO);
    }
}

static void draw_footer(rect_t footer, int64_t now_ms)
{
    draw_line((float)footer.x, (float)footer.y,
              (float)(footer.x + footer.w), (float)footer.y,
              GC_C_LINE, 1, LV_OPA_COVER);

    /* How long ago the tap was, not what time it was: the wall clock jumps
     * by decades the first time SNTP lands, and an elapsed time measured
     * across that jump is either zero or forever. */
    if (uptime_ms() - S.tap_flash_ms < (int64_t)(GC_TAP_FLASH_SECONDS * 1000)
        && S.flash_rect.w > 0) {
        /* A momentary highlight, so a tap is visibly acknowledged on
         * whichever control was actually hit. */
        fill_rect(S.flash_rect.x + 1, S.flash_rect.y + 3,
                  S.flash_rect.w - 2, S.flash_rect.h - 6,
                  GC_C_BAND, LV_OPA_COVER, 8);
    }

    const int pad = (int)(gc_board_width() * GC_L_FOOTER_PAD_W);
    const int px = footer_px(footer);
    const int mid_y = footer.y + footer.h / 2;

    const time_t now = (time_t)(now_ms / 1000);
    struct tm tm_now;
    localtime_r(&now, &tm_now);
    char when[48];
    strftime(when, sizeof(when),
             S.config.display.time_format == 12 ? "%a %d %b · %I:%M %p"
                                                : "%a %d %b · %H:%M",
             &tm_now);
    const rect_t when_rect = draw_label(when, px, GC_C_DIM, footer.x + pad,
                                        mid_y, ALIGN_MIDLEFT, FONT_MONO);

    bool showed_update = false;
    if (S.pending_update[0] != '\0') {
        char notice[64];
        snprintf(notice, sizeof(notice), "UPDATE %s", S.pending_update);
        const int notice_w =
            (int)(strlen(notice) * px * GC_L_FOOTER_GLYPH_ADVANCE);
        const int x = when_rect.x + when_rect.w
                      + (int)(px * GC_L_FOOTER_UPDATE_GAP);
        if (x + notice_w < footer.x + footer.w - px * GC_L_QR_W_RATIO) {
            draw_label(notice, px, GC_C_HIGH, x, mid_y, ALIGN_MIDLEFT,
                       FONT_MONO);
            showed_update = true;
        }
    }

    /* The mark sits in the middle and gives way to anything with something
     * to say: an update notice is actionable, this is decoration. */
    if (!showed_update) {
        int mark_px = (int)(px * GC_L_FOOTER_MARK_PX_RATIO);
        if (mark_px < GC_L_FOOTER_MARK_PX_MIN) {
            mark_px = GC_L_FOOTER_MARK_PX_MIN;
        }
        const int mark_size = (int)(mark_px * GC_L_FOOTER_MARK_SIZE_RATIO);
        const int mark_w = mark_size + (int)(mark_px * 0.6f)
                           + (int)(9 * mark_px * GC_L_FOOTER_GLYPH_ADVANCE);
        const int centre = footer.x + footer.w / 2;
        const int left_edge = centre - mark_w / 2;
        const int controls_left =
            S.qr_rect.w > 0 ? S.qr_rect.x : S.toggle_rect.x;
        if (left_edge > when_rect.x + when_rect.w + px
            && centre + mark_w / 2 < controls_left - px) {
            draw_logo((float)(left_edge + mark_size / 2), (float)mid_y,
                      (float)mark_size, GC_C_FAINT);
            draw_label("GlucoCube", mark_px, GC_C_FAINT,
                       left_edge + mark_size + (int)(mark_px * 0.6f), mid_y,
                       ALIGN_MIDLEFT, FONT_MONO);
        }
    }

    if (S.qr_rect.w > 0) {
        draw_qr_button(footer, S.qr_rect, px);
    }

    /* Sun or moon: whichever the tap would take you to. */
    int icon_r = (int)(footer.h * GC_L_FOOTER_ICON_R_H);
    if (icon_r < GC_L_FOOTER_ICON_R_PX) {
        icon_r = GC_L_FOOTER_ICON_R_PX;
    }
    const int icon_cx = footer.x + footer.w - pad - icon_r;
    draw_label(S.theme == GC_THEME_DARK ? "NIGHT" : "DAY", px, GC_C_DIM,
               icon_cx - icon_r - (int)(px * 0.9f), mid_y, ALIGN_MIDRIGHT,
               FONT_MONO);

    if (S.theme == GC_THEME_DARK) {
        fill_circle((float)icon_cx, (float)mid_y, icon_r * 0.55f, GC_C_DIM,
                    LV_OPA_COVER);
        fill_circle((float)icon_cx, (float)mid_y, icon_r * 0.55f - 2.0f,
                    GC_C_BG, LV_OPA_COVER);
        for (int i = 0; i < 8; i++) {
            const float angle = (float)(i * M_PI / 4.0);
            draw_line(icon_cx + cosf(angle) * icon_r * 0.75f,
                      mid_y + sinf(angle) * icon_r * 0.75f,
                      icon_cx + cosf(angle) * icon_r,
                      mid_y + sinf(angle) * icon_r, GC_C_DIM, 2, LV_OPA_COVER);
        }
    } else {
        fill_circle((float)icon_cx, (float)mid_y, icon_r * 0.8f, GC_C_DIM,
                    LV_OPA_COVER);
        fill_circle(icon_cx + icon_r * 0.45f, mid_y - icon_r * 0.3f,
                    icon_r * 0.7f, GC_C_BG, LV_OPA_COVER);
    }
}

/* ------------------------------------------------------------ QR codes -- */

/* A code big enough to read across a room, centred in `box`. */
static void draw_qr(const char *text, int cx, int cy, int target_px)
{
    if (text == NULL || *text == '\0') {
        return;
    }
    static uint8_t qr[qrcodegen_BUFFER_LEN_FOR_VERSION(11)];
    static uint8_t scratch[qrcodegen_BUFFER_LEN_FOR_VERSION(11)];
    /* Level M with a two-module quiet zone, which is what the Pi asks
     * python-qrcode for: the same URL has to produce the same code, because
     * the same phone scans both. */
    if (!qrcodegen_encodeText(text, scratch, qr, qrcodegen_Ecc_MEDIUM,
                              qrcodegen_VERSION_MIN, 11,
                              qrcodegen_Mask_AUTO, true)) {
        ESP_LOGW(TAG, "the URL is too long to put on the screen as a code");
        return;
    }
    const int modules = qrcodegen_getSize(qr);
    const int quiet = GC_QR_BORDER_MODULES;
    const int total = modules + quiet * 2;
    int scale = target_px / total;
    if (scale < 1) {
        scale = 1;
    }
    const int size = total * scale;
    const int x0 = cx - size / 2;
    const int y0 = cy - size / 2;

    /* The quiet zone has to be light whatever the theme, or a scanner
     * cannot find the code's edge. */
    fill_rect(x0, y0, size, size, GC_C_FG, LV_OPA_COVER, 0);
    for (int y = 0; y < modules; y++) {
        for (int x = 0; x < modules; x++) {
            if (qrcodegen_getModule(qr, x, y)) {
                fill_rect(x0 + (x + quiet) * scale, y0 + (y + quiet) * scale,
                          scale, scale, GC_C_BG, LV_OPA_COVER, 0);
            }
        }
    }
}

/* ------------------------------------------------------- other screens -- */

static void draw_centred_screen(const char *heading, const char *lede,
                                const char *url, const char *footnote)
{
    const int w = gc_board_width(), h = gc_board_height();
    const int s = w < h ? w : h;
    const int cx = w / 2;

    draw_text(heading, (int)(s * 0.075f), FONT_NUM, GC_C_FG, cx,
              (int)(h * 0.08f), ALIGN_MIDTOP, 0);
    if (lede != NULL && *lede != '\0') {
        draw_label(lede, (int)(s * 0.030f), GC_C_DIM, cx, (int)(h * 0.17f),
                   ALIGN_MIDTOP, FONT_MONO);
    }
    if (url != NULL && *url != '\0') {
        draw_qr(url, cx, (int)(h * 0.53f), (int)(h * 0.46f));
    }
    if (footnote != NULL && *footnote != '\0') {
        draw_label(footnote, (int)(s * 0.028f), GC_C_FAINT, cx,
                   (int)(h * 0.86f), ALIGN_MIDTOP, FONT_MONO);
    }
}

/* ---------------------------------------------------------------- draw -- */

static void draw_dashboard(gc_store_t *store, int64_t now_ms)
{
    const int full_w = gc_board_width();
    const int full_h = gc_board_height();
    const rect_t footer = footer_rect();
    compute_controls(footer, &S.qr_rect, &S.toggle_rect);

    const int people = S.config.user_count;
    const int body_h = footer.y;
    const bool portrait = full_h > full_w;

    for (int i = 0; i < people; i++) {
        rect_t panel;
        if (portrait) {
            const int row_h = body_h / people;
            panel = (rect_t){0, i * row_h, full_w, row_h};
            if (i > 0) {
                const int inset = (int)(full_w * GC_L_PANEL_DIVIDER_INSET_PORTRAIT);
                draw_line((float)inset, (float)panel.y,
                          (float)(full_w - inset), (float)panel.y,
                          GC_C_LINE, 1, LV_OPA_COVER);
            }
        } else {
            const int col_w = full_w / people;
            panel = (rect_t){i * col_w, 0, col_w, body_h};
            if (i > 0) {
                draw_line(
                    (float)panel.x,
                    (float)(int)(body_h * GC_L_PANEL_DIVIDER_INSET_LANDSCAPE),
                    (float)panel.x,
                    (float)(body_h
                            - (int)(body_h
                                    * GC_L_PANEL_DIVIDER_INSET_LANDSCAPE_BOTTOM)),
                    GC_C_LINE, 1, LV_OPA_COVER);
            }
        }

        gc_snapshot_t snap;
        if (gc_store_snapshot(store, i, now_ms, &snap)) {
            draw_panel(panel, i, &snap, now_ms);
        }
    }

    draw_footer(footer, now_ms);

    if (uptime_ms() < S.qr_open_until_ms && S.settings_url[0] != '\0') {
        /* Over the whole screen, not beside it: somebody has asked for this
         * and is holding a phone up to it. */
        fill_rect(0, 0, full_w, full_h, GC_C_BG, LV_OPA_COVER, 0);
        draw_centred_screen("Settings", "Scan to open on your phone",
                            S.settings_url, S.config.admin_password[0] != '\0'
                                                ? S.config.admin_password
                                                : NULL);
    }
}

esp_err_t gc_ui_draw(gc_store_t *store, int64_t now_ms)
{
    if (!S.ready) {
        return ESP_ERR_INVALID_STATE;
    }
    lv_canvas_init_layer(S.canvas, &S.layer);

    fill_rect(0, 0, gc_board_width(), gc_board_height(), GC_C_BG,
              LV_OPA_COVER, 0);

    switch (S.screen) {
    case GC_SCREEN_DASHBOARD:
        /* No controls on the setup screens: clear the targets so a tap
         * there cannot hit a rect left over from the dashboard. */
        draw_dashboard(store, now_ms);
        break;
    case GC_SCREEN_SETUP:
        S.qr_rect = S.toggle_rect = (rect_t){0, 0, 0, 0};
        draw_centred_screen("Set up GlucoCube",
                            S.setup_caption[0] ? S.setup_caption
                                               : "Scan to begin",
                            S.setup_url, NULL);
        break;
    case GC_SCREEN_HOTSPOT: {
        S.qr_rect = S.toggle_rect = (rect_t){0, 0, 0, 0};
        /* The code joins the phone to our own network; once it is on,
         * the setup page opens by itself. */
        char join[160];
        snprintf(join, sizeof(join), "WIFI:T:WPA;S:%s;P:%s;;",
                 S.hotspot_ssid, S.hotspot_password);
        draw_centred_screen("Connect GlucoCube to Wi-Fi",
                            "Scan to join this device's setup network",
                            join, S.hotspot_password);
        break;
    }
    case GC_SCREEN_PAIRING:
        S.qr_rect = S.toggle_rect = (rect_t){0, 0, 0, 0};
        draw_centred_screen("Pair with GlucoCore",
                            "Scan with a phone that is signed in",
                            S.setup_url, S.setup_caption);
        break;
    }

    lv_canvas_finish_layer(S.canvas, &S.layer);
    return gc_board_present(S.frame);
}

/* --------------------------------------------------------------- taps -- */

static bool inside(rect_t r, int x, int y)
{
    return r.w > 0 && r.h > 0 && x >= r.x && x < r.x + r.w && y >= r.y
           && y < r.y + r.h;
}

bool gc_ui_handle_touch(int x, int y)
{
    const int64_t now_ms = uptime_ms();

    /* Anywhere dismisses the QR overlay. Somebody who has just scanned it
     * should not have to find a small target to put it away. */
    if (now_ms < S.qr_open_until_ms) {
        S.qr_open_until_ms = 0;
        return true;
    }
    /* Touchscreens can deliver one tap twice; debounce so a single press
     * does not flip the theme back again. */
    if (now_ms - S.last_toggle_ms < (int64_t)(GC_TAP_DEBOUNCE_SECONDS * 1000)) {
        return false;
    }

    if (inside(S.qr_rect, x, y)) {
        S.last_toggle_ms = now_ms;
        S.tap_flash_ms = now_ms;
        S.flash_rect = S.qr_rect;
        S.qr_open_until_ms = now_ms + (int64_t)GC_QR_OPEN_SECONDS * 1000;
        return true;
    }
    if (inside(S.toggle_rect, x, y)) {
        S.last_toggle_ms = now_ms;
        S.tap_flash_ms = now_ms;
        S.flash_rect = S.toggle_rect;
        gc_ui_set_theme(S.theme == GC_THEME_DARK ? GC_THEME_LIGHT
                                                 : GC_THEME_DARK);
        return true;
    }
    return false;
}

/* --------------------------------------------------------------- state -- */

void gc_ui_set_config(const gc_config_t *config)
{
    if (config != NULL) {
        S.config = *config;
        S.theme = config->display.theme;
    }
}

void gc_ui_set_screen(gc_screen_t screen)
{
    S.screen = screen;
}

gc_screen_t gc_ui_screen(void)
{
    return S.screen;
}

void gc_ui_set_setup_url(const char *url, const char *caption)
{
    snprintf(S.setup_url, sizeof(S.setup_url), "%s", url ? url : "");
    snprintf(S.setup_caption, sizeof(S.setup_caption), "%s",
             caption ? caption : "");
}

void gc_ui_set_hotspot(const char *ssid, const char *password)
{
    snprintf(S.hotspot_ssid, sizeof(S.hotspot_ssid), "%s", ssid ? ssid : "");
    snprintf(S.hotspot_password, sizeof(S.hotspot_password), "%s",
             password ? password : "");
}

void gc_ui_set_theme(gc_theme_t theme)
{
    S.theme = theme;
    S.config.display.theme = theme;
}

gc_theme_t gc_ui_theme(void)
{
    return S.theme;
}

void gc_ui_show_settings_qr(const char *url)
{
    snprintf(S.settings_url, sizeof(S.settings_url), "%s", url ? url : "");
    S.qr_open_until_ms = uptime_ms() + (int64_t)GC_QR_OPEN_SECONDS * 1000;
}

void gc_ui_set_pending_update(const char *version)
{
    snprintf(S.pending_update, sizeof(S.pending_update), "%s",
             version ? version : "");
}

/* --------------------------------------------------------------- init -- */

/* LVGL wants a display before any object can be created. Nothing is ever
 * flushed through it — the canvas is pushed to the panel by gc_ui_draw —
 * so this exists only to give the canvas somewhere to live. */
static void flush_nowhere(lv_display_t *display, const lv_area_t *area,
                          uint8_t *px_map)
{
    (void)area;
    (void)px_map;
    lv_display_flush_ready(display);
}

esp_err_t gc_ui_init(const gc_config_t *config)
{
    if (S.ready) {
        return ESP_OK;
    }
    memset(&S, 0, sizeof(S));
    gc_ui_set_config(config);

    S.frame = gc_board_alloc_framebuffer();
    if (S.frame == NULL) {
        return ESP_ERR_NO_MEM;
    }

    lv_init();

    const int w = gc_board_width(), h = gc_board_height();
    S.display = lv_display_create(w, h);
    if (S.display == NULL) {
        gc_board_free_framebuffer(S.frame);
        S.frame = NULL;
        return ESP_ERR_NO_MEM;
    }
    lv_display_set_flush_cb(S.display, flush_nowhere);
    /* LVGL sizes a partial-render buffer by dividing it into whole lines and
     * asserts if that comes to none, so this has to be at least one line
     * wide and aligned even though nothing is ever rendered through it. */
    static __attribute__((aligned(LV_DRAW_BUF_ALIGN)))
        uint8_t stub[GC_PANEL_WIDTH * 2 * 2];
    lv_display_set_buffers(S.display, stub, NULL, sizeof(stub),
                           LV_DISPLAY_RENDER_MODE_PARTIAL);

    S.canvas = lv_canvas_create(lv_display_get_screen_active(S.display));
    if (S.canvas == NULL) {
        gc_board_free_framebuffer(S.frame);
        S.frame = NULL;
        return ESP_ERR_NO_MEM;
    }
    lv_canvas_set_buffer(S.canvas, S.frame, w, h, LV_COLOR_FORMAT_RGB565);

    S.screen = GC_SCREEN_DASHBOARD;
    S.ready = true;
    ESP_LOGI(TAG, "dashboard ready at %dx%d", w, h);
    return ESP_OK;
}

void gc_ui_deinit(void)
{
    if (!S.ready) {
        return;
    }
    for (int i = 0; i < S.font_count; i++) {
        lv_tiny_ttf_destroy(S.fonts[i].font);
    }
    S.font_count = 0;
    gc_board_free_framebuffer(S.frame);
    S.frame = NULL;
    S.ready = false;
}

size_t gc_ui_screenshot_png(uint8_t *out, size_t capacity)
{
    /* The Pi writes a PNG of every frame for the settings page's live view.
     * Encoding 750 KB of RGB565 on this hardware, every few seconds, would
     * cost more than the page is worth — so the settings page shows the
     * dashboard's own HTML instead, and this reports that there is no
     * image rather than pretending. */
    (void)out;
    (void)capacity;
    return 0;
}
