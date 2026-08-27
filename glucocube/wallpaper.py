"""Backgrounds for the display: quiet shades, and photo wallpapers.

Two kinds live here. The *shades* are drawn from the active palette at
the screen's own size — no asset to carry, and day and night both look
deliberate. The *photos* are bundled in ``wallpapers/`` (see the COPYING
file there for each one's license) and are shown behind a heavy scrim:
darkened at night, washed toward paper in the day.

The one rule is that the glucose numbers stay readable. The shades keep
within a whisper of the flat background they replace, and the scrim over
a photo is strong enough that the type, not the scenery, keeps the room.
"""

import os

import pygame

WALLPAPER_DIR = os.path.join(os.path.dirname(__file__), "wallpapers")

# Resolution the shaded styles are computed at before being smoothed up
# to the panel: high enough that gradients keep their shape, low enough
# that a style renders in well under a frame on a Pi.
SHADE_RES = 96

# How much of the scrim covers a photo, per theme. Night lowers a dark
# veil so light type reads on top; day bleaches the photo toward paper
# so dark type does. Both leave just enough picture to recognize.
SCRIM_DARK = (0, 0, 0, 168)
SCRIM_LIGHT = (255, 255, 255, 214)


def _mix(a, b, t):
    return tuple(
        max(0, min(255, int(round(a[i] + (b[i] - a[i]) * t)))) for i in range(3)
    )


def _shade(pal, t: float):
    """A color t steps off the flat background.

    One "step" is the distance from the background to the target-band
    tint. Positive t moves toward the band (lifted); negative t moves
    the same distance the other way (sunk). Shades keep |t| ≤ ~0.6 so
    the band, drawn at t=1, still reads on top of them.
    """
    away = tuple(2 * pal.bg[i] - pal.band[i] for i in range(3))
    return _mix(pal.bg, pal.band, t) if t >= 0 else _mix(pal.bg, away, -t)


def _shaded(size, px) -> pygame.Surface:
    """Render px(nx, ny) -> color over a low-res grid, smoothed up."""
    w, h = size
    lw = SHADE_RES
    lh = max(2, int(lw * h / max(1, w)))
    low = pygame.Surface((lw, lh))
    for y in range(lh):
        ny = (y + 0.5) / lh
        for x in range(lw):
            low.set_at((x, y), px((x + 0.5) / lw, ny))
    return pygame.transform.smoothscale(low, (w, h))


def _dusk(size, pal) -> pygame.Surface:
    """Gently lit above, settling darker below — a sky at the end of day."""
    def px(nx, ny):
        eased = ny * ny * (3 - 2 * ny)             # smoothstep
        return _shade(pal, 0.5 - eased * 0.9)
    return _shaded(size, px)


def _halo(size, pal) -> pygame.Surface:
    """A soft glow behind the middle of the screen, dimming to the corners."""
    def px(nx, ny):
        dx, dy = nx - 0.5, (ny - 0.42) * 0.8       # wide, slightly high
        d = dx * dx + dy * dy
        return _shade(pal, max(-0.45, 0.5 - d * 3.2))
    return _shaded(size, px)


def _photo(filename):
    """A renderer for one bundled photograph, scrimmed for the theme."""
    def render(size, pal) -> pygame.Surface:
        img = pygame.image.load(os.path.join(WALLPAPER_DIR, filename))
        w, h = size
        iw, ih = img.get_size()
        scale = max(w / iw, h / ih)               # cover, never letterbox
        img = pygame.transform.smoothscale(
            img, (max(w, round(iw * scale)), max(h, round(ih * scale))))
        surface = pygame.Surface(size)
        surface.blit(img, ((w - img.get_width()) // 2,
                           (h - img.get_height()) // 2))
        scrim = pygame.Surface(size, pygame.SRCALPHA)
        scrim.fill(SCRIM_DARK if pal.name == "dark" else SCRIM_LIGHT)
        surface.blit(scrim, (0, 0))
        return surface
    return render


# name -> (label, one-line description, renderer or None for the flat fill)
STYLES = {
    "solid": ("Solid", "The flat color, as it has always been", None),
    "dusk": ("Dusk", "Gently lit above, settling darker below", _dusk),
    "halo": ("Halo", "A soft glow behind the middle of the screen", _halo),
    "ferns": ("Ferns", "Fern fronds in deep shade", _photo("ferns.jpg")),
    "mountain-night": ("Mountain night", "Half Dome under the stars",
                       _photo("mountain-night.jpg")),
    "aurora": ("Aurora", "Green light over a sea arch", _photo("aurora.jpg")),
    "pier": ("Pier at dusk", "A long boardwalk into still water",
             _photo("pier.jpg")),
    "surf": ("Surf", "A wave breaking on turquoise water", _photo("surf.jpg")),
    "dunes": ("Dunes", "Footprints crossing soft sand", _photo("dunes.jpg")),
}
DEFAULT = "solid"
SHADE_NAMES = ("solid", "dusk", "halo")
PHOTO_NAMES = tuple(n for n in STYLES if n not in SHADE_NAMES)

_cache: dict[tuple, pygame.Surface | None] = {}


def normalize(name) -> str:
    """The stored value, or the default for anything unrecognized —
    including values written by a newer version that knew more styles."""
    return name if name in STYLES else DEFAULT


def label(name: str) -> str:
    return STYLES[normalize(name)][0]


def photo_path(name: str) -> str | None:
    """The bundled photo behind a style, for the web UI's previews."""
    if name in PHOTO_NAMES:
        return os.path.join(WALLPAPER_DIR, name + ".jpg")
    return None


def paint(surface: pygame.Surface, name: str, pal) -> None:
    """Fill `surface` with the named style in the palette's colors.

    Rendered once per (style, theme, size) and cached: the draw loop
    calls this every frame, and the answer only changes when someone
    changes the style. A photo that cannot be loaded (a stripped-down
    install, a corrupt card) falls back to the flat color rather than
    taking the dashboard down with it.
    """
    name = normalize(name)
    renderer = STYLES[name][2]
    if renderer is None:
        surface.fill(pal.bg)
        return
    key = (name, pal.name, surface.get_size())
    if key not in _cache:
        if len(_cache) > 8:      # a resize or theme churn; start afresh
            _cache.clear()
        try:
            _cache[key] = renderer(surface.get_size(), pal)
        except (pygame.error, OSError):
            _cache[key] = None
    cached = _cache[key]
    if cached is None:
        surface.fill(pal.bg)
    else:
        surface.blit(cached, (0, 0))
