"""The ambient screen: one person, full bleed, over a background.

Rendered for real against SDL's dummy driver, like the classic panel is,
and asserted by comparing frames rather than against a golden image — a
test that says "the ring changed when the reading went urgent" keeps
meaning something when the type moves a pixel.

The safety note in the handoff is what most of this is about. Ambient mode
makes the panel prettier and it must not make a stale or urgent reading
quieter, so those two cases get more attention here than the pretty one.
"""

import time

import pytest

pygame = pytest.importorskip("pygame")

import os  # noqa: E402

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["GLUCOCUBE_TOUCH"] = "off"

from glucocube import weather  # noqa: E402
from glucocube.config import (  # noqa: E402
    Config, DisplayConfig, UserConfig, WeatherConfig,
)
from glucocube.display import Display  # noqa: E402

MINUTE = 60 * 1000


def seed(store, name, *, sgv=120, minutes_ago=2, direction="Flat"):
    now = int(time.time() * 1000)
    store.add_entries(name, [
        {"sgv": sgv - 6 + i, "date": now - (36 - i) * 5 * MINUTE,
         "direction": direction} for i in range(35)])
    store.add_entries(name, [
        {"sgv": sgv, "date": now - minutes_ago * MINUTE,
         "direction": direction}])
    store.add_devicestatus(name, [
        {"created_at": now - 2 * MINUTE,
         "openaps": {"suggested": {"IOB": 1.4, "COB": 12}}}])


def build(store, *users, **display):
    settings = dict(fullscreen=False, width=800, height=480, layout="rotate")
    settings.update(display)
    config = Config(users=list(users), display=DisplayConfig(**settings),
                    admin_port=8080, admin_password="pw1234",
                    weather=WeatherConfig())
    return Display(config, store, windowed=True)


def frame(display):
    display.draw()
    return pygame.image.tostring(display.screen, "RGB")


@pytest.fixture
def ambient(store):
    seed(store, "Maya")
    display = build(store, UserConfig(name="Maya", port=1337, api_secret="a"))
    yield display
    pygame.quit()


# ---------------------------------------------------------------- it draws --

def test_a_full_frame_comes_out(ambient):
    ambient.draw()
    assert ambient.screen.get_size() == (800, 480)


def test_the_ambient_screen_is_not_the_classic_one(store):
    seed(store, "Maya")
    person = UserConfig(name="Maya", port=1337, api_secret="a")
    classic = build(store, person, layout="split")
    rotate = build(store, person, layout="rotate")
    assert frame(classic) != frame(rotate)
    pygame.quit()


def test_it_holds_up_on_a_panel_that_is_not_800_by_480(store):
    seed(store, "Maya")
    display = build(store, UserConfig(name="Maya", port=1337, api_secret="a"),
                    width=480, height=800)
    display.draw()
    assert display.screen.get_size() == (480, 800)
    pygame.quit()


# ------------------------------------------------------------ the art ------

def test_a_background_changes_what_is_behind_the_reading(store):
    seed(store, "Maya")
    person = UserConfig(name="Maya", port=1337, api_secret="a")
    bare = build(store, person)
    art = build(store, person, wallpaper="bundled:reeds")
    assert frame(bare) != frame(art)
    pygame.quit()


def test_a_background_that_is_not_there_is_a_flat_panel_not_a_crash(store):
    """The layout is designed to hold up with no art at all."""
    seed(store, "Maya")
    display = build(store, UserConfig(name="Maya", port=1337, api_secret="a"),
                    wallpaper="c" * 32)
    display.draw()
    assert display.screen.get_size() == (800, 480)
    pygame.quit()


def test_dimming_the_art_changes_the_frame(store):
    seed(store, "Maya")
    person = UserConfig(name="Maya", port=1337, api_secret="a",
                        wallpaper="bundled:tide")
    light = build(store, person, wallpaper_dim=10)
    heavy = build(store, person, wallpaper_dim=90)
    assert frame(light) != frame(heavy)
    pygame.quit()


def test_the_scrim_is_built_once_and_kept(ambient):
    """A per-pixel pass over the whole panel, every frame, otherwise."""
    first = ambient._ambient_dim((800, 480), 0.6)
    assert ambient._ambient_dim((800, 480), 0.6) is first
    assert ambient._ambient_dim((800, 480), 0.9) is not first


# --------------------------------------------------------- state and stale --

def test_an_urgent_reading_looks_different_from_a_calm_one(store):
    """The ring is the part of this design that is not decoration."""
    seed(store, "Calm", sgv=120)
    seed(store, "Urgent", sgv=280)
    calm = build(store, UserConfig(name="Calm", port=1337, api_secret="a"))
    urgent = build(store, UserConfig(name="Urgent", port=1338, api_secret="b"))
    assert frame(calm) != frame(urgent)
    pygame.quit()


def test_a_stale_reading_drops_the_arrow_and_the_delta(store):
    """Same rule as the classic panel: no claim about now without a reading."""
    seed(store, "Fresh", sgv=120, minutes_ago=2, direction="SingleUp")
    seed(store, "Old", sgv=120, minutes_ago=90, direction="SingleUp")
    fresh = build(store, UserConfig(name="Fresh", port=1337, api_secret="a"))
    old = build(store, UserConfig(name="Old", port=1338, api_secret="b"))
    assert frame(fresh) != frame(old)
    pygame.quit()


