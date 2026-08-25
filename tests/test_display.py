"""display.py — the physical screen.

Rendering runs against SDL's dummy video driver, which is what the
ready-made image uses too (``GLUCOCUBE_DISPLAY=fbdev`` forces it, with the
frames copied to /dev/fb0 afterwards). That makes a real frame renderable
in CI: the tests below draw the dashboard, the setup screen and the QR
overlay and check that a full-size image comes out of each.
"""

import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ["GLUCOCUBE_TOUCH"] = "off"

pygame = pytest.importorskip("pygame")

from glucocube.config import Config, DisplayConfig, UserConfig  # noqa: E402
from glucocube.display import (  # noqa: E402
    DARK,
    LIGHT,
    Display,
    age_compact,
    source_label,
)

MINUTE = 60 * 1000


# ------------------------------------------------------------ age_compact ----

@pytest.mark.parametrize("minutes, expected", [
    (0, "NOW"), (0.5, "NOW"), (1, "1M"), (4, "4M"), (59, "59M"),
    (60, "1H00M"), (67, "1H07M"), (23 * 60 + 59, "23H59M"),
    (24 * 60, "1D"), (72 * 60, "3D"),
])
def test_ages_are_shown_compactly(minutes, expected):
    now = 1_700_000_000_000
    assert age_compact(now, int(now - minutes * MINUTE)) == expected


def test_no_timestamp_shows_as_nothing():
    assert age_compact(1_700_000_000_000, None) == "--"


# ---------------------------------------------------------- source_label ----

@pytest.mark.parametrize("source, expected", [
    (None, "TRIO"),
    ({}, "TRIO"),
    ({"type": "tidepool"}, "TWIIST"),
    ({"type": "nightscout"}, "NS"),
    ({"type": "something-new"}, "TRIO"),
])
def test_each_source_gets_its_badge(source, expected):
    assert source_label(UserConfig(name="Ada", port=1337,
                                   source=source)) == expected


# ------------------------------------------------------------- rendering ----

@pytest.fixture
def display(store, monkeypatch):
    monkeypatch.setenv("GLUCOCUBE_TOUCH", "off")
    monkeypatch.delenv("GLUCOCUBE_DISPLAY", raising=False)
    config = Config(
        users=[UserConfig(name="Ada", port=1337, api_secret="a"),
               UserConfig(name="Bo", port=1338, api_secret="b")],
        display=DisplayConfig(fullscreen=False, width=800, height=480),
        admin_port=8080, admin_password="pw1234")
    display = Display(config, store, windowed=True)
    yield display
    pygame.quit()


def seed(store, name="Ada", *, now_ms=None, sgv=120):
    now_ms = now_ms or int(time.time() * 1000)
    store.add_entries(name, [
        {"sgv": sgv + i, "date": now_ms - (36 - i) * 5 * MINUTE,
         "direction": "Flat"} for i in range(36)])
    store.add_treatments(name, [
        {"_id": "c", "eventType": "Carb Correction", "carbs": 30,
         "created_at": now_ms - 45 * MINUTE},
        {"_id": "b", "eventType": "Bolus", "insulin": 2.5,
         "created_at": now_ms - 20 * MINUTE}])
    store.add_devicestatus(name, [
        {"created_at": now_ms - 2 * MINUTE,
         "openaps": {"iob": {"iob": 1.4}, "suggested": {"COB": 18}}}])


def test_a_frame_can_be_drawn_with_no_data_at_all(display):
    """A fresh device draws the setup screen rather than crashing."""
    display.draw()
    assert display.screen.get_size() == (800, 480)


def test_a_dashboard_frame_is_drawn_for_both_people(display, store):
    seed(store, "Ada")
    seed(store, "Bo", sgv=200)
    display.draw()
    surface = display.screen
    assert surface.get_size() == (800, 480)
    # Something was actually painted: more than one distinct colour.
    colours = {surface.get_at((x, y))[:3]
               for x in range(0, 800, 40) for y in range(0, 480, 40)}
    assert len(colours) > 3


def test_a_screenshot_is_written_to_disk(display, store, tmp_path):
    seed(store, "Ada")
    path = tmp_path / "screen.png"
    display.screenshot(str(path))
    assert path.exists()
    image = pygame.image.load(str(path))
    assert image.get_size() == (800, 480)


def test_the_saved_snapshot_is_what_the_screen_endpoint_serves(display, store,
                                                               monkeypatch,
                                                               tmp_path):
    from glucocube import display as display_mod

    seed(store, "Ada")
    target = tmp_path / "glucocube-screen.png"
    monkeypatch.setattr(display_mod, "SCREEN_PNG", str(target))
    display.draw()
    display.save_snapshot()
    assert target.exists()
    assert not list(tmp_path.glob("*.tmp.png"))


