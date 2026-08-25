"""touch.py — decoding the panel's evdev stream.

Nothing here needs a touchscreen: the device object is built by hand and
fed the same ``struct input_event`` bytes the kernel would write, which is
the part that has to be right for the NIGHT/DAY toggle to work on the
ready-made image (where SDL delivers no events at all).
"""

import os

import pytest

from glucocube import touch
from glucocube.touch import (
    ABS_MT_POSITION_X,
    ABS_MT_POSITION_Y,
    ABS_MT_TRACKING_ID,
    ABS_X,
    ABS_Y,
    BTN_TOUCH,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    EVENT,
    SYN_REPORT,
    TouchReader,
    _Device,
)


def event(etype: int, code: int, value: int) -> bytes:
    return EVENT.pack(0, 0, etype, code, value)


def device(multitouch: bool = True, width: int = 800, height: int = 480,
           x_range=(0, 799), y_range=(0, 479)) -> _Device:
    """A device object with fixed axis ranges and no file descriptor.

    ``_abs_range`` ioctls a closed fd and falls back to the panel size,
    which is exactly the fallback the Pi's own panel uses.
    """
    dev = _Device.__new__(_Device)
    dev.fd = -1
    dev.path = "/dev/input/event0"
    dev.name = "test panel"
    dev.multitouch = multitouch
    dev.x_min, dev.x_max = x_range
    dev.y_min, dev.y_max = y_range
    dev.x = dev.y = None
    dev._pending_down = False
    dev._contact = False
    dev._buffer = b""
    return dev


# ------------------------------------------------------------- decoding ----

def test_a_multitouch_tap_is_reported_once_the_frame_ends():
    dev = device()
    stream = (event(EV_ABS, ABS_MT_TRACKING_ID, 7)
              + event(EV_ABS, ABS_MT_POSITION_X, 400)
              + event(EV_ABS, ABS_MT_POSITION_Y, 240))
    assert dev.feed(stream) == []           # no SYN_REPORT yet
    taps = dev.feed(event(EV_SYN, SYN_REPORT, 0))
    assert len(taps) == 1
    assert taps[0] == pytest.approx((0.5, 0.5), abs=0.01)


def test_a_single_touch_panel_reports_through_btn_touch():
    dev = device(multitouch=False)
    stream = (event(EV_KEY, BTN_TOUCH, 1)
              + event(EV_ABS, ABS_X, 0)
              + event(EV_ABS, ABS_Y, 479)
              + event(EV_SYN, SYN_REPORT, 0))
    assert dev.feed(stream) == [(0.0, 1.0)]


def test_holding_a_contact_reports_one_tap_not_a_stream():
    """Otherwise resting a finger would toggle the theme dozens of times."""
    dev = device()
    dev.feed(event(EV_ABS, ABS_MT_TRACKING_ID, 7)
             + event(EV_ABS, ABS_MT_POSITION_X, 100)
             + event(EV_ABS, ABS_MT_POSITION_Y, 100)
             + event(EV_SYN, SYN_REPORT, 0))
    moved = dev.feed(event(EV_ABS, ABS_MT_POSITION_X, 120)
                     + event(EV_SYN, SYN_REPORT, 0))
    assert moved == []


def test_lifting_and_touching_again_is_a_second_tap():
    dev = device()
    press = (event(EV_ABS, ABS_MT_TRACKING_ID, 7)
             + event(EV_ABS, ABS_MT_POSITION_X, 100)
             + event(EV_ABS, ABS_MT_POSITION_Y, 100)
             + event(EV_SYN, SYN_REPORT, 0))
    lift = event(EV_ABS, ABS_MT_TRACKING_ID, -1) + event(EV_SYN, SYN_REPORT, 0)
    assert len(dev.feed(press)) == 1
    dev.feed(lift)
    assert len(dev.feed(press)) == 1


def test_a_press_with_no_coordinates_waits_for_the_frame_that_has_them():
    dev = device()
    assert dev.feed(event(EV_KEY, BTN_TOUCH, 1)
                    + event(EV_SYN, SYN_REPORT, 0)) == []
    taps = dev.feed(event(EV_ABS, ABS_X, 400)
                    + event(EV_ABS, ABS_Y, 240)
                    + event(EV_SYN, SYN_REPORT, 0))
    assert len(taps) == 1


def test_a_partial_read_is_buffered_until_the_event_is_whole():
    """select()/read() gives whatever is in the buffer, not whole events."""
    dev = device()
    stream = (event(EV_ABS, ABS_MT_TRACKING_ID, 7)
              + event(EV_ABS, ABS_MT_POSITION_X, 400)
              + event(EV_ABS, ABS_MT_POSITION_Y, 240)
              + event(EV_SYN, SYN_REPORT, 0))
    split = len(stream) - 5
    assert dev.feed(stream[:split]) == []
    assert len(dev.feed(stream[split:])) == 1


def test_unrelated_events_are_ignored():
    dev = device()
    noise = event(EV_KEY, 0x100, 1) + event(EV_ABS, 0x28, 5)
    assert dev.feed(noise) == []


