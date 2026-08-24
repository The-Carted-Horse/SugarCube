"""Touchscreen input read straight from evdev.

The shipped image runs the display with ``SUGARCUBE_DISPLAY=fbdev``, which
forces SDL's *dummy* video driver (see ``__main__.py``). That driver pumps
no events and never opens ``/dev/input``, so pygame sees no taps at all —
which is exactly why the NIGHT/DAY toggle stopped working on the image
while it kept working on kmsdrm installs.

This module reads the panel itself: it finds the touch device, decodes the
kernel's ``struct input_event`` stream, and calls back with tap positions
in screen pixels. Standard library only; the systemd units already put the
service user in the ``input`` group.

Where no touch device can be found or opened — a dev machine, a kmsdrm
install where SDL delivers events anyway — ``start()`` is a logged no-op
and nothing else in the app changes.

Panel orientation can be corrected without code changes via
``SUGARCUBE_TOUCH_TRANSFORM``, a comma-separated list of ``swap``,
``invx`` and ``invy`` (applied in that order).
"""

import errno
import fcntl
import glob
import logging
import os
import select
import struct
import threading

log = logging.getLogger("sugarcube.touch")

# ---- kernel constants (linux/input-event-codes.h) ----

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39
ABS_CNT, KEY_CNT = 0x40, 0x300

# struct input_event { struct timeval time; __u16 type, code; __s32 value; }
# Native sizing matters: 24 bytes on 64-bit, 16 on 32-bit userland.
EVENT = struct.Struct("@llHHi")
# struct input_absinfo { __s32 value, minimum, maximum, fuzz, flat, resolution; }
ABSINFO = struct.Struct("@6i")


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


def _ior(nr: int, size: int) -> int:
    return _ioc(2, "E", nr, size)


def EVIOCGNAME(length: int) -> int:
    return _ior(0x06, length)


def EVIOCGBIT(ev: int, length: int) -> int:
    return _ior(0x20 + ev, length)


def EVIOCGABS(code: int) -> int:
    return _ior(0x40 + code, ABSINFO.size)


def _bit(buf: bytes, index: int) -> bool:
    """Test one bit of a kernel capability bitmap.

    Byte-addressed on purpose: the textual bitmaps in
    /proc/bus/input/devices are printed in words whose width follows the
    *kernel's* long, which is 64-bit even under 32-bit userland on a Pi.
    Reading the bitmap over ioctl side-steps that mismatch entirely.
    """
    byte = index // 8
    return byte < len(buf) and bool(buf[byte] & (1 << (index % 8)))


def _capabilities(fd: int, ev: int, count: int) -> bytes:
    buf = bytearray((count + 7) // 8)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(ev, len(buf)), buf, True)
    except OSError:
        return b""
    return bytes(buf)


def _device_name(fd: int) -> str:
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(len(buf)), buf, True)
    except OSError:
        return "?"
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _abs_range(fd: int, code: int, fallback: int) -> tuple[int, int]:
    buf = bytearray(ABSINFO.size)
    try:
        fcntl.ioctl(fd, EVIOCGABS(code), buf, True)
    except OSError:
        return 0, max(1, fallback - 1)
    _, minimum, maximum, _, _, _ = ABSINFO.unpack(buf)
    if maximum <= minimum:
        return 0, max(1, fallback - 1)
    return minimum, maximum


def _transform() -> tuple[bool, bool, bool]:
    raw = os.environ.get("SUGARCUBE_TOUCH_TRANSFORM", "")
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return "swap" in parts, "invx" in parts, "invy" in parts


