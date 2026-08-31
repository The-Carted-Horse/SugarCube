"""Full-screen pygame dashboard, one panel per person.

Runs without a desktop: on the Pi, SDL's kmsdrm backend draws straight to
the display. On a dev machine it opens a normal window (--windowed).

Design: near-black background, left-aligned type. Each panel shows the
person's name with a source/freshness badge, a huge glucose number with
trend arrow + delta, a FORECAST 2H header row, a 5-hour chart (3h history,
2h forecast with a confidence cone), and an IOB/COB/CARBS/BOLUS stat row.
A footer spans the screen with the date/time, a SETTINGS control that
pops a QR code for the settings page, and the NIGHT/DAY toggle.
Numerals are Space Grotesk; labels are JetBrains Mono (bundled, OFL).

Taps reach both controls two ways: SDL events (kmsdrm, or a dev window)
and, because SDL's dummy driver used by the fbdev path delivers none,
the evdev reader in ``touch.py``.
"""

import math
import os
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass

import pygame

from . import contract, network, predict, touch
from .config import SCREEN_PNG, Config, admin_url, merged_thresholds
from .store import Store, UserSnapshot

# Every number that decides what a person sees is in contract.py, so the
# Pi and the ESP32 firmware lay the same dashboard out from one source.
L = contract.LAYOUT
LAYOUT_TRACKING = L["label_tracking"]


def footer_px(footer) -> int:
    """Type size for everything in the footer, from the footer's own height."""
    return max(L["footer_px_min"], int(footer.height * L["footer_px_h"]))


@dataclass(frozen=True)
class Palette:
    name: str
    bg: tuple
    band: tuple        # chart target-range band
    line: tuple        # dividers, rules, dashed gridlines
    fg: tuple
    dim: tuple         # labels
    faint: tuple       # axis ticks, band bounds, sub-labels
    trace: tuple       # history line
    stale: tuple
    in_range: tuple
    high: tuple
    low: tuple
    urgent: tuple


DARK = Palette(name="dark", **contract.PALETTES["dark"])
LIGHT = Palette(name="light", **contract.PALETTES["light"])
THEMES = {p.name: p for p in (DARK, LIGHT)}
THEME_STATE_USER = "__display"     # params-table key for persisted UI state
QR_OPEN_SECONDS = contract.QR_OPEN_SECONDS

DIRECTION_ANGLES = contract.DIRECTION_ANGLES

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_FILES = {
    "num": "SpaceGrotesk-Bold.ttf",        # glucose digits, stat values
    "num-med": "SpaceGrotesk-Medium.ttf",
    "mono": "JetBrainsMono-Regular.ttf",   # labels, badges, footer
    "mono-med": "JetBrainsMono-Medium.ttf",
    "mono-bold": "JetBrainsMono-Bold.ttf",
}


class FramebufferPresenter:
    """Presents pygame surfaces straight to /dev/fb0.

    Bypasses SDL's EGL/GLES scanout path entirely — the same route the
    text console uses, so if boot text is visible, this works. Selected
    with GLUCOCUBE_DISPLAY=fbdev (SDL renders into a dummy surface and we
    copy the pixels out once a second).
    """

    def __init__(self, device: str = "/dev/fb0"):
        base = "/sys/class/graphics/" + os.path.basename(device)
        w, h = open(base + "/virtual_size").read().strip().split(",")
        self.width, self.height = int(w), int(h)
        self.bpp = int(open(base + "/bits_per_pixel").read())
        self.stride = int(open(base + "/stride").read())
        if self.bpp not in (16, 32):
            raise RuntimeError(f"unsupported framebuffer depth: {self.bpp}")
        # The device is reopened for every frame: the kernel's fbdev
        # emulation flushes to the panel on close, so a held-open fd
        # shows nothing until the process exits.
        self.device = device
        self._conv = (
            pygame.Surface((self.width, self.height), 0, 16,
                           masks=(0xF800, 0x07E0, 0x001F, 0))
            if self.bpp == 16 else None
        )

    def present(self, surface: pygame.Surface) -> None:
        if self.bpp == 32:
            data = pygame.image.tobytes(surface, "BGRA")
            row = self.width * 4
        else:
            self._conv.blit(surface, (0, 0))
            raw = self._conv.get_buffer().raw
            pitch = self._conv.get_pitch()
            row = self.width * 2
            data = (raw if pitch == row else b"".join(
                raw[y * pitch:y * pitch + row] for y in range(self.height)
            ))
        with open(self.device, "r+b", buffering=0) as dev:
            if self.stride == row:
                dev.write(data)
            else:
                for y in range(self.height):
                    dev.seek(y * self.stride)
                    dev.write(data[y * row:(y + 1) * row])


def age_compact(now_ms: int, then_ms: int | None) -> str:
    """'NOW', '4M', '1H07M', '2D' — the badge/stat style from the design."""
    if then_ms is None:
        return "--"
    minutes = int((now_ms - then_ms) / 60000)
    if minutes < 1:
        return "NOW"
    if minutes < 60:
        return f"{minutes}M"
    if minutes < 24 * 60:
        return f"{minutes // 60}H{minutes % 60:02d}M"
    return f"{minutes // (24 * 60)}D"


def source_label(user_cfg) -> str:
    """Short badge name for where this person's data comes from."""
    stype = (user_cfg.source or {}).get("type")
    return contract.SOURCE_LABELS.get(stype, contract.SOURCE_LABEL_DEFAULT)


