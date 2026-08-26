"""config.py — the file that decides whether the device boots at all.

A config the loader accepts but the app cannot run is the one failure the
device cannot recover from by itself (the unit restarts forever), so the
validation paths get more attention here than the happy path does.
"""

import json
import os

import pytest

from glucocube import config as config_mod
from glucocube.config import (
    Config,
    DisplayConfig,
    UserConfig,
    admin_url,
    assign_ports,
    canonical_timezone,
    create_default,
    load,
    merged_thresholds,
    normalize_channel,
    readable_secret,
    simple_secret,
    valid_timezone,
    write_atomic,
)


# ----------------------------------------------------------------- load ----

def test_load_reads_users_display_and_admin(config_path):
    config = load(config_path)
    assert [u.name for u in config.users] == ["Ada", "Bo"]
    assert config.users[0].port == 1337
    assert config.users[0].api_secret == "ada-secret"
    assert config.display.low == 70
    assert config.admin_port == 8080
    assert config.admin_password == "letmein"


def test_load_applies_defaults_for_absent_sections(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "Solo", "port": 1337}]}))
    config = load(path)
    assert config.display == DisplayConfig()
    assert config.admin_port == 80
    assert config.admin_password == ""
    assert config.admin_password_off is False
    assert config.update_channel == "stable"
    assert config.users[0].api_secret == ""
    assert config.users[0].source is None


# An empty password disables Basic auth on its own; password_off only says
# that the emptiness is deliberate, so the settings hub can stop offering
# to fix it.

@pytest.mark.parametrize("admin, password, off", [
    ({"password": "", "password_off": True}, "", True),
    ({"password": ""}, "", False),
    ({}, "", False),
    # A password wins: a flag left behind by an earlier choice is inert
    # rather than a device that quietly stopped asking for one.
    ({"password": "letmein", "password_off": True}, "letmein", False),
])
def test_load_reads_a_deliberate_lack_of_password(tmp_path, admin, password,
                                                  off):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "Solo", "port": 1337}],
                                "admin": admin}))
    config = load(path)
    assert config.admin_password == password
    assert config.admin_password_off is off


def test_load_resolves_a_relative_database_next_to_the_config(tmp_path):
    """The service's working directory is not the config's directory."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "A", "port": 1337}],
                                "database": "glucocube.db"}))
    assert load(path).database == str(tmp_path / "glucocube.db")


def test_load_keeps_an_absolute_database_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "A", "port": 1337}],
                                "database": "/var/lib/glucocube/db.sqlite"}))
    assert load(path).database == "/var/lib/glucocube/db.sqlite"


def test_load_rejects_a_config_with_no_users(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": []}))
    with pytest.raises(ValueError, match="at least one user"):
        load(path)


def test_load_rejects_duplicate_ports(tmp_path):
    """Two servers on one port: the second bind fails and the app dies."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "A", "port": 1337},
                                          {"name": "B", "port": 1337}]}))
    with pytest.raises(ValueError, match="unique port"):
        load(path)


def test_load_rejects_an_unknown_user_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "A", "port": 1337,
                                           "nickname": "Al"}]}))
    with pytest.raises(TypeError):
        load(path)


def test_load_normalizes_a_junk_update_channel(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"name": "A", "port": 1337}],
                                "updates": {"channel": "BETA-ish"}}))
    assert load(path).update_channel == "stable"


def test_the_shipped_example_config_loads():
    """config.example.json is what people copy; it has to be valid."""
    example = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config.example.json")
    config = load(example)
    assert len(config.users) == 2
    assert config.display.units == "mg/dL"


# ------------------------------------------------------------- channels ----

@pytest.mark.parametrize("given, expected", [
    ("stable", "stable"),
    ("beta", "beta"),
    ("  BETA  ", "beta"),
    ("Stable", "stable"),
    ("nightly", "stable"),
    ("", "stable"),
    (None, "stable"),
    (5, "stable"),
])
def test_normalize_channel(given, expected):
    assert normalize_channel(given) == expected


