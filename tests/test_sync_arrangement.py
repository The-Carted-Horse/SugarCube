"""What a config push now carries, and what it still refuses to.

sync.py's whitelist is the only thing standing between GlucoCore and a
device that will not boot: config.load builds DisplayConfig with **display,
so a key the dataclass does not have is a TypeError, and the unit sets
StartLimitIntervalSec=0. These are the tests that keep that true as the
contract grows.
"""

import json

import pytest

from glucocube import sync
from glucocube.config import DisplayConfig, UserConfig


def write(path, **display):
    path.write_text(json.dumps({
        "users": [{"name": "Grace", "port": 1337, "api_secret": "s",
                   "source": {"type": "glucocore", "patient_id": "pat-1"}}],
        "display": display,
        "admin": {"port": 8080, "password": "letmein"},
    }))
    return path


def push(path, display=None, per_patient=None, version=9):
    return sync.apply_remote_config(
        path,
        {"patientIds": ["pat-1"],
         "display": display or {},
         "perPatient": per_patient or {"pat-1": {"label": "Grace"}}},
        version)


@pytest.fixture
def config_file(tmp_path):
    return write(tmp_path / "config.json")


# ------------------------------------------------------- the boot invariant --

def test_nothing_a_push_applies_can_stop_the_device_booting():
    fields = set(DisplayConfig().__dataclass_fields__)
    assert set(sync.DISPLAY_KEYS) <= fields


def test_nothing_a_push_puts_on_a_person_can_either():
    """Same rule, for the user dicts: load builds those with **u."""
    fields = set(UserConfig(name="x", port=1).__dataclass_fields__)
    sample = sync.patient_extras(
        {"perPatient": {"p": {"wallpaper": "none", "rotate_seconds": 20}}}, "p")
    assert set(sample) <= fields


def test_a_key_this_firmware_does_not_know_never_reaches_the_file(config_file):
    """A newer GlucoCore is not a device that refuses to start."""
    config = push(config_file, {"aurora_mode": True, "low": 75})
    assert config.display.low == 75
    assert "aurora_mode" not in json.loads(config_file.read_text())["display"]


def test_a_person_key_this_firmware_does_not_know_is_dropped_too(config_file):
    push(config_file, per_patient={"pat-1": {"label": "Grace",
                                             "favourite_colour": "green"}})
    user = json.loads(config_file.read_text())["users"][0]
    assert "favourite_colour" not in user


# ------------------------------------------------------------ arrangement ---

def test_the_layout_a_push_names_is_what_the_screen_draws(config_file):
    assert push(config_file, {"layout": "rotate"}).display.layout == "rotate"


def test_a_layout_this_display_cannot_draw_falls_back_to_the_classic_one(
        config_file):
    assert push(config_file, {"layout": "hologram"}).display.layout == "split"


def test_the_split_settings_arrive(config_file):
    config = push(config_file, {"split_direction": "rows", "split_max": 2})
    assert config.display.split_direction == "rows"
    assert config.display.split_max == 2


def test_a_rotation_interval_is_applied_now_that_something_rotates(config_file):
    assert push(config_file, {"rotate_seconds": 20}).display.rotate_seconds == 20


def test_the_clock_format_is_applied(config_file):
    assert push(config_file, {"time_format": 12}).display.time_format == 12


# ------------------------------------------------------------ backgrounds ---

def test_a_background_for_the_display_and_for_one_person(config_file):
    config = push(config_file,
                  {"wallpaper": "bundled:reeds"},
                  {"pat-1": {"label": "Grace", "wallpaper": "a" * 32,
                             "rotate_seconds": 20}})
    assert config.display.wallpaper == "bundled:reeds"
    assert config.users[0].wallpaper == "a" * 32
    assert config.users[0].rotate_seconds == 20


def test_none_survives_the_trip(config_file):
    """It is a value, not an absence — see wallpaper.resolve."""
    config = push(config_file, per_patient={"pat-1": {"label": "Grace",
                                                      "wallpaper": "none"}})
    assert config.users[0].wallpaper == "none"


def test_the_dim_settings_arrive(config_file):
    config = push(config_file, {"wallpaper_dim": 45, "night_dim_boost": 30})
    assert config.display.wallpaper_dim == 45
    assert config.display.night_dim_boost == 30


# --------------------------------------------------- absence that means it --

def test_taking_the_background_off_in_the_app_takes_it_off_the_wall(tmp_path):
    """The two keys where a missing key is the value.

    GlucoCore deletes a key rather than sending an empty one, so for "no
    art" and "everyone on screen" the leave-it-alone rule would mean a
    picture nobody can remove.
    """
    path = write(tmp_path / "config.json", wallpaper="bundled:reeds",
                 split_max=2)
    config = push(path, {"low": 75})
    assert config.display.wallpaper == ""
    assert config.display.split_max is None


def test_every_other_setting_keeps_the_leave_it_alone_rule(tmp_path):
    """A push that omits a brightness is not a push about brightness."""
    path = write(tmp_path / "config.json", brightness=80, timezone="Europe/London")
    config = push(path, {"low": 75})
    assert config.display.brightness == 80
    assert config.display.timezone == "Europe/London"


# ------------------------------------------------------------- reporting ----

def test_what_is_still_ignored_is_said_out_loud(config_file, caplog):
    with caplog.at_level("INFO", logger="glucocube.sync"):
        push(config_file, {"alert_urgent_low": True, "quiet_from_hour": 22,
                           "layout": "rotate"})
    listed = [line.split(": ")[-1] for line in caplog.text.splitlines()
              if "does not apply" in line]
    ignored = set(listed[0].split(", "))
    # This is not an alarm device, so the alert keys stay unapplied — but
    # the arrangement keys no longer do.
    assert "alert_urgent_low" in ignored and "quiet_from_hour" in ignored
    assert "layout" not in ignored