class _Device:
    """One open touch device and the tap state machine for it."""

    def __init__(self, fd: int, path: str, name: str, width: int, height: int,
                 multitouch: bool):
        self.fd = fd
        self.path = path
        self.name = name
        self.multitouch = multitouch
        x_code = ABS_MT_POSITION_X if multitouch else ABS_X
        y_code = ABS_MT_POSITION_Y if multitouch else ABS_Y
        self.x_min, self.x_max = _abs_range(fd, x_code, width)
        self.y_min, self.y_max = _abs_range(fd, y_code, height)
        self.x: int | None = None
        self.y: int | None = None
        self._pending_down = False
        self._contact = False
        self._buffer = b""

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def feed(self, data: bytes) -> list[tuple[float, float]]:
        """Decode a read() chunk; return the taps it completed (0..1 range)."""
        taps = []
        self._buffer += data
        size = EVENT.size
        count = len(self._buffer) // size
        for i in range(count):
            _, _, etype, code, value = EVENT.unpack_from(self._buffer, i * size)
            taps.extend(self._event(etype, code, value))
        self._buffer = self._buffer[count * size:]
        return taps

    def _event(self, etype: int, code: int, value: int) -> list:
        if etype == EV_ABS:
            if code in (ABS_MT_POSITION_X, ABS_X):
                self.x = value
            elif code in (ABS_MT_POSITION_Y, ABS_Y):
                self.y = value
            elif code == ABS_MT_TRACKING_ID:
                # Type-B multitouch: a new tracking id is a new contact,
                # -1 is a lift. Panels that also emit BTN_TOUCH just set
                # the same flag twice, which is harmless.
                if value == -1:
                    self._contact = False
                elif not self._contact:
                    self._contact = True
                    self._pending_down = True
        elif etype == EV_KEY and code == BTN_TOUCH:
            if value:
                if not self._contact:
                    self._contact = True
                    self._pending_down = True
            else:
                self._contact = False
        elif etype == EV_SYN and code == SYN_REPORT:
            if self._pending_down and self.x is not None and self.y is not None:
                self._pending_down = False
                return [self._normalize(self.x, self.y)]
            # A press with no coordinates yet stays pending until the
            # frame that carries them.
        return []

    def _normalize(self, x: int, y: int) -> tuple[float, float]:
        fx = (x - self.x_min) / max(1, self.x_max - self.x_min)
        fy = (y - self.y_min) / max(1, self.y_max - self.y_min)
        return min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))


def open_touch_devices(width: int, height: int) -> list[_Device]:
    """Every readable /dev/input device that looks like a touchscreen."""
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.ENOENT, errno.ENODEV):
                log.debug("touch: cannot open %s: %s", path, exc)
            continue
        abs_bits = _capabilities(fd, EV_ABS, ABS_CNT)
        key_bits = _capabilities(fd, EV_KEY, KEY_CNT)
        multitouch = _bit(abs_bits, ABS_MT_POSITION_X)
        single = _bit(abs_bits, ABS_X) and _bit(key_bits, BTN_TOUCH)
        if not (multitouch or single):
            os.close(fd)
            continue
        name = _device_name(fd)
        device = _Device(fd, path, name, width, height, multitouch)
        log.info("touch: using %s (%s), x=%d..%d y=%d..%d%s",
                 path, name, device.x_min, device.x_max,
                 device.y_min, device.y_max,
                 " multitouch" if multitouch else "")
        devices.append(device)
    return devices


class TouchReader:
    """Background thread turning evdev contacts into on_tap(x, y) in pixels."""

    def __init__(self, width: int, height: int, on_tap):
        self.width = width
        self.height = height
        self.on_tap = on_tap
        self._devices: list[_Device] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._swap, self._invx, self._invy = _transform()

    def start(self) -> bool:
        """Open the panel and start reading. False when there is nothing to read."""
        self._devices = open_touch_devices(self.width, self.height)
        if not self._devices:
            log.info("touch: no touchscreen found under /dev/input "
                     "(the on-screen toggle will need the web UI)")
            return False
        self._thread = threading.Thread(target=self._run, name="touch",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        for device in self._devices:
            device.close()
        self._devices = []

    def _run(self) -> None:
        while not self._stop.is_set():
            fds = [d.fd for d in self._devices]
            if not fds:
                return
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                return
            for device in list(self._devices):
                if device.fd not in ready:
                    continue
                try:
                    data = os.read(device.fd, EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    log.warning("touch: %s went away (%s)", device.path, exc)
                    self._devices.remove(device)
                    device.close()
                    continue
                if not data:
                    continue
                for fx, fy in device.feed(data):
                    self._emit(fx, fy)

    def _emit(self, fx: float, fy: float) -> None:
        if self._swap:
            fx, fy = fy, fx
        if self._invx:
            fx = 1.0 - fx
        if self._invy:
            fy = 1.0 - fy
        try:
            self.on_tap(fx * self.width, fy * self.height)
        except Exception as exc:  # noqa: BLE001 - a bad callback must not
            log.warning("touch: tap handler failed: %s", exc)  # kill the reader
