"""backlight.py — dimming the panel overnight.

Two rules run through all of it. A display that cannot dim must still
show glucose, so every path here is written to give up quietly rather
than fail: no backlight, an unreadable one, a read-only one. And the
panel must never be driven to black, because a glucose display nobody can
read is worse than a bright one.

Nothing here touches the real /sys/class/backlight — the tests point the
module at a directory they built.
"""

import pytest

from glucocube import backlight
from glucocube.config import DisplayConfig


@pytest.fixture(autouse=True)
def forget_what_was_found():
    """The lookup is cached, and each test builds a different world."""
    backlight.reset()
    yield
    backlight.reset()


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """A backlight shaped like the 7" display's, at a path we can read."""
    def make(max_brightness=255, writable=True):
        device = tmp_path / "sys" / "10-0045"
        device.mkdir(parents=True)
        (device / "max_brightness").write_text(f"{max_brightness}\n")
        if writable:
            (device / "brightness").write_text("255\n")
        else:
            # A directory, not a read-only file: the tests run as root on
            # CI, and root writes through a mode bit without blinking.
            (device / "brightness").mkdir()
        monkeypatch.setattr(backlight, "SYSFS", str(tmp_path / "sys"))
        return device
    return make


def written(device) -> int:
    return int((device / "brightness").read_text().strip())


# ------------------------------------------------------- finding a panel ----

def test_a_machine_with_no_backlight_is_not_an_error(tmp_path, monkeypatch):
    """Every dev machine, and any Pi on an HDMI monitor."""
    monkeypatch.setattr(backlight, "SYSFS", str(tmp_path / "nothing-here"))
    assert backlight.device() is None
    assert backlight.set_percent(50) is False


def test_the_panel_is_looked_for_once(panel, monkeypatch):
    device = panel()
    assert backlight.device() == str(device)
    monkeypatch.setattr(backlight, "SYSFS", "/gone")
    assert backlight.device() == str(device)


# ----------------------------------------------------------- setting it ----

def test_a_percentage_is_scaled_to_what_the_panel_takes(panel):
    device = panel(max_brightness=255)
    assert backlight.set_percent(100) is True
    assert written(device) == 255
    assert backlight.set_percent(20) is True
    assert written(device) == 51


def test_a_panel_with_a_different_range_is_scaled_to_its_own(panel):
    device = panel(max_brightness=10)
    backlight.set_percent(50)
    assert written(device) == 5


def test_the_screen_cannot_be_turned_off_from_a_web_form(panel):
    """A glucose display nobody can read is worse than a bright one."""
    device = panel(max_brightness=255)
    backlight.set_percent(0)
    assert written(device) == round(255 * backlight.MIN_PERCENT / 100)


def test_a_value_over_the_top_is_the_top(panel):
    device = panel(max_brightness=255)
    backlight.set_percent(400)
    assert written(device) == 255


def test_writing_the_same_value_again_does_not_touch_the_panel(panel):
    """This is called once a second; a write per frame is a needless wake."""
    device = panel()
    backlight.set_percent(40)
    (device / "brightness").write_text("changed by hand\n")
    assert backlight.set_percent(40) is True
    assert (device / "brightness").read_text() == "changed by hand\n"


def test_a_panel_that_cannot_be_written_gives_up_quietly(panel):
    panel(writable=False)
    assert backlight.set_percent(50) is False


def test_a_panel_with_no_maximum_is_left_alone(panel):
    device = panel()
    (device / "max_brightness").write_text("not a number\n")
    assert backlight.set_percent(50) is False


# ------------------------------------------------------- the night window ----

@pytest.mark.parametrize("hour, night", [
    (22, True), (23, True), (0, True), (6, True), (7, False),
    (12, False), (21, False),
])
def test_a_window_that_crosses_midnight(hour, night):
    assert backlight.is_night(hour, 22, 7) is night


@pytest.mark.parametrize("hour, night", [
    (0, False), (13, True), (14, True), (15, False),
])
def test_a_window_inside_one_day(hour, night):
    assert backlight.is_night(hour, 13, 15) is night


def test_equal_hours_mean_never():
    """The same convention the quiet-hours pair uses."""
    assert backlight.is_night(3, 22, 22) is False


@pytest.mark.parametrize("start, end", [(None, 7), (22, None), (99, 7),
                                        ("evening", 7)])
def test_a_window_that_is_not_a_window_means_never(start, end):
    assert backlight.is_night(3, start, end) is False


# ----------------------------------------------------- what an hour calls for ----

def test_a_display_nobody_asked_to_dim_is_left_alone():
    assert backlight.level_for(DisplayConfig(), 3) is None


def test_a_plain_brightness_holds_all_day():
    display = DisplayConfig(brightness=70)
    assert backlight.level_for(display, 3) == 70
    assert backlight.level_for(display, 15) == 70


def test_the_night_figure_applies_inside_the_window():
    display = DisplayConfig(brightness=80, night_brightness=15,
                            night_from_hour=22, night_to_hour=7)
    assert backlight.level_for(display, 23) == 15
    assert backlight.level_for(display, 9) == 80


def test_a_window_with_no_night_figure_keeps_the_day_one():
    display = DisplayConfig(brightness=80, night_from_hour=22,
                            night_to_hour=7)
    assert backlight.level_for(display, 23) == 80


def test_applying_puts_the_panel_where_the_hour_says(panel):
    device = panel(max_brightness=100)
    display = DisplayConfig(brightness=80, night_brightness=10,
                            night_from_hour=22, night_to_hour=7)
    assert backlight.apply(display, 23) is True
    assert written(device) == 10
    assert backlight.apply(display, 12) is True
    assert written(device) == 80


def test_applying_nothing_writes_nothing(panel):
    device = panel(max_brightness=100)
    assert backlight.apply(DisplayConfig(), 12) is False
    assert written(device) == 255
