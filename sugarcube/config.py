"""Configuration loading for SugarCube."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Where the display loop drops live screenshots for the /screen.png endpoint.
SCREEN_PNG = os.path.join(tempfile.gettempdir(), "sugarcube-screen.png")


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
    low: float = 70
    high: float = 180
    urgent_low: float = 55
    urgent_high: float = 250
    stale_minutes: float = 12


@dataclass
class Config:
    users: list[UserConfig] = field(default_factory=list)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    database: str = "sugarcube.db"
    admin_port: int = 80            # 0 disables the web admin
    admin_password: str = ""        # empty disables Basic auth


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
        "database": "sugarcube.db",
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

    database = raw.get("database", "sugarcube.db")
    if not Path(database).is_absolute():
        database = str(path.parent / database)

    admin = raw.get("admin", {})
    return Config(
        users=users,
        display=display,
        database=database,
        admin_port=int(admin.get("port", 80)),
        admin_password=admin.get("password", ""),
    )
