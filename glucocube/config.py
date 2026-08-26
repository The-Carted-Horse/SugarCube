"""Configuration loading for GlucoCube."""

import json
import logging
import os
import subprocess
import tempfile
import time
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("glucocube.config")

# Where the display loop drops live screenshots for the /screen.png endpoint.
SCREEN_PNG = os.path.join(tempfile.gettempdir(), "glucocube-screen.png")


@dataclass
class UserConfig:
    name: str
    port: int
    api_secret: str = ""
    # Optional pull-based source, for users whose data must be fetched rather
    # than pushed to us:
    #   {"type": "tidepool", "email": ..., "password": ..., "poll_seconds": 60}
    #   {"type": "nightscout", "url": ..., "api_secret" or "token": ...,
    #    "poll_seconds": 60}
    source: dict | None = None
    # Optional per-person overrides of the global display thresholds, e.g.
    # {"low": 80, "high": 160}. Keys not present inherit the display defaults.
    thresholds: dict | None = None


@dataclass
class DisplayConfig:
    fullscreen: bool = True
    width: int = 800
    height: int = 480
    units: str = "mg/dL"
    # IANA name, e.g. "Europe/London". Blank leaves the system alone.
    # A fresh image has no time zone set at all, so the clock reads UTC
    # until someone says where the device is.
    timezone: str = ""
    low: float = 70
    high: float = 180
    urgent_low: float = 55
    urgent_high: float = 250
    stale_minutes: float = 12


@dataclass
class GlucoCoreConfig:
    device_id: str = ""
    device_token: str = ""
    hardware_id: str = ""
    # What this display is called in GlucoCore. Kept locally so the
    # settings page can say which device this is without a round trip.
    name: str = ""


@dataclass
class Config:
    users: list[UserConfig] = field(default_factory=list)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    database: str = "glucocube.db"
    admin_port: int = 80            # 0 disables the web admin
    admin_password: str = ""        # empty disables Basic auth
    # True when having no password is a deliberate choice — a device on a
    # network its owner trusts. It grants nothing on its own: the empty
    # password above is what opens the door. All it does is stop the
    # settings page nagging about a password that is missing on purpose.
    admin_password_off: bool = False
    # Which releases this device updates from: "stable" (full releases
    # only) or "beta" (pre-releases too). Read live by the update
    # checker, so flipping it on the settings page takes effect at once.
    update_channel: str = "stable"
    glucocore: GlucoCoreConfig | None = None


# Kept here rather than in updater.py so config.load() can normalise the
# channel without importing the updater (which pulls in the network).
UPDATE_CHANNELS = ("stable", "beta")
CHANNEL_LABELS = {"stable": "Standard", "beta": "Beta"}


def normalize_channel(name) -> str:
    """A channel name we know, or "stable" — never a typo from a form."""
    name = str(name or "").strip().lower()
    return name if name in UPDATE_CHANNELS else "stable"


def admin_url(host: str, port: int, path: str = "") -> str:
    """URL for the admin UI; plain http URLs omit the default port."""
    origin = f"http://{host}" if port == 80 else f"http://{host}:{port}"
    return origin + path


FIRST_USER_PORT = 1337


def assign_ports(users: list[dict], reserved=frozenset()) -> None:
    """Give every user a unique push port, in place.

    Ports the user never sees still have to be valid and unique — load()
    rejects duplicates, and a config it rejects would restart-loop the
    device. Existing sound ports are kept; blanks, duplicates and
    privileged ports are reassigned from FIRST_USER_PORT upwards.
    """
    taken = set(reserved)
    for user in users:
        port = user.get("port")
        if isinstance(port, int) and 1024 <= port <= 65535 and port not in taken:
            taken.add(port)
        else:
            user["port"] = None
    candidate = FIRST_USER_PORT
    for user in users:
        if user["port"] is None:
            while candidate in taken:
                candidate += 1
            user["port"] = candidate
            taken.add(candidate)


def write_atomic(raw: dict, path: str | Path) -> "Config":
    """Validate, then replace the config file in one step.

    Both the settings page and the setup wizard write config.json; the
    validate-before-replace order is the only thing standing between a
    bad edit and a device that restart-loops forever (the unit sets
    StartLimitIntervalSec=0), so it lives in one place.
    """
    path = Path(path)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")
    try:
        config = load(tmp)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    return config


# Browsers still report tzdata's old names — Chromium says Asia/Calcutta
# for Asia/Kolkata — and a system whose tzdata was built without the
# backward-compatibility links does not know them. This covers the ones
# that actually turn up; anything else falls through to the picker rather
# than becoming a dead end.
TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Asia/Chongqing": "Asia/Shanghai",
    "Asia/Istanbul": "Europe/Istanbul",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Godthab": "America/Nuuk",
    "Europe/Kiev": "Europe/Kyiv",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Truk": "Pacific/Chuuk",
    "Australia/Canberra": "Australia/Sydney",
}