# ------------------------------------------------------------ admin_url ----

@pytest.mark.parametrize("host, port, path, expected", [
    ("glucocube.local", 80, "", "http://glucocube.local"),
    ("glucocube.local", 80, "/setup", "http://glucocube.local/setup"),
    ("10.0.0.5", 8080, "/settings", "http://10.0.0.5:8080/settings"),
])
def test_admin_url_omits_the_default_port(host, port, path, expected):
    assert admin_url(host, port, path) == expected


# --------------------------------------------------------- assign_ports ----

def test_assign_ports_keeps_ports_that_are_already_sound():
    users = [{"port": 1337}, {"port": 1338}]
    assign_ports(users)
    assert [u["port"] for u in users] == [1337, 1338]


def test_assign_ports_fills_blanks_from_the_first_user_port():
    users = [{"name": "A"}, {"port": None}]
    assign_ports(users)
    assert [u["port"] for u in users] == [1337, 1338]


def test_assign_ports_breaks_a_duplicate():
    """The second claim on a port loses it — load() would reject the pair."""
    users = [{"port": 1337}, {"port": 1337}]
    assign_ports(users)
    assert users[0]["port"] == 1337
    assert users[1]["port"] != 1337
    assert len({u["port"] for u in users}) == 2


@pytest.mark.parametrize("bad", [80, 443, 1023, 0, -1, 70000, "1337", 13.37])
def test_assign_ports_replaces_a_port_the_app_could_not_bind(bad):
    users = [{"port": bad}]
    assign_ports(users)
    assert isinstance(users[0]["port"], int)
    assert 1024 <= users[0]["port"] <= 65535


def test_assign_ports_avoids_reserved_ports():
    """The admin port is reserved; a person handed it would kill the web UI."""
    users = [{"port": None}, {"port": None}]
    assign_ports(users, reserved={1337, 1338})
    assert [u["port"] for u in users] == [1339, 1340]


def test_assign_ports_leaves_every_user_unique():
    users = [{"port": None} for _ in range(12)]
    assign_ports(users)
    ports = [u["port"] for u in users]
    assert len(set(ports)) == len(ports)


# --------------------------------------------------------- write_atomic ----

def test_write_atomic_writes_and_returns_the_loaded_config(tmp_path):
    path = tmp_path / "config.json"
    raw = {"users": [{"name": "A", "port": 1337}]}
    config = write_atomic(raw, path)
    assert isinstance(config, Config)
    assert json.loads(path.read_text())["users"][0]["name"] == "A"


def test_write_atomic_leaves_the_old_config_in_place_when_rejected(tmp_path):
    """The whole point: a bad edit must not become a restart loop."""
    path = tmp_path / "config.json"
    write_atomic({"users": [{"name": "Good", "port": 1337}]}, path)
    before = path.read_text()

    with pytest.raises(ValueError):
        write_atomic({"users": []}, path)

    assert path.read_text() == before
    assert not (tmp_path / "config.json.tmp").exists()


def test_write_atomic_output_is_reloadable(tmp_path):
    path = tmp_path / "config.json"
    write_atomic({"users": [{"name": "A", "port": 1337}],
                  "admin": {"port": 8080, "password": "pw"}}, path)
    assert load(path).admin_password == "pw"


# ------------------------------------------------------------ timezones ----

def test_valid_timezone_accepts_a_real_zone():
    assert valid_timezone("Europe/London")


@pytest.mark.parametrize("junk", ["", "Mars/Olympus", "Europe/Londonn", "  "])
def test_valid_timezone_rejects_nonsense(junk):
    assert not valid_timezone(junk)


def test_canonical_timezone_passes_a_known_zone_through():
    assert canonical_timezone("America/New_York") == "America/New_York"


def test_canonical_timezone_maps_a_browser_alias():
    """Chromium reports Asia/Calcutta; tzdata may only know Asia/Kolkata."""
    assert canonical_timezone("Asia/Calcutta") in ("Asia/Calcutta",
                                                   "Asia/Kolkata")
    assert valid_timezone(canonical_timezone("Asia/Calcutta"))


