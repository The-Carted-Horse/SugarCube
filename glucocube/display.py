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

from . import (
    backlight, contract, network, pairing, predict, touch, units, wallpaper,
    weather,
)
from .config import IDENTIFY_KEY, SCREEN_PNG, Config, admin_url, merged_thresholds
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
# How long a tap keeps the classic footer up over the ambient screen. Long
# enough to reach the control you meant, short enough that the screen goes
# back to being a picture on its own.
CONTROLS_SECONDS = 8              # how long the settings QR stays up

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
    return {"tidepool": "TWIIST", "nightscout": "NS"}.get(stype, "TRIO")


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
        # Ambient mode. The index is into the *drawable* people, recomputed
        # each frame — somebody with no data at all is skipped rather than
        # given a blank turn, so the list this indexes can change under it.
        self._rot_index = 0
        self._rot_started = time.monotonic()
        # Decoded and scaled once, then kept: a 7" Pi cannot afford to
        # rescale a photograph every frame. See wallpaper.Surfaces.
        self._art = wallpaper.Surfaces(config.database)
        # The flat scrim and its centre-left radial, composited once per
        # frame from a surface rebuilt only when the dim actually changes.
        self._dim_cache: tuple[tuple, pygame.Surface] | None = None
        # A tap in ambient mode brings the classic footer back for a few
        # seconds, which is where the theme toggle and the settings QR live.
        self._controls_until = 0.0
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
        elif self.config.display.layout == "rotate":
            # Ambient mode has no visible chrome to aim at, so a tap
            # anywhere brings the classic footer back for a few seconds —
            # the theme toggle and the settings QR live in it, and there is
            # nowhere else on this screen to reach them.
            if self._debounce():
                self._controls_until = time.monotonic() + CONTROLS_SECONDS
                self._wake.set()

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
            ufont = self.font(int(px * 0.5), "num")
            uimg = ufont.render(unit, True, self.pal.dim)
            baseline = topleft[1] + vfont.get_ascent()
            surface.blit(uimg, (topleft[0] + vimg.get_width() + int(px * 0.08),
                                baseline - ufont.get_ascent()))

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
        """3h history + 2h forecast: range band, trace, cone, dotted forecast.

        Every value here is mg/dL, including the axis it is plotted on —
        the two units are a linear scale apart, so the curve is the same
        shape either way and only the two numbers written on it change.
        """
        surface = self.screen
        pal = self.pal
        shown_in = self.config.display.units
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
        self.text(surface, units.fmt(th["high"], shown_in), lab_px, pal.faint,
                  topright=(chart.right - 5, band_top + 2))
        if band_bot - band_top > lab_px * L["chart_band_thin_ratio"]:
            # Skip the lower bound when the band is too thin to hold it.
            self.text(surface, units.fmt(th["low"], shown_in), lab_px,
                      pal.faint, bottomright=(chart.right - 5, band_bot - 2))

        # Dashed "now" divider between measured past and forecast.
        x_now = X(now_ms)
        y = chart.top
        while y < chart.bottom:
            pygame.draw.line(surface, pal.line, (x_now, y),
                             (x_now, min(y + 4, chart.bottom)), 1)
            y += 9

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
            self.pal.in_range if age_min is not None and age_min <= 7
            else self.pal.high
            if age_min is not None and age_min <= dc.stale_minutes
            else self.pal.low
        )
        badge = f"{source_label(user_cfg)} · {age_compact(now_ms, snap.sgv_date)}"
        badge_rect = self.label(surface, badge, int(h * 0.032), self.pal.dim,
                                topright=(right, top + int(h * 0.012)))
        pygame.draw.circle(surface, dot_color,
                           (badge_rect.left - int(h * 0.035),
                            badge_rect.centery), max(3, int(h * 0.011)))

        # Big glucose number, left-aligned; arrow/delta/unit column right of it.
        # Blit so the digits' cap top (not the font's line box) lands at
        # num_top — Space Grotesk carries a lot of internal leading.
        shown_in = self.config.display.units
        sgv_str = units.fmt(snap.sgv, shown_in)
        num_px = int(h * L["num_px_h"])
        num_font = self.font(num_px, "num")
        num_img = num_font.render(sgv_str, True, color)
        # Tiny panels: shrink the number so it and the trend column fit.
        max_num_w = (right - left) - int(w * 0.26)
        if num_img.get_width() > max_num_w:
            num_px = max(12, int(num_px * max_num_w / num_img.get_width()))
            num_font = self.font(num_px, "num")
            num_img = num_font.render(sgv_str, True, color)
        cap = int(num_px * L["num_cap_ratio"])
        num_top = rect.top + int(h * L["num_top_h"])
        surface.blit(num_img,
                     (left - int(num_px * 0.05),
                      num_top - (num_font.get_ascent() - cap)))

        col_x = left + num_img.get_width() + int(w * 0.04)
        if not stale and snap.direction in DIRECTION_ANGLES:
            arrow_size = int(h * 0.06)
            self.draw_arrow(surface, (col_x + arrow_size, num_top + cap * 0.16),
                            arrow_size, snap.direction, color)
        if not stale and snap.delta is not None:
            delta_font = self.font(int(h * 0.08), "num-med")
            delta_img = delta_font.render(units.fmt_delta(snap.delta, shown_in),
                                          True, self.pal.fg)
            surface.blit(delta_img, (col_x, num_top + int(cap * 0.36)))
        self.label(surface, units.label(shown_in), int(h * 0.027),
                   self.pal.faint,
                   topleft=(col_x, num_top + int(cap * 0.80)))

        # FORECAST 2H header: label — rule — value + arrival time.
        fy = rect.top + int(h * L["forecast_y_h"])
        horizons, future, fc_source = (None, None, None)
        if not stale:
            horizons, future, fc_source = predict.predict(snap, now_ms)
        lab_rect = self.label(surface, "FORECAST 2H", int(h * 0.031),
                              self.pal.dim, midleft=(left, fy))
        rule_end = right
        if horizons and 120 in horizons:
            eta = time.strftime("%H:%M",
                                time.localtime((now_ms + 120 * 60000) / 1000))
            eta_rect = self.label(surface, eta, int(h * 0.031), self.pal.faint,
                                  midright=(right, fy))
            tilde = "~" if fc_source == "est" else ""
            value_color = self.glucose_color(horizons[120], False, th)
            val_rect = self.text(surface,
                                 f"{tilde}{units.fmt(horizons[120], shown_in)}",
                                 int(h * 0.055), value_color, kind="num",
                                 midright=(eta_rect.left - int(w * 0.025), fy))
            rule_end = val_rect.left - int(w * 0.03)
        pygame.draw.line(surface, self.pal.line,
                         (lab_rect.right + int(w * 0.03), fy), (rule_end, fy))

        # Chart with its time axis.
        chart = pygame.Rect(left, rect.top + int(h * L["chart_top_h"]),
                            right - left, int(h * L["chart_height_h"]))
        self.draw_chart(chart, snap, stale, th, future,
                        fc_source == "est", now_ms)
        ax_y = chart.bottom + int(h * L["chart_axis_gap_h"])
        for minutes, lab in ((-180, "-3H"), (-120, "-2H"), (-60, "-1H"),
                             (0, "NOW"), (60, "+1H"), (120, "+2H")):
            x = chart.left + (minutes + 180) / 300 * chart.width
            self.label(surface, lab, int(h * 0.026), self.pal.faint,
                       midtop=(x, ax_y))

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
            self.label(surface, lab, int(h * 0.028), self.pal.dim,
                       topleft=(x, labels_y))
            value_y = labels_y + int(h * 0.048)
            self.stat_value(surface, value, unit,
                            int(h * L["stats_value_px_h"]), (x, value_y))
            if sub:
                self.label(surface, sub, int(h * 0.024), self.pal.faint,
                           topleft=(x, value_y + int(h * 0.1)))

        # Urgent readings get a colored border to catch the eye across a room.
        if color == self.pal.urgent:
            pygame.draw.rect(surface, self.pal.urgent, rect.inflate(-6, -6), 3,
                             border_radius=12)

    # ---- ambient mode ----

    def _ambient_dim(self, size, strength: float) -> pygame.Surface:
        """The scrim between the art and the chrome, built once and kept.

        Two layers in one surface. The flat dim is what makes any
        photograph a background rather than a competitor; the centre-left
        radial is what keeps the number readable when the art has
        something bright exactly where the digits sit — without it, a
        picture with a pale patch behind the reading wins.

        Cached on (size, strength) because it is the same every frame and
        costs a full-panel per-pixel pass to build.
        """
        key = (size, round(strength, 3))
        if self._dim_cache and self._dim_cache[0] == key:
            return self._dim_cache[1]

        width, height = size
        layer = pygame.Surface(size, pygame.SRCALPHA)
        layer.fill((10, 12, 15, int(max(0.0, min(1.0, strength)) * 255)))

        # Ellipse centred at (36%, 52%), radii (58%, 56%), opaque at the
        # centre and gone by 72% of the radius. Drawn as concentric
        # ellipses because pygame has no radial gradient; 48 steps is
        # under the eye's banding threshold at this alpha.
        radial = pygame.Surface(size, pygame.SRCALPHA)
        cx, cy = int(width * 0.36), int(height * 0.52)
        rx, ry = int(width * 0.58), int(height * 0.56)
        steps = 48
        for step in range(steps, 0, -1):
            fraction = step / steps
            alpha = int(0.58 * 255 * max(0.0, 1 - fraction / 0.72))
            if alpha <= 0:
                continue
            box = pygame.Rect(0, 0, int(rx * 2 * fraction), int(ry * 2 * fraction))
            box.center = (cx, cy)
            pygame.draw.ellipse(radial, (10, 12, 15, alpha), box)
        layer.blit(radial, (0, 0))

        self._dim_cache = (key, layer)
        return layer

    def draw_sparkline(self, box: pygame.Rect, snap: UserSnapshot, th: dict,
                       future, color, now_ms: int) -> None:
        """Three hours behind and two ahead, in a plate the size of a stamp.

        Its own function rather than a smaller draw_chart: that one writes
        to self.screen whatever surface it is handed, carries an axis, a
        cone and a halo, and is sized for a panel. This is the same maths
        with none of the furniture.
        """
        values = [v for _, v in snap.history] + [v for _, v in (future or [])]
        if not values:
            return
        lo, hi = min(values) - 12, max(values) + 12
        if hi - lo < 1:
            return
        t0 = now_ms - 180 * 60 * 1000

        def X(t):
            return box.left + (t - t0) / (300 * 60 * 1000) * box.width

        def Y(v):
            return box.bottom - (v - lo) / (hi - lo) * box.height

        # The target band, but only when both its edges are inside the
        # plate. When the whole plotted range sits inside the target, a
        # band would fill the box and read as a stray grey rectangle — so
        # that case gets a baseline instead, which says the same thing.
        band_top, band_bottom = Y(th["high"]), Y(th["low"])
        if band_top > box.top + 1 and band_bottom < box.bottom - 1:
            pygame.draw.rect(self.screen, self.pal.band,
                             pygame.Rect(box.left, int(band_top), box.width,
                                         max(1, int(band_bottom - band_top))))
        else:
            pygame.draw.line(self.screen, self.pal.line,
                             (box.left, box.bottom - 1),
                             (box.right, box.bottom - 1))

        history = [(X(t), Y(v)) for t, v in snap.history]
        if len(history) > 1:
            pygame.draw.aalines(self.screen, self.pal.trace, False, history)
        # Dashed, and deliberately not the mark measured data gets: a
        # forecast drawn like a reading is a forecast somebody will read
        # as one.
        if future and len(future) > 1:
            points = [(X(t), Y(v)) for t, v in future]
            for index in range(0, len(points) - 1, 2):
                pygame.draw.aaline(self.screen, color, points[index],
                                   points[index + 1])
        if snap.sgv is not None and snap.sgv_date:
            pygame.draw.circle(self.screen, color,
                               (int(X(snap.sgv_date)), int(Y(snap.sgv))), 3)

    def draw_state_ring(self, rect: pygame.Rect, color, urgent: bool) -> None:
        """The border that carries the state, whatever the art is doing.

        This is the part of ambient mode that is not decoration. A number
        over a photograph is harder to read across a room than a number on
        black, and the ring is what puts the state back — at ten feet it is
        the only thing still legible. It generalises the border the classic
        panel already draws for an urgent reading.
        """
        width = 3 if urgent else 2
        if urgent:
            # An inner glow, so urgent reads as urgent from the doorway.
            glow = pygame.Surface(rect.size, pygame.SRCALPHA)
            depth = max(6, int(min(rect.width, rect.height) * 0.12))
            for step in range(depth):
                alpha = int(56 * (1 - step / depth) ** 2)
                if alpha <= 0:
                    continue
                pygame.draw.rect(glow, (*color, alpha),
                                 pygame.Rect(step, step,
                                             rect.width - step * 2,
                                             rect.height - step * 2), 1)
            self.screen.blit(glow, rect.topleft)
            pygame.draw.rect(self.screen, color, rect, width)
            return
        ring = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(ring, (*color, 102),
                         pygame.Rect(0, 0, rect.width, rect.height), width)
        self.screen.blit(ring, rect.topleft)

    def draw_weather_mark(self, center, size: int, code: int, color) -> None:
        """Sun, cloud, rain, snow or fog — drawn, like the sun in the footer."""
        kind = weather.mark_for(code)
        radius = size * 0.34
        cx, cy = center
        if kind in ("clear", "partly"):
            sun = (cx - size * 0.12, cy - size * 0.12) if kind == "partly" else (cx, cy)
            pygame.draw.circle(self.screen, color,
                               (int(sun[0]), int(sun[1])), int(radius), 2)
            for i in range(8):
                angle = i * math.pi / 4
                pygame.draw.line(
                    self.screen, color,
                    (sun[0] + math.cos(angle) * radius * 1.45,
                     sun[1] + math.sin(angle) * radius * 1.45),
                    (sun[0] + math.cos(angle) * radius * 1.9,
                     sun[1] + math.sin(angle) * radius * 1.9), 2)
        if kind in ("partly", "cloudy", "rain", "snow", "storm", "fog"):
            cloud = pygame.Rect(0, 0, int(size * 0.8), int(size * 0.34))
            cloud.center = (int(cx + size * 0.05),
                            int(cy + (size * 0.16 if kind == "partly" else 0)))
            pygame.draw.ellipse(self.screen, color, cloud,
                                0 if kind != "fog" else 2)
        if kind in ("rain", "storm"):
            for i in range(3):
                x = cx - size * 0.24 + i * size * 0.24
                pygame.draw.line(self.screen, color,
                                 (x, cy + size * 0.26),
                                 (x - size * 0.06, cy + size * 0.46), 2)
        if kind == "snow":
            for i in range(3):
                x = cx - size * 0.24 + i * size * 0.24
                pygame.draw.circle(self.screen, color,
                                   (int(x), int(cy + size * 0.36)), 2)

    def _ambient_people(self, users, snaps):
        """Who ambient mode can actually show, and who holds the screen.

        Somebody with no data at all is skipped rather than given a turn —
        a rotation that stops for fifteen seconds on an empty panel is a
        display that looks broken. If nobody has anything, everybody is
        shown, because the alternative is a blank screen with no
        explanation.
        """
        pairs = [(u, s) for u, s in zip(users, snaps) if s.sgv is not None]
        return pairs or list(zip(users, snaps))

    def _ambient_turn(self, people, now_ms: int) -> int:
        """Whose turn it is, advancing the rotation when the interval is up.

        An urgent reading holds the screen. Ambient mode exists to make the
        panel prettier and it must not make an urgent reading quieter — so
        the rotation stops on somebody who is in trouble until they are out
        of it, rather than moving on after fifteen seconds.
        """
        if not people:
            return 0
        self._rot_index %= len(people)
        dc = self.config.display

        def is_urgent(pair) -> bool:
            user_cfg, snap = pair
            if snap.sgv is None or snap.sgv_date is None:
                return False
            if now_ms - snap.sgv_date > dc.stale_minutes * 60 * 1000:
                return False
            th = merged_thresholds(dc, user_cfg)
            return snap.sgv <= th["urgent_low"] or snap.sgv >= th["urgent_high"]

        urgent = [i for i, pair in enumerate(people) if is_urgent(pair)]
        if urgent:
            if self._rot_index not in urgent:
                self._rot_index = urgent[0]
                self._rot_started = time.monotonic()
            return self._rot_index

        user_cfg = people[self._rot_index][0]
        seconds = getattr(user_cfg, "rotate_seconds", None) or dc.rotate_seconds
        seconds = max(3.0, float(seconds))
        if time.monotonic() - self._rot_started >= seconds and len(people) > 1:
            self._rot_index = (self._rot_index + 1) % len(people)
            self._rot_started = time.monotonic()
        return self._rot_index

    def _ambient_seconds_left(self) -> float:
        """How long until the rotation moves on, for the wait in run()."""
        dc = self.config.display
        people = self.config.users
        if dc.layout != "rotate" or len(people) < 2:
            return 0.0
        seconds = max(3.0, float(dc.rotate_seconds or 12))
        return max(0.0, seconds - (time.monotonic() - self._rot_started))

    def draw_ambient(self, users, snaps) -> None:
        """One person, full bleed, everything else anchored to a corner.

        The design is "2b — Edge-lit calm": no bars at all, so the middle
        of the artwork stays visible and the reading sits over the part of
        it that has been dimmed for exactly that purpose.

        Every number here is the handoff's, expressed as a ratio of the
        panel the way the rest of this file does, so it holds its
        proportions on something that is not 800x480.
        """
        screen = self.screen
        width, height = screen.get_width(), screen.get_height()
        s = min(width, height)
        dc = self.config.display
        now_ms = int(time.time() * 1000)

        people = self._ambient_people(users, snaps)
        index = self._ambient_turn(people, now_ms)
        user_cfg, snap = people[index]

        th = merged_thresholds(dc, user_cfg)
        stale = (snap.sgv_date is None
                 or now_ms - snap.sgv_date > dc.stale_minutes * 60 * 1000)
        color = self.glucose_color(snap.sgv, stale, th)
        urgent = (not stale and snap.sgv is not None
                  and (snap.sgv <= th["urgent_low"]
                       or snap.sgv >= th["urgent_high"]))

        # 1. the art, 2. the scrim over it.
        art = self._art.get(wallpaper.resolve(dc, user_cfg), (width, height))
        if art is not None:
            screen.blit(art, (0, 0))
            strength = (dc.wallpaper_dim or 0) / 100
            if self.pal.name == "dark" and backlight.is_night(
                    time.localtime().tm_hour, dc.night_from_hour,
                    dc.night_to_hour):
                strength = min(0.88, strength + (dc.night_dim_boost or 0) / 100)
            screen.blit(self._ambient_dim((width, height), strength), (0, 0))

        inset = int(s * 0.071)          # 34px at 480
        top_inset = int(s * 0.058)      # 28px
        fg = (233, 237, 241)
        secondary = (138, 147, 156)     # a step brighter than `dim`, for art
        mark = (198, 205, 212)

        # --- top left: who this is ---
        avatar_r = int(s * 0.031)
        avatar_c = (inset + avatar_r, top_inset + avatar_r)
        pygame.draw.circle(screen, color, avatar_c, avatar_r)
        initial = (getattr(user_cfg, "name", "") or "?").strip()[:1].upper()
        self.text(screen, initial, int(s * 0.029), self.pal.bg, kind="num",
                  center=avatar_c)
        name_x = avatar_c[0] + avatar_r + int(s * 0.027)
        self.label(screen, getattr(user_cfg, "name", "") or "", int(s * 0.027),
                   fg, tracking=0.23,
                   midleft=(name_x, avatar_c[1] - int(s * 0.014)))
        self.label(screen,
                   f"{source_label(user_cfg)} · {age_compact(now_ms, snap.sgv_date)}",
                   int(s * 0.023), secondary, tracking=0.18,
                   midleft=(name_x, avatar_c[1] + int(s * 0.016)))

        # --- top right: the clock, and the weather if it knows where it is ---
        right = width - inset
        clock_px = int(s * 0.117)
        now_local = time.localtime()
        if dc.time_format == 12:
            hour = now_local.tm_hour % 12 or 12
            meridiem = "AM" if now_local.tm_hour < 12 else "PM"
        else:
            hour, meridiem = f"{now_local.tm_hour:02d}", ""
        clock = f"{hour}:{now_local.tm_min:02d}"
        # The clock and its meridiem are one right-aligned group, so the
        # clock has to make room for the AM before it is placed — right
        # -aligning the clock on its own puts the meridiem off the panel.
        gap = int(s * 0.012)
        meridiem_px = int(s * 0.025)
        meridiem_w = (self.font(meridiem_px).size(meridiem)[0]
                      + int(meridiem_px * 0.22) * max(0, len(meridiem) - 1)
                      + gap) if meridiem else 0
        clock_rect = self.text(screen, clock, clock_px, fg, kind="num-med",
                               topright=(right - meridiem_w, int(s * 0.05)))
        if meridiem:
            # On the clock's own baseline rather than its box: Space
            # Grotesk carries enough leading that aligning the boxes would
            # float the AM well below the digits.
            baseline = clock_rect.top + self.font(clock_px, "num-med").get_ascent()
            self.label(screen, meridiem, meridiem_px, secondary,
                       bottomleft=(clock_rect.right + gap, baseline))
        date_rect = self.label(
            screen, time.strftime("%a %d %b").upper(), int(s * 0.023),
            (154, 164, 174), tracking=0.25,
            topright=(right, clock_rect.bottom + int(s * 0.021)))

        reading = weather.current(self.store)
        if reading:
            # Right-aligned, laid out right to left so the row ends flush
            # with the clock above it: the range, then the temperature,
            # then the condition mark.
            row_y = date_rect.bottom + int(s * 0.045)
            cursor = right
            if reading.get("range"):
                range_rect = self.label(screen, reading["range"],
                                        int(s * 0.021), secondary,
                                        tracking=0.2, midright=(cursor, row_y))
                cursor = range_rect.left - int(s * 0.019)
            temp_rect = self.text(screen, reading["temp"], int(s * 0.042), fg,
                                  kind="num-med", midright=(cursor, row_y))
            mark_size = int(s * 0.035)
            self.draw_weather_mark(
                (temp_rect.left - int(s * 0.019) - mark_size // 2, row_y),
                mark_size, reading.get("code", 0),
                mark if reading.get("fresh") else secondary)

        # --- the hero: the reading itself ---
        hero_y = int(height * 0.483)
        num_px = int(height * 0.417)
        shown_in = dc.units
        sgv_text = units.fmt(snap.sgv, shown_in) if not stale else units.fmt(
            snap.sgv, shown_in)
        num_color = self.pal.stale if stale else color
        num_font = self.font(num_px, "num")
        num_img = num_font.render(sgv_text, True, num_color)
        # Shrink to leave the trend column room, exactly as the classic
        # panel does — a 200px number and a three-digit reading in mmol/L
        # would otherwise run into it.
        max_w = int(width * 0.52)
        if num_img.get_width() > max_w:
            num_px = max(24, int(num_px * max_w / num_img.get_width()))
            num_font = self.font(num_px, "num")
            num_img = num_font.render(sgv_text, True, num_color)
        cap = int(num_px * 0.70)
        num_top = hero_y - cap // 2
        screen.blit(num_img, (int(s * 0.083) - int(num_px * 0.05),
                              num_top - (num_font.get_ascent() - cap)))

        col_x = int(s * 0.083) + num_img.get_width() + int(s * 0.046)
        # Hidden when the reading is old, exactly as the classic panel
        # does: a trend arrow on a stale number is a claim about now that
        # nothing supports.
        if not stale:
            arrow_size = int(s * 0.113)
            self.draw_arrow(screen, (col_x + arrow_size // 2,
                                     num_top + int(cap * 0.20)),
                            arrow_size, snap.direction, color)
            self.text(screen, units.fmt_delta(snap.delta, shown_in),
                      int(s * 0.088), fg, kind="num-med",
                      topleft=(col_x, num_top + int(cap * 0.42)))
        self.label(screen, units.label(shown_in), int(s * 0.023), secondary,
                   tracking=0.24, topleft=(col_x, num_top + int(cap * 0.82)))

        # --- the forecast, and three hours of history ---
        forecast_y = height - int(s * 0.183)
        horizons, future, fc_source = (None, None, None)
        if not stale:
            horizons, future, fc_source = predict.predict(snap, now_ms)
        left = int(s * 0.083)
        if horizons and 120 in horizons:
            label_rect = self.label(screen, "2H", int(s * 0.023), secondary,
                                    tracking=0.24, midleft=(left, forecast_y))
            prefix = "~" if fc_source == "est" else ""
            value_rect = self.text(
                screen, prefix + units.fmt(horizons[120], shown_in),
                int(s * 0.046), self.glucose_color(horizons[120], False, th),
                kind="num",
                midleft=(label_rect.right + int(s * 0.029), forecast_y))
            spark_left = value_rect.right + int(s * 0.029)
        else:
            spark_left = left
        spark = pygame.Rect(spark_left, forecast_y - int(s * 0.042),
                            int(s * 0.437), int(s * 0.083))
        if spark.right < width - inset:
            self.draw_sparkline(spark, snap, th, future, color, now_ms)

        # --- bottom left: insulin, carbs, and the state in words ---
        bottom = height - int(s * 0.062)
        parts = [
            ("IOB", f"{snap.iob:.1f}U" if snap.iob is not None else "--"),
            ("COB", f"{snap.cob:.0f}G" if snap.cob is not None else "--"),
        ]
        x = left
        for name, value in parts:
            name_rect = self.label(screen, name, int(s * 0.025), (154, 164, 174),
                                   tracking=0.22, midleft=(x, bottom))
            value_rect = self.label(screen, value, int(s * 0.025), fg,
                                    tracking=0.22,
                                    midleft=(name_rect.right + int(s * 0.021),
                                             bottom))
            x = value_rect.right + int(s * 0.046)
        if stale:
            state = "NO READING"
        elif urgent:
            state = ("URGENT LOW" if snap.sgv <= th["urgent_low"]
                     else "URGENT HIGH")
        elif snap.sgv is None:
            state = "NO READING"
        elif snap.sgv < th["low"]:
            state = "BELOW RANGE"
        elif snap.sgv > th["high"]:
            state = "ABOVE RANGE"
        else:
            state = "IN RANGE"
        self.label(screen, state, int(s * 0.025),
                   self.pal.stale if stale else color, tracking=0.22,
                   midleft=(x, bottom))

        # --- bottom right: status, whose turn it is, and the way in ---
        cursor = width - inset
        cells = len(self.QR_GLYPH)
        cell = max(2, int(s * 0.0063))
        qr_size = cell * cells
        qr_box = pygame.Rect(0, 0, qr_size, qr_size)
        qr_box.midright = (cursor, bottom)
        for row, line in enumerate(self.QR_GLYPH):
            for column, glyph in enumerate(line):
                if glyph == "X":
                    pygame.draw.rect(screen, (122, 132, 142),
                                     pygame.Rect(qr_box.left + column * cell,
                                                 qr_box.top + row * cell,
                                                 cell, cell))
        cursor = qr_box.left - int(s * 0.033)

        if len(people) > 1:
            dot_r = max(2, int(s * 0.0063))
            gap = dot_r * 2 + int(s * 0.015)
            for i in range(len(people) - 1, -1, -1):
                centre = (cursor - dot_r, bottom)
                if i == index:
                    pygame.draw.circle(screen, fg, centre, dot_r)
                else:
                    faint = pygame.Surface((dot_r * 2, dot_r * 2),
                                           pygame.SRCALPHA)
                    pygame.draw.circle(faint, (233, 237, 241, 82),
                                       (dot_r, dot_r), dot_r)
                    screen.blit(faint, (centre[0] - dot_r, centre[1] - dot_r))
                cursor -= gap
            cursor -= int(s * 0.018)

        if self._pending_update().get("latest"):
            self.label(screen, "UPDATE", int(s * 0.021), self.pal.high,
                       tracking=0.2, midright=(cursor, bottom))

        # The touch targets. In ambient mode the settings mark is the only
        # thing with a target of its own; a tap anywhere else brings the
        # classic footer back, which is where the theme toggle lives.
        self._qr_rect = qr_box.inflate(int(s * 0.05), int(s * 0.05))
        self._toggle_rect = pygame.Rect(0, 0, 0, 0)

        self.draw_state_ring(pygame.Rect(0, 0, width, height), color, urgent)

        # The revealed footer, over the bottom of the art, for a few
        # seconds after a tap.
        if time.monotonic() < self._controls_until:
            footer = self._footer_rect()
            plate = pygame.Surface(footer.size, pygame.SRCALPHA)
            plate.fill((10, 12, 15, 224))
            screen.blit(plate, footer.topleft)
            self._qr_rect, self._toggle_rect = self._controls_for(footer)
            self.draw_footer(footer)

    def _split_page(self, pairs):
        """Everyone, or one page of them when a cap is set.

        Four people on a 7" panel is four 200px columns and a number too
        small to read from a doorway, which is what the cap is for: show
        two, then the other two, on the interval the rotation uses.
        """
        cap = self.config.display.split_max
        if not cap or cap >= len(pairs) or cap < 1:
            self._rot_index = 0
            return pairs
        pages = (len(pairs) + cap - 1) // cap
        seconds = max(3.0, float(self.config.display.rotate_seconds or 12))
        if time.monotonic() - self._rot_started >= seconds:
            self._rot_index = (self._rot_index + 1) % pages
            self._rot_started = time.monotonic()
        page = self._rot_index % pages
        return pairs[page * cap:(page + 1) * cap] or pairs[:cap]

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
        if (time.monotonic() - self._tap_flash < 0.35
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
            notice_w = int(len(notice) * px * 0.85)
            x = when_rect.right + int(px * 2.2)
            if x + notice_w < rect.right - int(px * 10):
                self.label(surface, notice, px, self.pal.high,
                           midleft=(x, rect.centery))

        # The mark sits in the middle of the footer, and gives way to
        # anything that has something to say: the update notice is
        # actionable, this is decoration.
        mark_px = max(9, int(px * 0.92))
        mark_size = int(mark_px * 1.35)
        mark_w = mark_size + int(mark_px * 0.6) + int(len("GLUCOCUBE") * mark_px * 0.85)
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
            # Moon: tapping goes back to dark mode. Carved on its own
            # surface — pygame.draw writes alpha rather than blending, so
            # the second circle punches a genuinely transparent bite and
            # whatever sits behind the footer shows through it.
            side = int(icon_r * 2) + 2
            moon = pygame.Surface((side, side), pygame.SRCALPHA)
            c = side // 2
            pygame.draw.circle(moon, color, (c, c), icon_r * 0.8)
            pygame.draw.circle(moon, (0, 0, 0, 0),
                               (c + icon_r * 0.45, c - icon_r * 0.3),
                               icon_r * 0.7)
            surface.blit(moon, (icon_c[0] - c, icon_c[1] - c))

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
        if label_right - int(len(label) * px * 0.85) >= self._qr_rect.left:
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

        # A display that has asked GlucoCore to pair it shows that instead:
        # scanning it needs no address, no password and nothing typed, and
        # a phone that is already signed in finishes the job in one tap.
        approve = pairing.public_state(self.store).get("approve_url") or ""
        if approve:
            self.text(screen, "Scan to add this display to GlucoCore",
                      int(s * 0.045), self.pal.dim, midtop=(cx, int(h * 0.17)))
        else:
            self.text(screen, "Scan from a phone on this network to set up",
                      int(s * 0.045), self.pal.dim, midtop=(cx, int(h * 0.17)))

        # The setup code carries the admin key so that scanning it opens
        # setup outright. The address printed below deliberately does not:
        # it is for typing, and the login for it is printed with it.
        target = approve or self._with_key(primary)
        qr = (self._qr_surface(target, int(s * 0.46))
              if approve or self.config.admin_port else None)
        if qr:
            rect = qr.get_rect(center=(cx, int(h * 0.52)))
            screen.blit(qr, rect)
            info_y = rect.bottom + int(s * 0.035)
        else:
            info_y = int(h * 0.45)

        if approve:
            # Nothing under a GlucoCore code but the way in without one:
            # the address of this display's own settings page.
            self.text(screen, "or set it up at", int(s * 0.038),
                      self.pal.faint, midtop=(cx, info_y))
            self.text(screen, primary, int(s * 0.045), self.pal.dim,
                      midtop=(cx, info_y + int(s * 0.05)))
            return
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

    def _identify_left(self) -> float:
        """Seconds this display should still be waving, if it was asked to."""
        until = self.store.get_params(IDENTIFY_KEY).get("until") or 0
        return max(0.0, (float(until) - time.time() * 1000) / 1000)

    def draw_identify(self):
        """Unmissable, and gone by itself.

        The point is telling one display from another across a room, so it
        flashes rather than merely captioning itself: a band that alternates
        every half second reads as "this one" from the doorway.
        """
        screen = self.screen
        w, h = screen.get_width(), screen.get_height()
        s = min(w, h)
        on = int(time.monotonic() * 2) % 2 == 0
        band = pygame.Rect(0, 0, w, int(h * 0.22))
        band.center = (w // 2, h // 2)
        pygame.draw.rect(screen, self.pal.in_range if on else self.pal.bg,
                         band)
        pygame.draw.rect(screen, self.pal.in_range, band,
                         width=max(2, int(s * 0.01)))
        self.text(screen, "here!", int(s * 0.13),
                  self.pal.bg if on else self.pal.in_range, kind="num",
                  center=band.center)

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
            if self._identify_left():
                self.draw_identify()
            self._present()
            return
        users = self.config.users
        snaps = [self.store.snapshot(user.name) for user in users]
        if self.is_unconfigured(snaps):
            self._toggle_rect = self._qr_rect = pygame.Rect(0, 0, 0, 0)
            self._qr_open_until = 0.0
            self.draw_setup_screen()
            if self._identify_left():
                self.draw_identify()
            self._present()
            return
        if self.config.display.layout == "rotate":
            self.draw_ambient(users, snaps)
            if self.qr_open():
                self.draw_settings_qr()
            if self._identify_left():
                self.draw_identify()
            self._present()
            return
        full_w = self.screen.get_width()
        full_h = self.screen.get_height()
        footer = self._footer_rect()
        self._qr_rect, self._toggle_rect = self._controls_for(footer)
        body_h = footer.top
        shown = self._split_page(list(zip(users, snaps)))
        dc = self.config.display
        if dc.split_direction == "columns":
            portrait = False
        elif dc.split_direction == "rows":
            portrait = True
        else:
            portrait = full_h > full_w
        for i, (user, snap) in enumerate(shown):
            if portrait:
                row_h = body_h // len(shown)
                rect = pygame.Rect(0, i * row_h, full_w, row_h)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (int(full_w * 0.03), rect.top),
                                     (full_w - int(full_w * 0.03), rect.top))
            else:
                col_w = full_w // len(shown)
                rect = pygame.Rect(i * col_w, 0, col_w, body_h)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (rect.left, int(body_h * 0.05)),
                                     (rect.left, body_h - int(body_h * 0.03)))
            self.draw_panel(rect, user, snap)
        self.draw_footer(footer)
        if self.qr_open():
            self.draw_settings_qr()
        if self._identify_left():
            self.draw_identify()
        self._present()

    def _present(self) -> None:
        """Put the frame on the panel.

        One place, because there are now four ways out of draw() and the
        fbdev copy is the half that is invisible in tests — the dummy
        driver makes flip() alone look like it worked.
        """
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
            # Once a frame, and free when the answer has not changed. The
            # hour is read here rather than cached so that a device left
            # running dims when the evening arrives, not at the next boot.
            backlight.apply(self.config.display,
                            time.localtime().tm_hour)
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
            identify_left = self._identify_left()
            if identify_left:
                # A flash at one frame a second is not a flash. Also brings
                # the frame that clears it forward to when it runs out.
                timeout = min(timeout, 0.25, identify_left + 0.02)
            # Without these two the rotation lands up to a second late and
            # the revealed footer overstays by the same, because the loop
            # is otherwise asleep for a whole second at a time.
            rotate_left = self._ambient_seconds_left()
            if rotate_left:
                timeout = min(timeout, rotate_left + 0.02)
            controls_left = self._controls_until - time.monotonic()
            if controls_left > 0:
                timeout = min(timeout, controls_left + 0.02)
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
