"""Background art: what to put behind a person, and how to get it there.

Three kinds of value can name a background, and `resolve` is the only place
that knows the difference:

* ``""``           — nothing here; fall through to the display's own.
* ``"none"``       — deliberately nothing, even where the display has art.
                     Somebody who has chosen a picture for themselves in
                     GlucoCore appears on displays they do not own, and the
                     person who owns the wall needs a way to say "not
                     behind them, on mine" that is not the same as "unset".
* ``"bundled:<n>"``— art this device draws itself. Costs the network
                     nothing and works on a display that has never reached
                     GlucoCore.
* 32 hex           — bytes to fetch, cached on disk, keyed by that id.

Two caches, and both matter. The bytes are cached on disk so a restart does
not re-download the wall; the *scaled surface* is cached in memory, because
a 7" Pi redrawing at 1fps cannot afford to decode and rescale a photograph
every frame. A background never changes under its id, so neither cache
needs invalidating — a different picture is a different id.

Nothing here raises into the draw loop. A fetch that fails, a file that has
been truncated, a decoder that does not like a JPEG: all of them answer
None, and the screen falls back to the flat background the layout was
designed to hold up against.
"""

import logging
import math
import os
import re
import threading
from pathlib import Path

from . import glucocore, synclog

log = logging.getLogger("glucocube.wallpaper")

# The id shape GlucoCore mints, and the bundled names it offers. Anything
# else is not a background — a display is never handed something it has to
# parse, so it can never be pointed at a host somebody typed into a form.
ID_RE = re.compile(r"^[0-9a-f]{32}$")
BUNDLED_RE = re.compile(r"^bundled:([a-z0-9][a-z0-9-]{0,31})$")

ETAG_KEY = "__wallpapers"

# Drawn, not shipped. display.py's own rule for the logo — "drawn rather
# than blitted, so it stays crisp on any panel and adds no asset to carry
# around" — applies here more than anywhere: four photographs would be
# megabytes on every SD card, and these are backgrounds that spend their
# life under a 60% scrim. Each is a vertical ramp plus one soft shape.
BUNDLED = {
    "reeds":  ((14, 26, 22), (32, 58, 44), (60, 92, 66)),
    "dusk":   ((28, 20, 38), (58, 34, 56), (104, 56, 62)),
    "tide":   ((10, 22, 38), (18, 46, 72), (36, 84, 110)),
    "slate":  ((16, 18, 21), (34, 38, 44), (58, 64, 72)),
}


def is_id(value: str) -> bool:
    return bool(ID_RE.match(value or ""))


def resolve(display, user) -> str:
    """What goes behind this person: their own, else the display's, else none.

    One line of policy, in one place, because the device and GlucoCore have
    to agree on it — see docs/sugarcube-display-contract.md in GlucoCore.
    """
    own = (getattr(user, "wallpaper", "") or "").strip()
    if own:
        return own
    return (getattr(display, "wallpaper", "") or "").strip()


def cache_dir(database: str) -> Path:
    """Beside the database, which is beside config.json — one place to back up."""
    return Path(database).resolve().parent / "wallpapers"


def cached_path(database: str, wallpaper_id: str) -> Path:
    return cache_dir(database) / wallpaper_id