def test_the_setup_screen_is_shown_until_data_arrives(display, store):
    snaps = [store.snapshot(user.name) for user in display.config.users]
    assert display.is_unconfigured(snaps) is True
    display.draw_setup_screen()


def test_a_person_with_a_pull_source_counts_as_configured(display, store):
    display.config.users[0].source = {"type": "tidepool",
                                      "email": "c@example.invalid"}
    snaps = [store.snapshot(user.name) for user in display.config.users]
    assert display.is_unconfigured(snaps) is False


def test_a_reading_counts_as_configured(display, store):
    seed(store, "Ada")
    snaps = [store.snapshot(user.name) for user in display.config.users]
    assert display.is_unconfigured(snaps) is False


def test_the_qr_overlay_draws_and_times_itself_out(display, store):
    seed(store, "Ada")
    display.toggle_qr()
    assert display.qr_open() is True
    display.draw()
    display._qr_open_until = time.monotonic() - 1
    assert display.qr_open() is False


def test_the_theme_toggle_flips_and_is_remembered(display, store):
    assert display.pal.name == "dark"
    display.toggle_theme()
    assert display.pal.name == "light"
    assert store.get_params("__display")["theme"] == "light"


def test_one_tap_does_not_flip_the_theme_twice(display):
    """A touchscreen can deliver a tap as both a finger and a mouse event."""
    display.toggle_theme()
    display.toggle_theme()
    assert display.pal.name == "light"


def test_a_theme_set_from_the_web_ui_is_adopted(display, store):
    store.set_params("__display", {"theme": "light"})
    display._sync_theme()
    assert display.pal.name == "light"


def test_both_themes_render(display, store):
    seed(store, "Ada")
    for palette in (DARK, LIGHT):
        display.pal = palette
        display.draw()


# ------------------------------------------------------- glucose colours ----

THRESHOLDS = {"low": 70, "high": 180, "urgent_low": 55, "urgent_high": 250}


@pytest.mark.parametrize("sgv, expected", [
    (120, "in_range"), (70, "in_range"), (180, "in_range"),
    (69, "low"), (56, "low"),
    (181, "high"), (249, "high"),
    (55, "urgent"), (40, "urgent"), (250, "urgent"), (300, "urgent"),
])
def test_a_reading_is_coloured_by_where_it_sits(display, sgv, expected):
    colour = display.glucose_color(sgv, False, THRESHOLDS)
    assert colour == getattr(display.pal, expected)


def test_a_stale_reading_is_greyed_out(display):
    """The number is still there, but it is no longer telling you anything."""
    assert display.glucose_color(40, True, THRESHOLDS) == display.pal.stale


def test_no_reading_at_all_is_greyed_out(display):
    assert display.glucose_color(None, False, THRESHOLDS) == display.pal.stale


# ------------------------------------------------------------- the footer ----

def test_the_footer_controls_sit_over_the_footer_and_are_big_enough_to_hit(
        display):
    """They are deliberately taller than the footer — a 44px tap target."""
    footer = display._footer_rect()
    qr_rect, toggle_rect = display._controls_for(footer)
    screen = display.screen.get_rect()
    assert screen.contains(footer)
    for rect in (qr_rect, toggle_rect):
        assert rect.left >= 0 and rect.right <= screen.right
        assert rect.colliderect(footer)
        assert rect.height >= 44 and rect.width >= 100


def test_the_two_footer_controls_do_not_overlap(display):
    """They are 48px tap targets a few pixels apart on a 7-inch panel."""
    qr_rect, toggle_rect = display._controls_for(display._footer_rect())
    assert not qr_rect.colliderect(toggle_rect)


def test_tapping_the_theme_control_flips_the_theme(display):
    _qr_rect, toggle_rect = display._controls_for(display._footer_rect())
    display._handle_tap(toggle_rect.center)
    assert display.pal.name == "light"


def test_tapping_the_settings_control_opens_the_qr_code(display):
    qr_rect, _toggle = display._controls_for(display._footer_rect())
    display._handle_tap(qr_rect.center)
    assert display.qr_open() is True


def test_tapping_the_chart_does_nothing(display):
    display._handle_tap((400, 200))
    assert display.pal.name == "dark"
    assert display.qr_open() is False


def test_a_tap_from_the_panel_is_queued_for_the_draw_loop(display):
    """The reader runs on its own thread; drawing happens on the main one."""
    display._on_touch(10, 20)
    assert display._taps.get_nowait() == (10, 20)


def test_the_settings_url_carries_the_login(display):
    url = display._settings_url()
    assert url is None or "key=pw1234" in url
