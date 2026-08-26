"""The panel's backlight, on the devices that have one.

The official 7" display exposes one under /sys/class/backlight, and
writing a number into it is the whole interface. A Pi driving an HDMI
monitor exposes nothing, and neither does a developer's laptop, so every
function here is written to do nothing quietly rather than to fail: a
display that cannot dim must still show glucose.

Two settings drive it, both of them GlucoCore's to say — a daytime
brightness and a night-time one, with the hours between which the night
figure applies. Equal hours mean the night figure is never used, the same
convention the quiet-hours pair uses.
"""

import glob
import logging
import os

log = logging.getLogger("glucocube.backlight")

SYSFS = "/sys/class/backlight"
# Anything under this and the screen reads as off, which is not a state a
# glucose display should be able to be put into from a web form.
MIN_PERCENT = 5

_device: str | None = None
_looked = False
_last: int | None = None


def device() -> str | None:
    """The backlight to write to, or None. Looked up once."""
    global _device, _looked
    if not _looked:
        _looked = True
        found = sorted(glob.glob(os.path.join(SYSFS, "*")))
        _device = found[0] if found else None
        log.info("backlight: %s", _device or "none — brightness is not "
                                            "controllable here")
    return _device


def reset() -> None:
    """Forget what was found and last written. For tests."""
    global _device, _looked, _last
    _device, _looked, _last = None, False, None


def _read_int(path: str) -> int | None:
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def max_raw() -> int | None:
    path = device()
    return _read_int(os.path.join(path, "max_brightness")) if path else None


def set_percent(percent: float) -> bool:
    """Set the backlight, 0–100. False when there is nothing to set.

    Repeating a value is free: the write is skipped, because this is
    called once a second by the frame loop and a sysfs write per frame
    would be a needless wake-up on a device that is otherwise idle.
    """
    global _last
    path = device()
    if not path:
        return False
    ceiling = max_raw()
    if not ceiling:
        return False
    wanted = max(MIN_PERCENT, min(100, int(round(percent))))
    if wanted == _last:
        return True
    raw = max(1, int(round(ceiling * wanted / 100)))
    try:
        with open(os.path.join(path, "brightness"), "w") as handle:
            handle.write(str(raw))
    except OSError as exc:
        # Root writes this; a dev checkout run as a user does not. Once,
        # not once a second.
        if _last is None:
            log.info("backlight is not writable (%s)", exc)
        _last = wanted
        return False
    _last = wanted
    return True


def is_night(hour: int, from_hour, to_hour) -> bool:
    """Whether `hour` falls in the night window, which may cross midnight."""
    try:
        start, end = int(from_hour), int(to_hour)
    except (TypeError, ValueError):
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def level_for(display, hour: int) -> float | None:
    """The brightness this hour calls for, or None to leave the panel alone."""
    day = display.brightness
    night = display.night_brightness
    if is_night(hour, display.night_from_hour, display.night_to_hour):
        if night is not None:
            return night
    return day


def apply(display, hour: int) -> bool:
    """Put the panel where the settings say it should be for this hour."""
    level = level_for(display, hour)
    if level is None:
        return False
    return set_percent(level)