def _write_atomic(path: Path, data: bytes) -> None:
    """Same discipline as config.write_atomic, for the same reason.

    The draw loop reads these files from another thread; a half-written one
    is a decode error on screen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def ensure(store, database: str, device_token: str, wallpaper_id: str) -> bool:
    """Fetch this background if it is not already on disk. True if it is now.

    The ETag rides in the params table rather than beside the file, so a
    revalidation costs one request and no bytes at all when nothing has
    changed — which is every config push that was about something else.
    """
    if not is_id(wallpaper_id) or not device_token:
        return False
    path = cached_path(database, wallpaper_id)
    etags = store.get_params(ETAG_KEY)
    have = path.exists()
    if have and not etags.get(wallpaper_id):
        # On disk with no ETag recorded: usable, and nothing to revalidate
        # against, so leave it rather than re-downloading on every boot.
        return True
    try:
        data, etag = glucocore.fetch_wallpaper(
            device_token, wallpaper_id, etags.get(wallpaper_id, ""))
    except Exception as exc:  # noqa: BLE001 - never into the draw loop
        log.warning("could not fetch background %s: %s", wallpaper_id, exc)
        synclog.add("wallpaper", "display",
                    f"could not fetch a background: {exc}", ok=False)
        return have
    if data is None:
        return have
    _write_atomic(path, data)
    store.set_params(ETAG_KEY, {wallpaper_id: etag or "-"})
    log.info("fetched background %s (%d bytes)", wallpaper_id, len(data))
    return True


def wanted(display, users) -> set[str]:
    """Every id this config asks for — what to fetch, and what to keep."""
    ids = set()
    for value in [getattr(display, "wallpaper", "")] + [
            getattr(u, "wallpaper", "") for u in users]:
        value = (value or "").strip()
        if is_id(value):
            ids.add(value)
    return ids


def sweep(database: str, keep: set[str]) -> int:
    """Drop cached art nothing on this display names any more.

    A wall that has had a dozen pictures through it should not carry all
    twelve on an SD card forever.
    """
    directory = cache_dir(database)
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in directory.iterdir():
        if entry.is_file() and entry.name not in keep and is_id(entry.name):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def refresh(store, config) -> None:
    """Bring the cache in line with the config. Safe to call on any thread."""
    token = config.glucocore.device_token if config.glucocore else ""
    ids = wanted(config.display, config.users)
    for wallpaper_id in ids:
        ensure(store, config.database, token, wallpaper_id)
    sweep(config.database, ids)


def refresh_async(store, config) -> threading.Thread:
    """Fetch in the background: nothing about art may hold up a reading."""
    thread = threading.Thread(target=refresh, args=(store, config),
                              name="wallpapers", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------- drawing ----

def _draw_bundled(name: str, size: tuple[int, int]):
    """One of the bundled backgrounds, drawn at the size asked for."""
    import pygame

    ramp = BUNDLED.get(name)
    if ramp is None:
        return None
    width, height = size
    surface = pygame.Surface(size)
    top, mid, bottom = ramp
    for y in range(height):
        t = y / max(1, height - 1)
        # Two-stop ramp through the middle colour, so it reads as light
        # falling rather than as a linear fade between two flat colours.
        if t < 0.5:
            a, b, k = top, mid, t * 2
        else:
            a, b, k = mid, bottom, (t - 0.5) * 2
        pygame.draw.line(surface, tuple(
            int(a[i] + (b[i] - a[i]) * k) for i in range(3)),
            (0, y), (width, y))
    # One soft off-centre glow, which is what stops it looking like a
    # gradient and starts it looking like a place.
    glow = pygame.Surface(size, pygame.SRCALPHA)
    cx, cy = int(width * 0.68), int(height * 0.34)
    radius = int(max(width, height) * 0.42)
    for step in range(radius, 0, -max(1, radius // 24)):
        alpha = int(26 * (1 - step / radius) ** 2)
        if alpha <= 0:
            continue
        pygame.draw.circle(glow, (*bottom, alpha), (cx, cy), step)
    surface.blit(glow, (0, 0))
    return surface


def _scale_to_cover(image, size: tuple[int, int]):
    """Fill the panel, cropping the overflow — never letterbox, never stretch."""
    import pygame

    width, height = size
    source_w, source_h = image.get_size()
    if source_w <= 0 or source_h <= 0:
        return None
    scale = max(width / source_w, height / source_h)
    scaled = pygame.transform.smoothscale(
        image, (max(1, math.ceil(source_w * scale)),
                max(1, math.ceil(source_h * scale))))
    out = pygame.Surface(size)
    out.blit(scaled, ((width - scaled.get_width()) // 2,
                      (height - scaled.get_height()) // 2))
    return out


class Surfaces:
    """Decoded, scaled backgrounds, kept by (value, size).

    The cache the frame rate depends on. Decoding a 2 MB JPEG and scaling
    it to 800x480 costs the better part of a second on a Pi Zero; doing it
    once and keeping the result is the difference between a display that
    redraws and one that stutters.

    A miss is cached too — as None — because the expensive thing to repeat
    is the failure: a file that will not decode will not decode next frame
    either, and retrying it every second is how a broken background becomes
    a broken display.
    """

    def __init__(self, database: str):
        self.database = database
        self._cache: dict[tuple[str, tuple[int, int]], object] = {}

    def clear(self) -> None:
        self._cache.clear()

    def get(self, value: str, size: tuple[int, int]):
        """The surface for this value at this size, or None for a flat panel."""
        value = (value or "").strip()
        if not value or value == "none":
            return None
        key = (value, size)
        if key in self._cache:
            return self._cache[key]
        surface = self._build(value, size)
        self._cache[key] = surface
        return surface

    def _build(self, value: str, size: tuple[int, int]):
        import pygame

        bundled = BUNDLED_RE.match(value)
        if bundled:
            try:
                return _draw_bundled(bundled.group(1), size)
            except Exception as exc:  # noqa: BLE001 - a flat panel is fine
                log.warning("could not draw %s: %s", value, exc)
                return None
        if not is_id(value):
            return None
        path = cached_path(self.database, value)
        if not path.exists():
            return None
        try:
            image = pygame.image.load(str(path))
            return _scale_to_cover(image, size)
        except Exception as exc:  # noqa: BLE001 - a corrupt file is not fatal
            log.warning("could not decode background %s: %s", value, exc)
            return None