def canonical_timezone(name: str) -> str:
    """The name this system actually knows, or "" if it knows nothing like it."""
    name = (name or "").strip()
    if not name:
        return ""
    if valid_timezone(name):
        return name
    alias = TIMEZONE_ALIASES.get(name)
    return alias if alias and valid_timezone(alias) else ""


def valid_timezone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False


def available_timezones() -> list[str]:
    """Every zone this system knows, sorted. Empty if tzdata is missing."""
    try:
        return sorted(zoneinfo.available_timezones())
    except Exception:  # noqa: BLE001
        return []


def apply_timezone(name: str) -> bool:
    """Point the clock at a place. False if the name is unusable.

    Two halves. This process's own zone changes immediately, which is what
    every strftime and localtime in the app reads — the footer clock, the
    forecast arrival time, the update-check time. Then the system is asked
    to adopt it too, so journald and everything else on the device agree;
    that half needs the polkit permission the image grants, and is best
    effort everywhere else.
    """
    if not name:
        return True
    canonical = canonical_timezone(name)
    if not canonical:
        log.warning("Ignoring unknown time zone %r", name)
        return False
    name = canonical
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):      # Unix only; a dev box on Windows skips it
        time.tzset()
    _set_system_timezone(name)
    return True


def _set_system_timezone(name: str) -> None:
    try:
        proc = subprocess.run(["timedatectl", "set-timezone", name],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            log.info("Could not set the system time zone: %s",
                     (proc.stdout + proc.stderr).strip())
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.info("Could not set the system time zone: %s", exc)


READABLE_ALPHABET = "abcdefghjkmnpqrstuvwxyzACDEFGHJKMNPQRSTUVWXYZ23456789"
SIMPLE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def simple_secret(length: int = 6) -> str:
    """Short, easily-typed secret for the on-device admin login."""
    import secrets
    return "".join(secrets.choice(SIMPLE_ALPHABET) for _ in range(length))


def readable_secret(length: int = 10) -> str:
    """Random secret without lookalike characters (no I/l/1/O/0) — these
    get read off a screen and typed on a phone."""
    import secrets
    return "".join(secrets.choice(READABLE_ALPHABET) for _ in range(length))


def create_default(path: str | Path) -> None:
    """Write a starter config so a fresh install boots straight into the
    on-screen setup flow (QR code -> web settings) with secure secrets."""
    import secrets
    starter = {
        "users": [
            {"name": "Person A", "port": 1337, "api_secret": secrets.token_hex(12)},
            {"name": "Person B", "port": 1338, "api_secret": secrets.token_hex(12)},
        ],
        "display": {},
        "database": "glucocube.db",
        "admin": {"port": 80, "password": simple_secret()},
    }
    Path(path).write_text(json.dumps(starter, indent=2) + "\n")


def merged_thresholds(display: DisplayConfig, user: UserConfig) -> dict:
    """Global display thresholds with this person's overrides applied."""
    merged = {
        "low": display.low, "high": display.high,
        "urgent_low": display.urgent_low, "urgent_high": display.urgent_high,
    }
    for key, value in (user.thresholds or {}).items():
        if key in merged and value:
            merged[key] = float(value)
    return merged


def load(path: str | Path) -> Config:
    path = Path(path)
    raw = json.loads(path.read_text())

    users = [UserConfig(**u) for u in raw.get("users", [])]
    if not users:
        raise ValueError(f"{path}: at least one user must be configured")
    ports = [u.port for u in users]
    if len(set(ports)) != len(ports):
        raise ValueError(f"{path}: each user needs a unique port")

    display = DisplayConfig(**raw.get("display", {}))

    database = raw.get("database", "glucocube.db")
    if not Path(database).is_absolute():
        database = str(path.parent / database)

    admin = raw.get("admin", {})
    admin_password = admin.get("password", "")
    updates = raw.get("updates", {})
    gc_raw = raw.get("glucocore") or {}
    glucocore = None
    if gc_raw.get("device_token"):
        glucocore = GlucoCoreConfig(
            device_id=str(gc_raw.get("device_id") or ""),
            device_token=str(gc_raw.get("device_token") or ""),
            hardware_id=str(gc_raw.get("hardware_id") or ""),
            name=str(gc_raw.get("name") or ""),
        )
    return Config(
        users=users,
        display=display,
        database=database,
        admin_port=int(admin.get("port", 80)),
        admin_password=admin_password,
        # A password and "no password on purpose" cannot both be true;
        # the password wins, so a stale flag left in the file is inert.
        admin_password_off=bool(admin.get("password_off")) and not admin_password,
        update_channel=normalize_channel(updates.get("channel")),
        glucocore=glucocore,
    )