class Display:
    def __init__(self, config: Config, store: Store, windowed: bool = False):
        self.config = config
        self.store = store
        pygame.init()
        pygame.mouse.set_visible(False)
        dc = config.display
        self.fb: FramebufferPresenter | None = None
        if os.environ.get("GLUCOCUBE_DISPLAY") == "fbdev":
            self.fb = FramebufferPresenter()
            dc.width, dc.height = self.fb.width, self.fb.height
            # Never pass FULLSCREEN to the dummy driver: it substitutes its
            # fake 1024x768 desktop size, and the framebuffer copy would
            # then crop to the top-left corner (a "zoomed in" panel).
            pygame.display.set_mode((dc.width, dc.height))
            self.screen = pygame.Surface((dc.width, dc.height))
        else:
            flags = 0 if (windowed or not dc.fullscreen) else pygame.FULLSCREEN
            self.screen = pygame.display.set_mode((dc.width, dc.height), flags)
        pygame.display.set_caption("GlucoCube")
        self.clock = pygame.time.Clock()
        self._fonts: dict[tuple[str, int], pygame.font.Font] = {}
        saved = store.get_params(THEME_STATE_USER).get("theme", "dark")
        self.pal = THEMES.get(saved, DARK)
        self._qr_rect, self._toggle_rect = self._controls_for(
            self._footer_rect())
        self._last_toggle = 0.0
        self._tap_flash = -99.0        # monotonic time of the last toggle
        self._flash_rect = pygame.Rect(0, 0, 0, 0)
        self._qr_open_until = 0.0      # monotonic; the QR overlay's deadline
        self._taps: queue.SimpleQueue = queue.SimpleQueue()
        self._wake = threading.Event()  # set by the touch thread to redraw now
        self._touch: touch.TouchReader | None = None
        self._qr_cache: tuple[str, pygame.Surface | None] | None = None
        self._lan_ip = ("", 0.0)  # (ip, fetched-at monotonic time)
        self._hotspot_pw = store.get_params("__network").get("hotspot_password", "")
        self._hotspot_state = (False, 0.0)  # (active, checked-at monotonic time)
        self._update_state: tuple[dict, float] = ({}, 0.0)
        # SDL's dummy video driver (forced by the fbdev path) pumps no
        # events at all, so the panel has to be read directly. kmsdrm
        # already delivers taps; GLUCOCUBE_TOUCH=evdev forces the reader
        # there too, and toggle_theme() debounces any double delivery.
        want_touch = os.environ.get("GLUCOCUBE_TOUCH", "").lower()
        if want_touch != "off" and (self.fb is not None or want_touch == "evdev"):
            reader = touch.TouchReader(dc.width, dc.height, self._on_touch)
            if reader.start():
                self._touch = reader

    def toggle_theme(self):
        # Touchscreens can deliver a tap as both finger and mouse events;
        # debounce so one tap doesn't flip the theme twice.
        if not self._debounce():
            return
        self._flash_rect = self._toggle_rect
        self.pal = LIGHT if self.pal.name == "dark" else DARK
        self.store.set_params(THEME_STATE_USER, {"theme": self.pal.name})

    def toggle_qr(self):
        """Show (or hide) the QR code that opens the settings page."""
        if not self._debounce():
            return
        self._flash_rect = self._qr_rect
        self._qr_open_until = (0.0 if self.qr_open()
                               else time.monotonic() + QR_OPEN_SECONDS)

    def qr_open(self) -> bool:
        # Times out rather than latching: this hangs on a wall, and a
        # dashboard covered by a QR code all afternoon helps nobody.
        return time.monotonic() < self._qr_open_until

    def _debounce(self) -> bool:
        now = time.monotonic()
        if now - self._last_toggle < contract.TAP_DEBOUNCE_SECONDS:
            return False
        self._last_toggle = now
        self._tap_flash = now
        return True

    def _sync_theme(self) -> None:
        """Adopt a theme set elsewhere (the web UI writes the same key)."""
        name = self.store.get_params(THEME_STATE_USER).get("theme")
        if name in THEMES and name != self.pal.name:
            self.pal = THEMES[name]

    # ---- taps ----

    def _on_touch(self, x: float, y: float) -> None:
        """Called from the evdev reader thread; wakes the draw loop."""
        self._taps.put((x, y))
        self._wake.set()

    def _handle_tap(self, pos) -> None:
        if self.qr_open():
            # Anywhere dismisses it. Someone who has just scanned the code
            # should not have to find a small target to put it away.
            self._qr_open_until = 0.0
            self._wake.set()
            return
        if self._qr_rect.collidepoint(pos):
            self.toggle_qr()
        elif self._toggle_rect.collidepoint(pos):
            self.toggle_theme()

    def _footer_rect(self) -> pygame.Rect:
        full_h = self.screen.get_height()
        height = max(L["footer_h_px"], int(full_h * L["footer_h"]))
        return pygame.Rect(0, full_h - height,
                           self.screen.get_width(), height)

    def _toggle_rect_for(self, footer: pygame.Rect) -> pygame.Rect:
        """Touch target for the NIGHT/DAY control.

        Derived from the footer geometry rather than the rendered label:
        taps are tested before the frame is drawn, so the target must not
        depend on text metrics measured during the *previous* frame — that
        is why the first tap after boot used to do nothing.
        """
        px = footer_px(footer)
        rect = pygame.Rect(
            0, 0,
            max(L["toggle_w_px"], px * L["toggle_w_ratio"]),
            max(L["toggle_h_px"], footer.height * L["toggle_h_ratio"]),
        )
        rect.midright = (footer.right, footer.centery)
        if rect.top < 0:
            rect.top = 0
        return rect

    def _controls_for(self, footer: pygame.Rect) -> tuple:
        """(QR, NIGHT/DAY) touch targets, packed in from the right.

        The QR control gives way on a screen too narrow to hold both it
        and the clock — the settings page is still reachable from any
        phone on the network, and a control overlapping the date is worse
        than no control.
        """
        toggle = self._toggle_rect_for(footer)
        px = footer_px(footer)
        # Wide enough for the word as well as the mark: "SETTINGS" at
        # this size is about 8 characters of tracked mono, and a control
        # that is only a glyph is a control nobody presses.
        qr = pygame.Rect(0, 0, max(L["qr_w_px"], px * L["qr_w_ratio"]),
                         toggle.height)
        qr.midright = (toggle.left, footer.centery)
        if qr.left < footer.left + int(footer.width * L["qr_min_left_w"]):
            return pygame.Rect(0, 0, 0, 0), toggle
        return qr, toggle

    # ---- type ----

    def font(self, px: int, kind: str = "mono") -> pygame.font.Font:
        key = (kind, px)
        if key not in self._fonts:
            path = os.path.join(FONT_DIR, FONT_FILES.get(kind, ""))
            try:
                self._fonts[key] = pygame.font.Font(path, px)
            except OSError:
                self._fonts[key] = pygame.font.Font(None, px)
        return self._fonts[key]

    def text(self, surface, s, px, color, kind="mono", **anchor):
        img = self.font(px, kind).render(s, True, color)
        rect = img.get_rect(**anchor)
        surface.blit(img, rect)
        return rect

    def label(self, surface, s, px, color, kind="mono",
              tracking=LAYOUT_TRACKING, **anchor):
        """Uppercase letterspaced text — the design's small-caps labels."""
        font = self.font(px, kind)
        imgs = [font.render(ch, True, color) for ch in s.upper()]
        space = int(px * tracking)
        width = sum(i.get_width() for i in imgs) + space * max(0, len(imgs) - 1)
        height = max((i.get_height() for i in imgs), default=1)
        out = pygame.Surface((max(1, width), height), pygame.SRCALPHA)
        x = 0
        for img in imgs:
            out.blit(img, (x, 0))
            x += img.get_width() + space
        rect = out.get_rect(**anchor)
        surface.blit(out, rect)
        return rect

    def stat_value(self, surface, value: str, unit: str, px: int, topleft):
        """Big numeral with a smaller, dimmer unit suffix on the baseline."""
        vfont = self.font(px, "num")
        vimg = vfont.render(value, True, self.pal.fg)
        surface.blit(vimg, topleft)
        if unit:
            ufont = self.font(int(px * L["stats_unit_ratio"]), "num")
            uimg = ufont.render(unit, True, self.pal.dim)
            baseline = topleft[1] + vfont.get_ascent()
            surface.blit(
                uimg,
                (topleft[0] + vimg.get_width() + int(px * L["stats_unit_gap"]),
                 baseline - ufont.get_ascent()),
            )

    # ---- panel pieces ----

    def glucose_color(self, sgv: float | None, stale: bool, th: dict):
        if sgv is None or stale:
            return self.pal.stale
        if sgv <= th["urgent_low"] or sgv >= th["urgent_high"]:
            return self.pal.urgent
        if sgv < th["low"]:
            return self.pal.low
        if sgv > th["high"]:
            return self.pal.high
        return self.pal.in_range

    def draw_arrow(self, surface, center, size, direction, color):
        info = DIRECTION_ANGLES.get(direction or "")
        if info is None:
            return
        angle, count = info
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)

        def rot(x, y, cx, cy):
            return (cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a)

        half = size / 2
        # Perpendicular offset separates the arrows of a double arrow.
        perp = (-sin_a, cos_a)
        offsets = [0] if count == 1 else [-size * 0.32, size * 0.32]
        for off in offsets:
            cx = center[0] + perp[0] * off
            cy = center[1] + perp[1] * off
            shaft_w = max(2, int(size * 0.14))
            pygame.draw.line(
                surface, color,
                rot(-half, 0, cx, cy), rot(half * 0.45, 0, cx, cy), shaft_w,
            )
            head = [
                rot(half, 0, cx, cy),
                rot(half * 0.25, -half * 0.55, cx, cy),
                rot(half * 0.25, half * 0.55, cx, cy),
            ]
            pygame.draw.polygon(surface, color, head)

    def draw_logo(self, surface, center, size, color):
        """An isometric cube: a hexagon with three edges to the near corner.

        Drawn rather than blitted, like the sun and moon above it, so it
        stays crisp on any panel and adds no asset to carry around.
        """
        radius = size / 2
        points = [
            (center[0] + math.cos(math.radians(60 * i - 90)) * radius,
             center[1] + math.sin(math.radians(60 * i - 90)) * radius)
            for i in range(6)
        ]
        width = max(1, int(size * 0.09))
        pygame.draw.lines(surface, color, True, points, width)
        # Vertices 1, 3 and 5 are the three edges meeting at the near
        # corner — the line that turns a hexagon into a cube.
        for index in (1, 3, 5):
            pygame.draw.line(surface, color, center, points[index], width)

    def draw_chart(self, chart: pygame.Rect, snap: UserSnapshot, stale: bool,
                   th: dict, future, est: bool, now_ms: int):
        """3h history + 2h forecast: range band, trace, cone, dotted forecast."""
        surface = self.screen
        pal = self.pal
        t0 = now_ms - contract.CHART_HISTORY_MINUTES * 60 * 1000
        t1 = now_ms + contract.CHART_FORECAST_MINUTES * 60 * 1000
        values = [v for _, v in snap.history] + [v for _, v in (future or [])]
        if not values:
            values = [th["low"], th["high"]]
        lo = min(min(values), th["low"]) - contract.CHART_PAD_BELOW
        hi = max(max(values), th["high"]) + contract.CHART_PAD_ABOVE

        def X(t):
            return chart.left + (t - t0) / (t1 - t0) * chart.width

        def Y(v):
            return chart.bottom - (v - lo) / (hi - lo) * chart.height

        # Target-range band with its bounds labeled at the right edge.
        band_top, band_bot = int(Y(th["high"])), int(Y(th["low"]))
        pygame.draw.rect(surface, pal.band,
                         (chart.left, band_top, chart.width, band_bot - band_top))
        lab_px = max(L["chart_band_label_px_min"],
                     int(chart.height * L["chart_band_label_px_h"]))
        inset = L["chart_band_label_inset"]
        self.text(surface, f"{th['high']:.0f}", lab_px, pal.faint,
                  topright=(chart.right - inset, band_top + 2))
        if band_bot - band_top > lab_px * L["chart_band_thin_ratio"]:
            # Skip the lower bound when the band is too thin to hold it.
            self.text(surface, f"{th['low']:.0f}", lab_px, pal.faint,
                      bottomright=(chart.right - inset, band_bot - 2))

        # Dashed "now" divider between measured past and forecast.
        x_now = X(now_ms)
        y = chart.top
        while y < chart.bottom:
            pygame.draw.line(
                surface, pal.line, (x_now, y),
                (x_now, min(y + L["chart_now_dash_on"], chart.bottom)), 1)
            y += L["chart_now_dash_period"]

        # Forecast confidence cone: widens with time; wider when the curve
        # is our own estimate rather than the pump's. Needs two points —
        # pygame polygons want at least 4 vertices.
        color_now = self.glucose_color(snap.sgv, stale, th)
        if future and len(future) >= 2:
            rate = (contract.CONE_RATE_ESTIMATE if est
                    else contract.CONE_RATE_DEVICE)
            upper, lower = [], []
            for t, v in future:
                spread = contract.CONE_BASE_SPREAD + (t - now_ms) / 60000 * rate
                upper.append((X(t) - chart.left, max(0, Y(v - spread) - chart.top)))
                lower.append((X(t) - chart.left,
                              min(chart.height, Y(v + spread) - chart.top)))
            cone_color = self.glucose_color(future[-1][1], False, th)
            overlay = pygame.Surface(chart.size, pygame.SRCALPHA)
            pygame.draw.polygon(overlay, (*cone_color, contract.CONE_ALPHA),
                                upper + list(reversed(lower)))
            surface.blit(overlay, chart.topleft)

        # History: a smooth neutral line (dots would imply per-reading
        # color), split on >15-minute gaps so sensor outages stay visible.
        trace = pal.stale if stale else pal.trace
        segments, seg, last_t = [], [], None
        for t, v in snap.history:
            if t < t0:
                continue
            if last_t is not None and t - last_t > contract.CHART_GAP_SPLIT_MS:
                segments.append(seg)
                seg = []
            seg.append((X(t), Y(v)))
            last_t = t
        segments.append(seg)
        for seg in segments:
            if len(seg) < 2:
                continue
            pygame.draw.aalines(surface, trace, False, seg)
            pygame.draw.aalines(surface, trace, False,
                                [(x, y + 1) for x, y in seg])

        # Forecast: dots only, no line — clearly not measured data.
        dot_r = max(L["chart_dot_r_px"], chart.height * L["chart_dot_r_h"])
        for t, v in (future or []):
            pygame.draw.circle(surface, self.glucose_color(v, False, th),
                               (X(t), Y(v)), dot_r)

        # "Now" marker: soft halo + solid dot at the latest reading.
        if snap.history:
            t_last, v_last = snap.history[-1]
            cx, cy = X(min(t_last, now_ms)), Y(v_last)
            r = max(L["chart_now_r_px"], int(chart.height * L["chart_now_r_h"]))
            halo = pygame.Surface((r * 6, r * 6), pygame.SRCALPHA)
            pygame.draw.circle(halo, (*color_now, contract.NOW_HALO_ALPHA),
                               (r * 3, r * 3), r * 2)
            surface.blit(halo, (cx - r * 3, cy - r * 3))
            pygame.draw.circle(surface, color_now, (cx, cy), r)

    def draw_panel(self, rect: pygame.Rect, user_cfg, snap: UserSnapshot):
        surface = self.screen
        # One person's data must never render inside the neighbor's panel,
        # whatever the resolution — clip everything to this rect.
        surface.set_clip(rect)
        try:
            self._draw_panel_content(rect, user_cfg, snap)
        finally:
            surface.set_clip(None)

    def _draw_panel_content(self, rect: pygame.Rect, user_cfg, snap: UserSnapshot):
        surface = self.screen
        dc = self.config.display
        th = merged_thresholds(dc, user_cfg)
        now_ms = int(time.time() * 1000)
        h, w = rect.height, rect.width
        pad = int(w * L["panel_pad_w"])
        left, right = rect.left + pad, rect.right - pad

        stale = (
            snap.sgv_date is None
            or now_ms - snap.sgv_date > dc.stale_minutes * 60 * 1000
        )
        color = self.glucose_color(snap.sgv, stale, th)

        # Header: name left; freshness dot + source + reading age right.
        top = rect.top + int(h * L["header_top_h"])
        self.label(surface, user_cfg.name, int(h * L["name_px_h"]), self.pal.fg,
                   kind="mono-med", topleft=(left, top))
        age_min = (now_ms - snap.sgv_date) / 60000 if snap.sgv_date else None
        dot_color = (
            self.pal.in_range
            if age_min is not None and age_min <= contract.FRESH_MINUTES
            else self.pal.high
            if age_min is not None and age_min <= dc.stale_minutes
            else self.pal.low
        )
        badge = f"{source_label(user_cfg)} · {age_compact(now_ms, snap.sgv_date)}"
        badge_rect = self.label(surface, badge, int(h * L["badge_px_h"]),
                                self.pal.dim,
                                topright=(right, top + int(h * L["badge_top_h"])))
        pygame.draw.circle(
            surface, dot_color,
            (badge_rect.left - int(h * L["badge_dot_gap_h"]), badge_rect.centery),
            max(L["badge_dot_r_px"], int(h * L["badge_dot_r_h"])))

        # Big glucose number, left-aligned; arrow/delta/unit column right of it.
        # Blit so the digits' cap top (not the font's line box) lands at
        # num_top — Space Grotesk carries a lot of internal leading.
        sgv_str = f"{snap.sgv:.0f}" if snap.sgv is not None else "---"
        num_px = int(h * L["num_px_h"])
        num_font = self.font(num_px, "num")
        num_img = num_font.render(sgv_str, True, color)
        # Tiny panels: shrink the number so it and the trend column fit.
        max_num_w = (right - left) - int(w * L["num_max_w_reserve_w"])
        if num_img.get_width() > max_num_w:
            num_px = max(L["num_px_min"],
                         int(num_px * max_num_w / num_img.get_width()))
            num_font = self.font(num_px, "num")
            num_img = num_font.render(sgv_str, True, color)
        cap = int(num_px * L["num_cap_ratio"])
        num_top = rect.top + int(h * L["num_top_h"])
        surface.blit(num_img,
                     (left - int(num_px * L["num_left_nudge"]),
                      num_top - (num_font.get_ascent() - cap)))

        col_x = left + num_img.get_width() + int(w * L["trend_gap_w"])
        if not stale and snap.direction in DIRECTION_ANGLES:
            arrow_size = int(h * L["arrow_size_h"])
            self.draw_arrow(
                surface, (col_x + arrow_size, num_top + cap * L["arrow_y_cap"]),
                arrow_size, snap.direction, color)
        if not stale and snap.delta is not None:
            delta_font = self.font(int(h * L["delta_px_h"]), "num-med")
            delta_img = delta_font.render(f"{snap.delta:+.0f}", True, self.pal.fg)
            surface.blit(delta_img,
                         (col_x, num_top + int(cap * L["delta_y_cap"])))
        self.label(surface, "MG/DL", int(h * L["unit_px_h"]), self.pal.faint,
                   topleft=(col_x, num_top + int(cap * L["unit_y_cap"])))

        # FORECAST 2H header: label — rule — value + arrival time.
        fy = rect.top + int(h * L["forecast_y_h"])
        far = contract.HORIZONS[-1]
        horizons, future, fc_source = (None, None, None)
        if not stale:
            horizons, future, fc_source = predict.predict(snap, now_ms)
        lab_px = int(h * L["forecast_label_px_h"])
        lab_rect = self.label(surface, f"FORECAST {far // 60}H", lab_px,
                              self.pal.dim, midleft=(left, fy))
        rule_gap = int(w * L["forecast_rule_gap_w"])
        rule_end = right
        if horizons and far in horizons:
            eta = time.strftime("%H:%M",
                                time.localtime((now_ms + far * 60000) / 1000))
            eta_rect = self.label(surface, eta, lab_px, self.pal.faint,
                                  midright=(right, fy))
            tilde = "~" if fc_source == "est" else ""
            value_color = self.glucose_color(horizons[far], False, th)
            val_rect = self.text(
                surface, f"{tilde}{horizons[far]:.0f}",
                int(h * L["forecast_value_px_h"]), value_color, kind="num",
                midright=(eta_rect.left - int(w * L["forecast_eta_gap_w"]), fy))
            rule_end = val_rect.left - rule_gap
        pygame.draw.line(surface, self.pal.line,
                         (lab_rect.right + rule_gap, fy), (rule_end, fy))

        # Chart with its time axis.
        chart = pygame.Rect(left, rect.top + int(h * L["chart_top_h"]),
                            right - left, int(h * L["chart_height_h"]))
        self.draw_chart(chart, snap, stale, th, future,
                        fc_source == "est", now_ms)
        ax_y = chart.bottom + int(h * L["chart_axis_gap_h"])
        span = contract.CHART_HISTORY_MINUTES + contract.CHART_FORECAST_MINUTES
        for minutes, lab in contract.CHART_TICKS:
            x = chart.left + (
                minutes + contract.CHART_HISTORY_MINUTES) / span * chart.width
            self.label(surface, lab, int(h * L["chart_axis_px_h"]),
                       self.pal.faint, midtop=(x, ax_y))

        # Stats row: IOB, COB, last carbs, last bolus.
        stats = [
            ("IOB", f"{snap.iob:.1f}" if snap.iob is not None else "--",
             "U" if snap.iob is not None else "", None),
            ("COB", f"{snap.cob:.0f}" if snap.cob is not None else "--",
             "G" if snap.cob is not None else "", None),
            ("CARBS",
             f"{snap.last_carbs:.0f}" if snap.last_carbs is not None else "--",
             "G" if snap.last_carbs is not None else "",
             f"{age_compact(now_ms, snap.last_carbs_date)} AGO"
             if snap.last_carbs_date else None),
            ("BOLUS",
             f"{snap.last_bolus:.2f}" if snap.last_bolus is not None else "--",
             "U" if snap.last_bolus is not None else "",
             f"{age_compact(now_ms, snap.last_bolus_date)} AGO"
             if snap.last_bolus_date else None),
        ]
        labels_y = rect.top + int(h * L["stats_label_y_h"])
        col_w = (right - left) / len(stats)
        for idx, (lab, value, unit, sub) in enumerate(stats):
            x = left + int(idx * col_w)
            self.label(surface, lab, int(h * L["stats_label_px_h"]), self.pal.dim,
                       topleft=(x, labels_y))
            value_y = labels_y + int(h * L["stats_value_gap_h"])
            self.stat_value(surface, value, unit,
                            int(h * L["stats_value_px_h"]), (x, value_y))
            if sub:
                self.label(surface, sub, int(h * L["stats_sub_px_h"]),
                           self.pal.faint,
                           topleft=(x, value_y + int(h * L["stats_sub_gap_h"])))

        # Urgent readings get a colored border to catch the eye across a room.
        if color == self.pal.urgent:
            inset = L["urgent_inset"]
            pygame.draw.rect(surface, self.pal.urgent,
                             rect.inflate(-inset, -inset), L["urgent_width"],
                             border_radius=L["urgent_radius"])

    def _pending_update(self) -> dict:
        state, checked = self._update_state
        if time.monotonic() - checked > 60:
            state = self.store.get_params("__updates")
            self._update_state = (state, time.monotonic())
        return state if state.get("available") else {}

    def draw_footer(self, rect: pygame.Rect):
        """Date/time left (plus update notice); SETTINGS and NIGHT/DAY right."""
        surface = self.screen
        pygame.draw.line(surface, self.pal.line,
                         (rect.left, rect.top), (rect.right, rect.top))
        if (time.monotonic() - self._tap_flash < contract.TAP_FLASH_SECONDS
                and self._flash_rect.width):
            # Momentary highlight so a tap is visibly acknowledged, on
            # whichever control was actually hit.
            pygame.draw.rect(surface, self.pal.band,
                             self._flash_rect.clip(rect).inflate(-2, -6),
                             border_radius=8)
        pad = int(surface.get_width() * L["footer_pad_w"])
        px = footer_px(rect)
        when = time.strftime("%a %d %b · %H:%M").upper()
        when_rect = self.label(surface, when, px, self.pal.dim,
                               midleft=(rect.left + pad, rect.centery))
        update = self._pending_update()
        if update:
            notice = f"UPDATE {update.get('latest', '')}"
            # Mono glyph ≈ 0.6em + 0.22em tracking; skip the notice when
            # it would run into the theme toggle (narrow/portrait screens
            # still surface updates in the web UI).
            notice_w = int(len(notice) * px * L["footer_glyph_advance"])
            x = when_rect.right + int(px * L["footer_update_gap"])
            if x + notice_w < rect.right - int(px * L["qr_w_ratio"]):
                self.label(surface, notice, px, self.pal.high,
                           midleft=(x, rect.centery))

        # The mark sits in the middle of the footer, and gives way to
        # anything that has something to say: the update notice is
        # actionable, this is decoration.
        mark_px = max(L["footer_mark_px_min"],
                      int(px * L["footer_mark_px_ratio"]))
        mark_size = int(mark_px * L["footer_mark_size_ratio"])
        mark_w = (mark_size + int(mark_px * 0.6)
                  + int(len("GLUCOCUBE") * mark_px * L["footer_glyph_advance"]))
        left_edge = rect.centerx - mark_w // 2
        controls_left = (self._qr_rect.left if self._qr_rect.width
                         else self._toggle_rect.left)
        if (not update
                and left_edge > when_rect.right + px
                and rect.centerx + mark_w // 2 < controls_left - px):
            self.draw_logo(surface, (left_edge + mark_size // 2, rect.centery),
                           mark_size, self.pal.faint)
            self.label(surface, "GlucoCube", mark_px, self.pal.faint,
                       midleft=(left_edge + mark_size + int(mark_px * 0.6),
                                rect.centery))

        if self._qr_rect.width:
            self.draw_qr_button(rect, px)

        icon_r = max(L["footer_icon_r_px"], int(rect.height * L["footer_icon_r_h"]))
        icon_c = (rect.right - pad - icon_r, rect.centery)
        mode = "NIGHT" if self.pal.name == "dark" else "DAY"
        self.label(
            surface, mode, px, self.pal.dim,
            midright=(icon_c[0] - icon_r - int(px * 0.9), rect.centery),
        )
        color = self.pal.dim
        if self.pal.name == "dark":
            # Sun: tapping goes to light mode.
            pygame.draw.circle(surface, color, icon_c, icon_r * 0.55, 2)
            for i in range(8):
                angle = i * math.pi / 4
                inner = (icon_c[0] + math.cos(angle) * (icon_r * 0.75),
                         icon_c[1] + math.sin(angle) * (icon_r * 0.75))
                outer = (icon_c[0] + math.cos(angle) * icon_r,
                         icon_c[1] + math.sin(angle) * icon_r)
                pygame.draw.line(surface, color, inner, outer, 2)
        else:
            # Moon: tapping goes back to dark mode.
            pygame.draw.circle(surface, color, icon_c, icon_r * 0.8)
            pygame.draw.circle(surface, self.pal.bg,
                               (icon_c[0] + icon_r * 0.45,
                                icon_c[1] - icon_r * 0.3), icon_r * 0.7)

    # A miniature code, cell by cell: three finder squares and enough
    # data specks to read as a QR at fourteen pixels. Drawn rather than
    # set in a font because at this size a glyph turns to mush.
    QR_GLYPH = contract.QR_GLYPH

    def draw_qr_button(self, rect: pygame.Rect, px: int):
        """The SETTINGS control: a small QR mark, labelled, in the footer."""
        surface = self.screen
        color = self.pal.dim
        cell = max(L["qr_glyph_cell_px"],
                   int(rect.height * L["qr_glyph_cell_h"]) // len(self.QR_GLYPH))
        size = cell * len(self.QR_GLYPH)
        box = pygame.Rect(0, 0, size, size)
        box.midright = (self._qr_rect.right - int(px * 0.7), rect.centery)
        for y, row in enumerate(self.QR_GLYPH):
            for x, mark in enumerate(row):
                if mark == "X":
                    pygame.draw.rect(surface, color,
                                     (box.left + x * cell, box.top + y * cell,
                                      cell, cell))
        label = "SETTINGS"
        label_right = box.left - int(px * 0.8)
        if (label_right - int(len(label) * px * L["footer_glyph_advance"])
                >= self._qr_rect.left):
            self.label(surface, label, px, color,
                       midright=(label_right, rect.centery))

    # ---- the settings QR overlay ----

    def _settings_url(self, keyed: bool = True) -> str | None:
        """Where the settings page lives, or None with no network yet.

        The keyed form carries the admin password as ?key=, which the web
        admin accepts and turns into a cookie — scanning the code has to
        land on the settings page, not on a login box asking for a
        password that is printed six inches away.
        """
        ip = self._cached_lan_ip()
        mdns = self._mdns_url("")
        if ip == "127.0.0.1" and not mdns:
            return None
        path = "/settings"
        if keyed and self.config.admin_password:
            path += f"?key={self.config.admin_password}"
        if mdns:
            return mdns + path
        return admin_url(ip, self.config.admin_port, path)

    def draw_settings_qr(self):
        """Full-screen card holding a QR code for the settings page.

        The same gesture as the sun and moon beside it: something you can
        reach for on the device itself, rather than having to remember an
        address and a password on the phone in your hand.
        """
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        cx = w // 2
        s = min(w, h)
        # Painted over, not tinted: a phone camera wants a clean field
        # around the code, and a ghost of a glucose number behind it is
        # the one way this fails.
        screen.fill(self.pal.bg)
        self.text(screen, "Settings", int(s * 0.075), self.pal.fg,
                  kind="num", midtop=(cx, int(h * 0.04)))
        close = "tap anywhere to close"
        self.text(screen, close, int(s * 0.035), self.pal.faint,
                  midbottom=(cx, h - int(s * 0.035)))
        url = self._settings_url()
        if not url:
            self.text(screen, "This device is not on a network yet",
                      int(s * 0.05), self.pal.dim, midtop=(cx, int(h * 0.44)))
            return
        self.text(screen, "Scan from a phone on this network",
                  int(s * 0.042), self.pal.dim, midtop=(cx, int(h * 0.15)))
        qr = self._qr_surface(url, int(s * 0.44))
        if qr:
            qr_rect = qr.get_rect(center=(cx, int(h * 0.50)))
            screen.blit(qr, qr_rect)
            info_y = qr_rect.bottom + int(s * 0.03)
        else:
            info_y = int(h * 0.44)
        # Without the key: this line is for someone typing it in, and the
        # password below is the half they type second.
        self.text(screen, self._settings_url(keyed=False) or "",
                  int(s * 0.045), self.pal.fg, midtop=(cx, info_y))
        if self.config.admin_password:
            self.text(screen,
                      f"login:  admin  /  {self.config.admin_password}",
                      int(s * 0.038), self.pal.dim,
                      midtop=(cx, info_y + int(s * 0.06)))

    # ---- first-boot setup screens ----

    def _cached_lan_ip(self) -> str:
        ip, fetched = self._lan_ip
        # Loopback means "no route yet" (e.g. right after joining Wi-Fi):
        # retry quickly instead of caching a useless address for a minute.
        ttl = 60 if ip and ip != "127.0.0.1" else 3
        if not ip or time.monotonic() - fetched > ttl:
            ip = network.get_lan_ip()
            self._lan_ip = (ip, time.monotonic())
        return ip

    def _mdns_url(self, path: str = "/setup") -> str | None:
        """http://<hostname>.local/... — works once avahi is up (the image
        ships it); dev machines and odd hostnames just skip the hint."""
        if not sys.platform.startswith("linux"):
            return None
        host = socket.gethostname().split(".")[0]
        if not host or host == "localhost":
            return None
        return admin_url(f"{host}.local", self.config.admin_port, path)

    def _with_key(self, url: str) -> str:
        """The same URL, but one a phone can open without a login box."""
        if not self.config.admin_password:
            return url
        return f"{url}?key={self.config.admin_password}"

    def _qr_surface(self, url: str, target_px: int) -> pygame.Surface | None:
        if self._qr_cache and self._qr_cache[0] == url:
            return self._qr_cache[1]
        surface = None
        try:
            import qrcode
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M, border=2
            )
            qr.add_data(url)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            n = len(matrix)
            scale = max(2, target_px // n)
            # Always dark-on-white regardless of theme — scanners need contrast.
            surface = pygame.Surface((n * scale, n * scale))
            surface.fill((255, 255, 255))
            for y, row in enumerate(matrix):
                for x, dark in enumerate(row):
                    if dark:
                        pygame.draw.rect(
                            surface, (0, 0, 0),
                            (x * scale, y * scale, scale, scale),
                        )
        except ImportError:
            pass  # no qrcode library: the URL text below still shows the way
        self._qr_cache = (url, surface)
        return surface

    def _hotspot_is_active(self) -> bool:
        active, checked = self._hotspot_state
        if time.monotonic() - checked > 5:
            active = network.available() and network.hotspot_active()
            self._hotspot_state = (active, time.monotonic())
        return active

    def _draw_wifi_error(self, screen, cx, h, s):
        """Bottom-of-screen notice after a failed join.

        When Wi-Fi setup fails there is no network to serve a web page
        over, so the panel itself has to say what went wrong.
        """
        wifi = self.store.get_params("__wifi")
        if wifi.get("state") == "joining":
            ssid = str(wifi.get("ssid", ""))
            self.text(screen, f"trying to join {ssid}…", int(s * 0.038),
                      self.pal.dim, midtop=(cx, int(h * 0.90)))
            return
        if wifi.get("state") != "failed":
            return
        ssid = str(wifi.get("ssid", ""))
        reason = str(wifi.get("error", "unknown error"))
        self.text(screen, f"last attempt: {ssid} failed", int(s * 0.04),
                  self.pal.low, midtop=(cx, int(h * 0.875)))
        # Long nmcli reasons get wrapped rather than clipped at the bezel.
        max_chars = max(24, int(screen.get_width() / (s * 0.036 * 0.62)))
        words, line, lines = reason.split(), "", []
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > max_chars:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        for index, text in enumerate(lines[:2]):
            self.text(screen, text, int(s * 0.036), self.pal.dim,
                      midtop=(cx, int(h * 0.915) + index * int(s * 0.045)))

    def draw_hotspot_screen(self):
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        cx = w // 2
        s = min(w, h)  # scale text by the smaller dimension (portrait-safe)
        ssid, pw = network.HOTSPOT_SSID, self._hotspot_pw

        if network.hotspot_client_connected():
            # Stage 2: a phone has joined — offer a QR that opens the
            # settings page already logged in (?key= auto-auth).
            url = self._with_key(admin_url(network.HOTSPOT_ADDR,
                                           self.config.admin_port, "/setup"))
            self.text(screen, "Connected!  One more scan", int(s * 0.075),
                      self.pal.fg, kind="num", midtop=(cx, int(h * 0.035)))
            self.text(screen, "2.  Scan to open setup", int(s * 0.045),
                      self.pal.dim, midtop=(cx, int(h * 0.15)))
            qr = self._qr_surface(url, int(s * 0.42))
            if qr:
                rect = qr.get_rect(center=(cx, int(h * 0.47)))
                screen.blit(qr, rect)
                info_y = rect.bottom + int(s * 0.03)
            else:
                info_y = int(h * 0.40)
            plain = admin_url(network.HOTSPOT_ADDR, self.config.admin_port,
                              "/setup")
            self.text(screen, f"or open  {plain}", int(s * 0.04),
                      self.pal.dim, midtop=(cx, info_y))
            if self.config.admin_password:
                self.text(
                    screen,
                    f"log in:  admin  /  {self.config.admin_password}",
                    int(s * 0.04), self.pal.fg,
                    midtop=(cx, info_y + int(s * 0.065)),
                )
            self._draw_wifi_error(screen, cx, h, s)
            return

        # Stage 1: nothing has joined yet — show the Wi-Fi join QR.
        self.text(screen, "Connect GlucoCube to Wi-Fi", int(s * 0.075),
                  self.pal.fg, kind="num", midtop=(cx, int(h * 0.035)))
        self.text(screen, "1.  Scan to join the setup hotspot", int(s * 0.045),
                  self.pal.dim, midtop=(cx, int(h * 0.15)))
        self._draw_wifi_error(screen, cx, h, s)

        qr = self._qr_surface(f"WIFI:T:WPA;S:{ssid};P:{pw};;", int(s * 0.42))
        if qr:
            rect = qr.get_rect(center=(cx, int(h * 0.47)))
            screen.blit(qr, rect)
            info_y = rect.bottom + int(s * 0.03)
        else:
            info_y = int(h * 0.40)

        self.text(screen, f"{ssid}   password: {pw}", int(s * 0.045),
                  self.pal.fg, midtop=(cx, info_y))
        self.text(screen, "the screen changes once your phone joins",
                  int(s * 0.04), self.pal.dim,
                  midtop=(cx, info_y + int(s * 0.07)))

    def is_unconfigured(self, snaps) -> bool:
        """True until any data has arrived or any pull source is configured."""
        if any(s.sgv_date for s in snaps):
            return False
        if any(u.source for u in self.config.users):
            return False
        return True

    def draw_setup_screen(self):
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        cx = w // 2
        s = min(w, h)  # scale text by the smaller dimension (portrait-safe)

        self.text(screen, "GlucoCube", int(s * 0.09), self.pal.fg,
                  kind="num", midtop=(cx, int(h * 0.045)))

        ip = self._cached_lan_ip()
        mdns = self._mdns_url()
        if ip == "127.0.0.1":
            # No route yet (booting, or just switched networks): showing a
            # loopback URL would only mislead — say what's happening instead.
            self.text(screen, "Connecting to the network…", int(s * 0.05),
                      self.pal.dim, midtop=(cx, int(h * 0.42)))
            if mdns:
                self.text(screen, f"setup will open at  {mdns}",
                          int(s * 0.04), self.pal.faint,
                          midtop=(cx, int(h * 0.52)))
            return

        # The .local name leads: mDNS keeps working when the address
        # changes, and it reaches the device over IPv6 on networks that
        # filter client-to-client IPv4.
        ip_url = admin_url(ip, self.config.admin_port, "/setup")
        primary = mdns or ip_url
        self.text(screen, "Scan from a phone on this network to set up",
                  int(s * 0.045), self.pal.dim, midtop=(cx, int(h * 0.17)))

        # The code carries the admin key so that scanning it opens setup
        # outright. The address printed below deliberately does not: it
        # is for typing, and the login for it is printed with it.
        qr = (self._qr_surface(self._with_key(primary), int(s * 0.46))
              if self.config.admin_port else None)
        if qr:
            rect = qr.get_rect(center=(cx, int(h * 0.52)))
            screen.blit(qr, rect)
            info_y = rect.bottom + int(s * 0.035)
        else:
            info_y = int(h * 0.45)

        self.text(screen, primary, int(s * 0.05), self.pal.fg, midtop=(cx, info_y))
        line_y = info_y + int(s * 0.065)
        if mdns:
            self.text(screen, f"or  {ip_url}", int(s * 0.038), self.pal.dim,
                      midtop=(cx, line_y))
            line_y += int(s * 0.06)
        if self.config.admin_password:
            self.text(
                screen,
                f"login:  admin  /  {self.config.admin_password}",
                int(s * 0.04), self.pal.dim, midtop=(cx, line_y),
            )

    def draw(self):
        self._sync_theme()
        self.screen.fill(self.pal.bg)
        if self._hotspot_is_active():
            # No controls on the setup screens: clear the targets so taps
            # there can't hit a rect left over from the dashboard, and
            # put away an overlay that was up when the network dropped.
            self._toggle_rect = self._qr_rect = pygame.Rect(0, 0, 0, 0)
            self._qr_open_until = 0.0
            self.draw_hotspot_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        users = self.config.users
        snaps = [self.store.snapshot(user.name) for user in users]
        if self.is_unconfigured(snaps):
            self._toggle_rect = self._qr_rect = pygame.Rect(0, 0, 0, 0)
            self._qr_open_until = 0.0
            self.draw_setup_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        full_w = self.screen.get_width()
        full_h = self.screen.get_height()
        footer = self._footer_rect()
        self._qr_rect, self._toggle_rect = self._controls_for(footer)
        body_h = footer.top
        portrait = full_h > full_w
        for i, (user, snap) in enumerate(zip(users, snaps)):
            if portrait:
                row_h = body_h // len(users)
                rect = pygame.Rect(0, i * row_h, full_w, row_h)
                if i > 0:
                    inset = int(full_w * L["panel_divider_inset_portrait"])
                    pygame.draw.line(self.screen, self.pal.line,
                                     (inset, rect.top),
                                     (full_w - inset, rect.top))
            else:
                col_w = full_w // len(users)
                rect = pygame.Rect(i * col_w, 0, col_w, body_h)
                if i > 0:
                    pygame.draw.line(
                        self.screen, self.pal.line,
                        (rect.left,
                         int(body_h * L["panel_divider_inset_landscape"])),
                        (rect.left,
                         body_h - int(
                             body_h * L["panel_divider_inset_landscape_bottom"])))
            self.draw_panel(rect, user, snap)
        self.draw_footer(footer)
        if self.qr_open():
            self.draw_settings_qr()
        pygame.display.flip()
        if self.fb:
            self.fb.present(self.screen)

    def save_snapshot(self):
        """Atomically write the current frame for the /screen.png endpoint."""
        tmp = SCREEN_PNG + ".tmp.png"
        try:
            pygame.image.save(self.screen, tmp)
            os.replace(tmp, SCREEN_PNG)
        except (pygame.error, OSError):
            pass  # a missed snapshot is harmless

    def run(self):
        running = True
        last_snapshot = 0.0
        while running:
            # Cleared before draining so a tap that lands mid-frame still
            # wakes the next wait instead of being swallowed.
            self._wake.clear()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_ESCAPE, pygame.K_q,
                ):
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_t:
                    self.toggle_theme()
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_s:
                    self.toggle_qr()
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    self._handle_tap(event.pos)
                elif event.type == pygame.FINGERDOWN:
                    self._handle_tap((event.x * self.screen.get_width(),
                                      event.y * self.screen.get_height()))
            while True:  # taps read straight from the panel
                try:
                    self._handle_tap(self._taps.get_nowait())
                except queue.Empty:
                    break
            self.draw()
            if time.time() - last_snapshot >= 5:
                self.save_snapshot()
                last_snapshot = time.time()
            # One frame a second is plenty for a glucose dashboard, but a
            # tap must not wait for it: the reader wakes us immediately,
            # and the tap highlight schedules its own frame to clear it.
            timeout = 1.0
            flash_left = 0.35 - (time.monotonic() - self._tap_flash)
            if flash_left > 0:
                timeout = min(timeout, flash_left + 0.02)
            if self.qr_open():
                # Redraw promptly when the overlay's welcome runs out.
                timeout = min(timeout,
                              max(0.05, self._qr_open_until - time.monotonic()))
            self._wake.wait(timeout)
        self._stop_touch()
        pygame.quit()

    def _stop_touch(self) -> None:
        if self._touch:
            self._touch.stop()
            self._touch = None

    def screenshot(self, path: str):
        self.draw()
        pygame.image.save(self.screen, path)
        self._stop_touch()
        pygame.quit()
