"""The weather in the corner of the ambient screen.

Open-Meteo, which needs no API key and no account — the only source that
can be switched on by typing a town name, which is the whole of the setup
this deserves.

Off until somebody says where the device is. The obvious shortcut is to
derive a location from the time zone the clock already knows, and it is a
bad one: it needs a coordinate table in the firmware, and it confidently
shows the wrong town's sky for anyone not in the zone's namesake city.
Nothing is better than wrong here, so `/settings/weather` asks.

The reading lives in the params table rather than in memory, so a device
that boots with no network still has something to show — greyed once it is
old enough that it might be lying.
"""

import json
import logging
import time
import urllib.parse
import urllib.request

from . import synclog
from .sources import BasePoller

log = logging.getLogger("glucocube.weather")

PARAMS_KEY = "__weather"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

POLL_SECONDS = 15 * 60
# Past this the reading is drawn in the dimmer grey. It is not wrong yet,
# but it is old enough that saying so is more honest than not.
STALE_MS = 2 * 60 * 60 * 1000


def mark_for(code) -> str:
    """A WMO weather code as one of the few shapes this display can draw.

    The full table is a hundred codes and the panel is 17 pixels wide at
    this size; the distinctions that survive being drawn that small are
    these seven.
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "clear"
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "rain"


def _get(url: str, params: dict, timeout: float) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}",
                                     headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read()) or {}


def geocode(place: str, timeout: float = 20) -> dict | None:
    """Turn a town name into coordinates, once, when somebody types one.

    Answers the resolved name as well as the numbers, so the settings page
    can say which "Springfield" it decided on rather than silently picking.
    """
    place = (place or "").strip()
    if not place:
        return None
    payload = _get(GEOCODE_URL, {"name": place, "count": 1,
                                 "language": "en", "format": "json"}, timeout)
    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    parts = [first.get("name"), first.get("admin1"), first.get("country")]
    return {
        "latitude": float(first["latitude"]),
        "longitude": float(first["longitude"]),
        "place": ", ".join(p for p in parts if p),
    }


def fetch(latitude: float, longitude: float, units: str,
          timeout: float = 30) -> dict:
    """Now and today's range, in one request."""
    payload = _get(FORECAST_URL, {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": units,
        "timezone": "auto",
    }, timeout)
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    def first(key):
        values = daily.get(key) or []
        return values[0] if values else None

    return {
        "temp": current.get("temperature_2m"),
        "code": current.get("weather_code"),
        "high": first("temperature_2m_max"),
        "low": first("temperature_2m_min"),
        "fetched_at": int(time.time() * 1000),
    }


def current(store, now_ms: int | None = None) -> dict | None:
    """What to draw, already formatted, or None when there is nothing.

    Formatting here rather than in display.py because the rounding and the
    degree sign are part of what was stored, not part of how it is painted.
    """
    stored = store.get_params(PARAMS_KEY)
    if stored.get("temp") is None:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    fetched = stored.get("fetched_at") or 0
    reading = {
        "temp": f"{round(float(stored['temp']))}°",
        "code": stored.get("code") or 0,
        "fresh": (now_ms - fetched) < STALE_MS,
        "range": "",
    }
    high, low = stored.get("high"), stored.get("low")
    if high is not None and low is not None:
        # Two spaces between them, as the design has it: one reads as a
        # single value with a stray letter in it.
        reading["range"] = f"H {round(float(high))}  L {round(float(low))}"
    return reading


class WeatherPoller(BasePoller):
    """Every fifteen minutes, and never in the way of a reading.

    A BasePoller like every other source, so it gets the same backoff, the
    same "refresh now" poke and the same line in the sync log — the weather
    failing should look like any other source failing, because to whoever
    is reading /log that is exactly what it is.
    """

    def __init__(self, weather_config, store):
        super().__init__("weather", "display", POLL_SECONDS, store)
        self.weather = weather_config

    def _poll_once(self) -> None:
        config = self.weather
        if not (config.enabled and config.latitude is not None
                and config.longitude is not None):
            return
        reading = fetch(config.latitude, config.longitude, config.units)
        if reading.get("temp") is None:
            raise ValueError("no temperature in the answer")
        # replace_params, not set_params: a temperature of 0 is a real
        # temperature, and set_params drops falsy values.
        self.store.replace_params(PARAMS_KEY, reading)
        synclog.add("weather", "display",
                    f"{round(float(reading['temp']))}° at "
                    f"{config.place or 'the configured location'}")


def start_poller(config, store) -> WeatherPoller | None:
    """Start it, if this device has been told where it is."""
    weather_config = getattr(config, "weather", None)
    if not weather_config or not weather_config.enabled:
        return None
    if weather_config.latitude is None or weather_config.longitude is None:
        log.info("Weather is on but no location is set; not polling.")
        return None
    poller = WeatherPoller(weather_config, store)
    poller.start()
    return poller
