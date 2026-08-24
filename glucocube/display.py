"""Full-screen pygame dashboard, one panel per person.

Runs without a desktop: on the Pi, SDL's kmsdrm backend draws straight to
the display. On a dev machine it opens a normal window (--windowed).

Design: near-black background, left-aligned type. Each panel shows the
person's name with a source/freshness badge, a huge glucose number with
trend arrow + delta, a FORECAST 2H header row, a 5-hour chart (3h history,
2h forecast with a confidence cone), and an IOB/COB/CARBS/BOLUS stat row.
A footer spans the screen with the date/time and the NIGHT/DAY toggle.
Numerals are Space Grotesk; labels are JetBrains Mono (bundled, OFL).

Taps reach the toggle two ways: SDL events (kmsdrm, or a dev window) and,
because SDL's dummy driver used by the fbdev path delivers none, the
evdev reader in ``touch.py``.
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

from . import network, predict, touch
from .config import SCREEN_PNG, Config, admin_url, merged_thresholds
from .store import Store, UserSnapshot


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


DARK = Palette(
    name="dark",
    bg=(10, 12, 15), band=(20, 25, 30), line=(38, 45, 52),
    fg=(233, 237, 241), dim=(122, 132, 142), faint=(84, 93, 102),
    trace=(157, 165, 174), stale=(96, 104, 112),
    in_range=(95, 222, 150), high=(233, 185, 73), low=(244, 92, 84),
    urgent=(255, 69, 58),
)
LIGHT = Palette(
    name="light",
    bg=(246, 247, 245), band=(233, 235, 230), line=(209, 212, 207),
    fg=(24, 28, 32), dim=(102, 110, 118), faint=(148, 155, 162),
    trace=(122, 130, 138), stale=(170, 176, 182),
    in_range=(16, 148, 72), high=(176, 116, 8), low=(204, 44, 36),
    urgent=(224, 0, 0),
)
THEMES = {p.name: p for p in (DARK, LIGHT)}
THEME_STATE_USER = "__display"     # params-table key for persisted UI state

DIRECTION_ANGLES = {
    "DoubleUp": (-90, 2),
    "SingleUp": (-90, 1),
    "FortyFiveUp": (-45, 1),
    "Flat": (0, 1),
    "FortyFiveDown": (45, 1),
    "SingleDown": (90, 1),
    "DoubleDown": (90, 2),
}

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
        self._toggle_rect = self._toggle_rect_for(self._footer_rect())
        self._last_toggle = 0.0
        self._tap_flash = -99.0        # monotonic time of the last toggle
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
        now = time.monotonic()
        if now - self._last_toggle < 0.5:
            return
        self._last_toggle = now
        self._tap_flash = now
        self.pal = LIGHT if self.pal.name == "dark" else DARK
        self.store.set_params(THEME_STATE_USER, {"theme": self.pal.name})

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
        if self._toggle_rect.collidepoint(pos):
            self.toggle_theme()

    def _footer_rect(self) -> pygame.Rect:
        full_h = self.screen.get_height()
        return pygame.Rect(0, full_h - max(26, int(full_h * 0.072)),
                           self.screen.get_width(), max(26, int(full_h * 0.072)))

    def _toggle_rect_for(self, footer: pygame.Rect) -> pygame.Rect:
        """Touch target for the NIGHT/DAY control.

        Derived from the footer geometry rather than the rendered label:
        taps are tested before the frame is drawn, so the target must not
        depend on text metrics measured during the *previous* frame — that
        is why the first tap after boot used to do nothing.
        """
        px = max(11, int(footer.height * 0.30))
        rect = pygame.Rect(0, 0, max(120, px * 11), max(44, footer.height * 2))
        rect.midright = (footer.right, footer.centery)
        if rect.top < 0:
            rect.top = 0
        return rect

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

    def label(self, surface, s, px, color, kind="mono", tracking=0.22, **anchor):
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

    def draw_chart(self, chart: pygame.Rect, snap: UserSnapshot, stale: bool,
                   th: dict, future, est: bool, now_ms: int):
        """3h history + 2h forecast: range band, trace, cone, dotted forecast."""
        surface = self.screen
        pal = self.pal
        t0 = now_ms - 180 * 60 * 1000
        t1 = now_ms + 120 * 60 * 1000
        values = [v for _, v in snap.history] + [v for _, v in (future or [])]
        if not values:
            values = [th["low"], th["high"]]
        lo = min(min(values), th["low"]) - 18
        hi = max(max(values), th["high"]) + 24

        def X(t):
            return chart.left + (t - t0) / (t1 - t0) * chart.width

        def Y(v):
            return chart.bottom - (v - lo) / (hi - lo) * chart.height

        # Target-range band with its bounds labeled at the right edge.
        band_top, band_bot = int(Y(th["high"])), int(Y(th["low"]))
        pygame.draw.rect(surface, pal.band,
                         (chart.left, band_top, chart.width, band_bot - band_top))
        lab_px = max(10, int(chart.height * 0.15))
        self.text(surface, f"{th['high']:.0f}", lab_px, pal.faint,
                  topright=(chart.right - 5, band_top + 2))
        if band_bot - band_top > lab_px * 2.4:  # skip when the band is thin
            self.text(surface, f"{th['low']:.0f}", lab_px, pal.faint,
                      bottomright=(chart.right - 5, band_bot - 2))

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
            rate = 0.26 if est else 0.17
            upper, lower = [], []
            for t, v in future:
                spread = 4 + (t - now_ms) / 60000 * rate
                upper.append((X(t) - chart.left, max(0, Y(v - spread) - chart.top)))
                lower.append((X(t) - chart.left,
                              min(chart.height, Y(v + spread) - chart.top)))
            cone_color = self.glucose_color(future[-1][1], False, th)
            overlay = pygame.Surface(chart.size, pygame.SRCALPHA)
            pygame.draw.polygon(overlay, (*cone_color, 30),
                                upper + list(reversed(lower)))
            surface.blit(overlay, chart.topleft)

        # History: a smooth neutral line (dots would imply per-reading
        # color), split on >15-minute gaps so sensor outages stay visible.
        trace = pal.stale if stale else pal.trace
        segments, seg, last_t = [], [], None
        for t, v in snap.history:
            if t < t0:
                continue
            if last_t is not None and t - last_t > 15 * 60 * 1000:
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
        dot_r = max(1.6, chart.height * 0.022)
        for t, v in (future or []):
            pygame.draw.circle(surface, self.glucose_color(v, False, th),
                               (X(t), Y(v)), dot_r)

        # "Now" marker: soft halo + solid dot at the latest reading.
        if snap.history:
            t_last, v_last = snap.history[-1]
            cx, cy = X(min(t_last, now_ms)), Y(v_last)
            r = max(4, int(chart.height * 0.075))
            halo = pygame.Surface((r * 6, r * 6), pygame.SRCALPHA)
            pygame.draw.circle(halo, (*color_now, 46), (r * 3, r * 3), r * 2)
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
        pad = int(w * 0.075)
        left, right = rect.left + pad, rect.right - pad

        stale = (
            snap.sgv_date is None
            or now_ms - snap.sgv_date > dc.stale_minutes * 60 * 1000
        )
        color = self.glucose_color(snap.sgv, stale, th)

        # Header: name left; freshness dot + source + reading age right.
        top = rect.top + int(h * 0.055)
        self.label(surface, user_cfg.name, int(h * 0.052), self.pal.fg,
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
        sgv_str = f"{snap.sgv:.0f}" if snap.sgv is not None else "---"
        num_px = int(h * 0.33)
        num_font = self.font(num_px, "num")
        num_img = num_font.render(sgv_str, True, color)
        # Tiny panels: shrink the number so it and the trend column fit.
        max_num_w = (right - left) - int(w * 0.26)
        if num_img.get_width() > max_num_w:
            num_px = max(12, int(num_px * max_num_w / num_img.get_width()))
            num_font = self.font(num_px, "num")
            num_img = num_font.render(sgv_str, True, color)
        cap = int(num_px * 0.70)   # Space Grotesk capHeight = 700/1000 em
        num_top = rect.top + int(h * 0.125)
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
            delta_img = delta_font.render(f"{snap.delta:+.0f}", True, self.pal.fg)
            surface.blit(delta_img, (col_x, num_top + int(cap * 0.36)))
        self.label(surface, "MG/DL", int(h * 0.027), self.pal.faint,
                   topleft=(col_x, num_top + int(cap * 0.80)))

        # FORECAST 2H header: label — rule — value + arrival time.
        fy = rect.top + int(h * 0.48)
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
            val_rect = self.text(surface, f"{tilde}{horizons[120]:.0f}",
                                 int(h * 0.055), value_color, kind="num",
                                 midright=(eta_rect.left - int(w * 0.025), fy))
            rule_end = val_rect.left - int(w * 0.03)
        pygame.draw.line(surface, self.pal.line,
                         (lab_rect.right + int(w * 0.03), fy), (rule_end, fy))

        # Chart with its time axis.
        chart = pygame.Rect(left, rect.top + int(h * 0.53),
                            right - left, int(h * 0.20))
        self.draw_chart(chart, snap, stale, th, future,
                        fc_source == "est", now_ms)
        ax_y = chart.bottom + int(h * 0.022)
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
        labels_y = rect.top + int(h * 0.815)
        col_w = (right - left) / len(stats)
        for idx, (lab, value, unit, sub) in enumerate(stats):
            x = left + int(idx * col_w)
            self.label(surface, lab, int(h * 0.028), self.pal.dim,
                       topleft=(x, labels_y))
            value_y = labels_y + int(h * 0.048)
            self.stat_value(surface, value, unit, int(h * 0.082), (x, value_y))
            if sub:
                self.label(surface, sub, int(h * 0.024), self.pal.faint,
                           topleft=(x, value_y + int(h * 0.1)))

        # Urgent readings get a colored border to catch the eye across a room.
        if color == self.pal.urgent:
            pygame.draw.rect(surface, self.pal.urgent, rect.inflate(-6, -6), 3,
                             border_radius=12)

    def _pending_update(self) -> dict:
        state, checked = self._update_state
        if time.monotonic() - checked > 60:
            state = self.store.get_params("__updates")
            self._update_state = (state, time.monotonic())
        return state if state.get("available") else {}

    def draw_footer(self, rect: pygame.Rect):
        """Date/time left (plus update notice); NIGHT/DAY toggle right."""
        surface = self.screen
        pygame.draw.line(surface, self.pal.line,
                         (rect.left, rect.top), (rect.right, rect.top))
        if time.monotonic() - self._tap_flash < 0.35:
            # Momentary highlight so a tap is visibly acknowledged.
            pygame.draw.rect(surface, self.pal.band,
                             self._toggle_rect.clip(rect).inflate(-2, -6),
                             border_radius=8)
        pad = int(surface.get_width() * 0.028)
        px = max(11, int(rect.height * 0.30))
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

        icon_r = max(6, int(rect.height * 0.14))
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
            url = admin_url(
                network.HOTSPOT_ADDR, self.config.admin_port,
                f"/setup?key={self.config.admin_password}",
            )
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

        qr = (self._qr_surface(primary, int(s * 0.46))
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
            # No toggle on the setup screens: clear the target so taps
            # there can't hit a rect left over from the dashboard.
            self._toggle_rect = pygame.Rect(0, 0, 0, 0)
            self.draw_hotspot_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        users = self.config.users
        snaps = [self.store.snapshot(user.name) for user in users]
        if self.is_unconfigured(snaps):
            self._toggle_rect = pygame.Rect(0, 0, 0, 0)
            self.draw_setup_screen()
            pygame.display.flip()
            if self.fb:
                self.fb.present(self.screen)
            return
        full_w = self.screen.get_width()
        full_h = self.screen.get_height()
        footer = self._footer_rect()
        self._toggle_rect = self._toggle_rect_for(footer)
        body_h = footer.top
        portrait = full_h > full_w
        for i, (user, snap) in enumerate(zip(users, snaps)):
            if portrait:
                row_h = body_h // len(users)
                rect = pygame.Rect(0, i * row_h, full_w, row_h)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (int(full_w * 0.03), rect.top),
                                     (full_w - int(full_w * 0.03), rect.top))
            else:
                col_w = full_w // len(users)
                rect = pygame.Rect(i * col_w, 0, col_w, body_h)
                if i > 0:
                    pygame.draw.line(self.screen, self.pal.line,
                                     (rect.left, int(body_h * 0.05)),
                                     (rect.left, body_h - int(body_h * 0.03)))
            self.draw_panel(rect, user, snap)
        self.draw_footer(footer)
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