def test_both_a_tracking_id_and_btn_touch_still_mean_one_tap():
    """Many panels emit both; the flag is simply set twice."""
    dev = device()
    stream = (event(EV_ABS, ABS_MT_TRACKING_ID, 7)
              + event(EV_KEY, BTN_TOUCH, 1)
              + event(EV_ABS, ABS_MT_POSITION_X, 400)
              + event(EV_ABS, ABS_MT_POSITION_Y, 240)
              + event(EV_SYN, SYN_REPORT, 0))
    assert len(dev.feed(stream)) == 1


# ------------------------------------------------------------ normalize ----

@pytest.mark.parametrize("x, y, expected", [
    (0, 0, (0.0, 0.0)),
    (799, 479, (1.0, 1.0)),
    (400, 240, (0.5, 0.5)),
    (-50, -50, (0.0, 0.0)),        # out of range readings are clamped
    (5000, 5000, (1.0, 1.0)),
])
def test_coordinates_are_normalized_and_clamped(x, y, expected):
    dev = device()
    assert dev._normalize(x, y) == pytest.approx(expected, abs=0.01)


def test_a_panel_whose_axes_do_not_start_at_zero_is_handled():
    dev = device(x_range=(100, 300), y_range=(200, 400))
    assert dev._normalize(200, 300) == pytest.approx((0.5, 0.5))


def test_a_degenerate_axis_range_does_not_divide_by_zero():
    dev = device(x_range=(0, 0), y_range=(0, 0))
    assert dev._normalize(0, 0) == (0.0, 0.0)


# ------------------------------------------------------------ transform ----

@pytest.mark.parametrize("value, expected", [
    ("", (False, False, False)),
    ("swap", (True, False, False)),
    ("invx", (False, True, False)),
    ("invy", (False, False, True)),
    ("swap,invx,invy", (True, True, True)),
    (" SWAP , InvY ", (True, False, True)),
    ("nonsense", (False, False, False)),
])
def test_the_orientation_transform_is_read_from_the_environment(
        monkeypatch, value, expected):
    monkeypatch.setenv("GLUCOCUBE_TOUCH_TRANSFORM", value)
    assert touch._transform() == expected


def test_no_transform_variable_at_all_means_no_transform(monkeypatch):
    monkeypatch.delenv("GLUCOCUBE_TOUCH_TRANSFORM", raising=False)
    assert touch._transform() == (False, False, False)


# ----------------------------------------------------------- TouchReader ----

def reader(transform: str, monkeypatch, size=(800, 480)):
    monkeypatch.setenv("GLUCOCUBE_TOUCH_TRANSFORM", transform)
    taps = []
    return TouchReader(size[0], size[1], lambda x, y: taps.append((x, y))), taps


def test_a_tap_is_delivered_in_screen_pixels(monkeypatch):
    reader_, taps = reader("", monkeypatch)
    reader_._emit(0.5, 0.25)
    assert taps == [(400.0, 120.0)]


def test_swap_exchanges_the_axes(monkeypatch):
    reader_, taps = reader("swap", monkeypatch)
    reader_._emit(0.0, 1.0)
    assert taps == [(800.0, 0.0)]


def test_invx_mirrors_horizontally(monkeypatch):
    reader_, taps = reader("invx", monkeypatch)
    reader_._emit(0.25, 0.5)
    assert taps == [(600.0, 240.0)]


def test_invy_mirrors_vertically(monkeypatch):
    reader_, taps = reader("invy", monkeypatch)
    reader_._emit(0.5, 0.25)
    assert taps == [(400.0, 360.0)]


def test_a_failing_tap_handler_does_not_kill_the_reader(monkeypatch):
    """The handler runs UI code; a bad frame must not end touch for good."""
    monkeypatch.setenv("GLUCOCUBE_TOUCH_TRANSFORM", "")

    def boom(x, y):
        raise RuntimeError("bad handler")

    TouchReader(800, 480, boom)._emit(0.5, 0.5)   # must not raise


def test_starting_without_a_touchscreen_is_a_no_op(monkeypatch):
    """A dev machine, or a kmsdrm install where SDL delivers events."""
    monkeypatch.setattr(touch, "open_touch_devices", lambda w, h: [])
    assert TouchReader(800, 480, lambda x, y: None).start() is False


def test_stopping_closes_every_device(monkeypatch):
    closed = []

    class FakeDevice:
        fd = 99

        def close(self):
            closed.append(True)

    monkeypatch.setattr(touch, "open_touch_devices", lambda w, h: [FakeDevice()])
    monkeypatch.setattr(touch.threading, "Thread",
                        lambda **kwargs: type("T", (), {"start": lambda s: None})())
    reader_ = TouchReader(800, 480, lambda x, y: None)
    reader_.start()
    reader_.stop()
    assert closed == [True]


def test_no_touch_devices_are_opened_where_there_is_no_input_directory(
        monkeypatch):
    monkeypatch.setattr(touch.glob, "glob", lambda pattern: [])
    assert touch.open_touch_devices(800, 480) == []


def test_a_device_that_cannot_be_opened_is_skipped(monkeypatch):
    monkeypatch.setattr(touch.glob, "glob", lambda pattern: ["/dev/input/event0"])

    def denied(path, flags):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "open", denied)
    assert touch.open_touch_devices(800, 480) == []