def test_a_display_with_no_readings_at_all_still_draws(store):
    display = build(store, UserConfig(name="Nobody", port=1337,
                                      api_secret="a", source={"type": "push"}))
    display.draw()
    assert display.screen.get_size() == (800, 480)
    pygame.quit()


# ------------------------------------------------------------- rotation ----

def test_the_rotation_moves_on_when_the_interval_is_up(store):
    seed(store, "Maya", sgv=120)
    seed(store, "Theo", sgv=130)
    display = build(store,
                    UserConfig(name="Maya", port=1337, api_secret="a"),
                    UserConfig(name="Theo", port=1338, api_secret="b"),
                    rotate_seconds=15)
    display.draw()
    assert display._rot_index == 0
    display._rot_started -= 100
    display.draw()
    assert display._rot_index == 1
    pygame.quit()


def test_one_person_never_rotates(store):
    seed(store, "Maya")
    display = build(store, UserConfig(name="Maya", port=1337, api_secret="a"))
    display.draw()
    display._rot_started -= 100
    display.draw()
    assert display._rot_index == 0
    pygame.quit()


def test_an_urgent_reading_holds_the_screen(store):
    """The handoff's recommendation, and the safety note's whole point.

    A rotation that moves on from somebody in trouble after fifteen
    seconds is ambient mode making an urgent reading quieter than the
    classic panel does.
    """
    seed(store, "Calm", sgv=120)
    seed(store, "Urgent", sgv=290)
    display = build(store,
                    UserConfig(name="Calm", port=1337, api_secret="a"),
                    UserConfig(name="Urgent", port=1338, api_secret="b"))
    display.draw()
    assert display._rot_index == 1          # taken immediately
    for _ in range(3):
        display._rot_started -= 100
        display.draw()
        assert display._rot_index == 1      # and held
    pygame.quit()


def test_somebody_with_no_data_does_not_get_a_turn(store):
    """A rotation that stops on an empty panel looks like a broken display."""
    seed(store, "Maya")
    display = build(store,
                    UserConfig(name="Maya", port=1337, api_secret="a"),
                    UserConfig(name="Silent", port=1338, api_secret="b"))
    display.draw()
    display._rot_started -= 100
    display.draw()
    assert display._rot_index == 0
    pygame.quit()


def test_a_person_can_hold_the_screen_longer_than_the_others(store):
    seed(store, "Maya")
    seed(store, "Theo")
    display = build(store,
                    UserConfig(name="Maya", port=1337, api_secret="a",
                               rotate_seconds=60),
                    UserConfig(name="Theo", port=1338, api_secret="b"),
                    rotate_seconds=5)
    display.draw()
    display._rot_started = time.monotonic() - 10   # past 5, short of 60
    display.draw()
    assert display._rot_index == 0
    pygame.quit()


def test_the_loop_wakes_for_the_rotation_rather_than_a_second_late(store):
    seed(store, "Maya")
    seed(store, "Theo")
    display = build(store,
                    UserConfig(name="Maya", port=1337, api_secret="a"),
                    UserConfig(name="Theo", port=1338, api_secret="b"),
                    rotate_seconds=15)
    assert 0 < display._ambient_seconds_left() <= 15
    pygame.quit()


def test_a_classic_display_declares_no_rotation_wake(store):
    seed(store, "Maya")
    display = build(store, UserConfig(name="Maya", port=1337, api_secret="a"),
                    layout="split")
    assert display._ambient_seconds_left() == 0.0
    pygame.quit()


# ---------------------------------------------------------------- weather --

def test_the_weather_appears_once_the_device_knows_where_it_is(store):
    seed(store, "Maya")
    person = UserConfig(name="Maya", port=1337, api_secret="a")
    without = build(store, person)
    blank = frame(without)
    store.replace_params(weather.PARAMS_KEY, {
        "temp": 72.0, "code": 2, "high": 78, "low": 61,
        "fetched_at": int(time.time() * 1000)})
    with_weather = build(store, person)
    assert frame(with_weather) != blank
    pygame.quit()


def test_a_twelve_hour_clock_is_not_a_twenty_four_hour_one(store):
    seed(store, "Maya")
    person = UserConfig(name="Maya", port=1337, api_secret="a")
    twelve = build(store, person, time_format=12)
    twenty_four = build(store, person, time_format=24)
    assert frame(twelve) != frame(twenty_four)
    pygame.quit()


# ------------------------------------------------------------------ taps ---

def test_a_tap_brings_the_classic_controls_back(ambient):
    """Ambient mode has no chrome to aim at, and the theme toggle is in it."""
    ambient.draw()
    assert ambient._controls_until == 0.0
    ambient._handle_tap((400, 240))
    assert ambient._controls_until > time.monotonic()
    ambient.draw()
    # With the footer up, the toggle has a target again.
    assert ambient._toggle_rect.width > 0


def test_the_settings_mark_still_opens_the_qr(ambient):
    ambient.draw()
    ambient._handle_tap(ambient._qr_rect.center)
    assert ambient.qr_open()


def test_the_overlay_swallows_the_next_tap_wherever_it_lands(ambient):
    ambient.draw()
    ambient._handle_tap(ambient._qr_rect.center)
    assert ambient.qr_open()
    ambient._handle_tap((10, 10))
    assert not ambient.qr_open()