@pytest.mark.parametrize("alias", sorted(config_mod.TIMEZONE_ALIASES))
def test_every_alias_resolves_to_something_this_system_knows(alias):
    assert canonical_timezone(alias) != ""


def test_canonical_timezone_returns_empty_for_the_unrecognisable():
    assert canonical_timezone("Not/AZone") == ""
    assert canonical_timezone("") == ""


def test_available_timezones_is_sorted_and_populated():
    zones = config_mod.available_timezones()
    assert zones == sorted(zones)
    assert "UTC" in zones


def test_apply_timezone_sets_the_process_zone(monkeypatch):
    monkeypatch.setattr(config_mod, "_set_system_timezone", lambda name: None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    assert config_mod.apply_timezone("Europe/London") is True
    assert os.environ["TZ"] == "Europe/London"


def test_apply_timezone_ignores_an_unknown_zone(monkeypatch):
    calls = []
    monkeypatch.setattr(config_mod, "_set_system_timezone", calls.append)
    assert config_mod.apply_timezone("Mars/Olympus") is False
    assert calls == []


def test_apply_timezone_of_nothing_is_a_no_op(monkeypatch):
    calls = []
    monkeypatch.setattr(config_mod, "_set_system_timezone", calls.append)
    assert config_mod.apply_timezone("") is True
    assert calls == []


# ----------------------------------------------------------- thresholds ----

def test_merged_thresholds_uses_the_display_defaults():
    display = DisplayConfig(low=70, high=180, urgent_low=55, urgent_high=250)
    user = UserConfig(name="A", port=1337)
    assert merged_thresholds(display, user) == {
        "low": 70, "high": 180, "urgent_low": 55, "urgent_high": 250}


def test_merged_thresholds_applies_a_persons_overrides():
    display = DisplayConfig()
    user = UserConfig(name="A", port=1337, thresholds={"low": 80, "high": 160})
    merged = merged_thresholds(display, user)
    assert (merged["low"], merged["high"]) == (80.0, 160.0)
    assert merged["urgent_low"] == display.urgent_low


def test_merged_thresholds_ignores_blanks_and_unknown_keys():
    display = DisplayConfig()
    user = UserConfig(name="A", port=1337,
                      thresholds={"low": "", "high": None, "nonsense": 1})
    assert merged_thresholds(display, user) == {
        "low": display.low, "high": display.high,
        "urgent_low": display.urgent_low, "urgent_high": display.urgent_high}


# -------------------------------------------------------------- secrets ----

def test_readable_secret_avoids_lookalike_characters():
    """These are read off a screen and typed on a phone."""
    for _ in range(50):
        secret = readable_secret(16)
        assert len(secret) == 16
        assert not set(secret) & set("Il1O0")
        assert set(secret) <= set(config_mod.READABLE_ALPHABET)


def test_simple_secret_is_lowercase_and_unambiguous():
    for _ in range(50):
        secret = simple_secret(6)
        assert len(secret) == 6
        # Not islower(): that is False for a string with no cased letters
        # at all, and eight of this alphabet's characters are digits — so
        # roughly one run in seventy drew "234567" and failed on it.
        assert secret == secret.lower()
        assert set(secret) <= set(config_mod.SIMPLE_ALPHABET)


def test_secrets_are_not_all_the_same():
    assert len({readable_secret(10) for _ in range(20)}) > 1


def test_create_default_writes_a_config_the_loader_accepts(tmp_path):
    path = tmp_path / "config.json"
    create_default(path)
    config = load(path)
    assert len(config.users) == 2
    assert config.users[0].port != config.users[1].port
    assert config.admin_password
    # A shipped-secret device would be open to everyone on the network.
    secrets = {u.api_secret for u in config.users}
    assert len(secrets) == 2
    assert all(len(s) >= 16 for s in secrets)
