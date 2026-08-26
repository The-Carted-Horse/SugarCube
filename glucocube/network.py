"""Wi-Fi provisioning via NetworkManager (nmcli).

When the Pi has no network at all, we bring up a setup hotspot
("GlucoCube-Setup"). The display shows a WIFI: QR code that joins a
phone to that hotspot, where the settings page offers a list of nearby
networks so the user can hand the Pi their home Wi-Fi credentials.

Scanning is the subtle part: while the radio is running the hotspot it
is in AP mode and *cannot* scan, so a rescan there blocks until it times
out and comes back empty. Every scan therefore goes through a cache that
is refreshed while the device still has a working connection (and once
more immediately before the hotspot comes up); the settings page only
ever reads that cache, so it renders instantly even in AP mode.

Provisioning results are recorded in the store so they survive the
reboot/restart that follows, and so both the settings page and the
physical display can explain what happened.

All calls go through nmcli, which is present on Raspberry Pi OS
(Bookworm and later). On systems without it — e.g. a dev Mac — every
probe degrades to "unknown"/no-op so the rest of the app is unaffected.
"""

import logging
import shutil
import socket
import subprocess
import threading
import time

from . import synclog

log = logging.getLogger("glucocube.network")

HOTSPOT_SSID = "GlucoCube-Setup"
HOTSPOT_CONN = "glucocube-hotspot"
HOTSPOT_ADDR = "10.42.0.1"

STATE_KEY = "__wifi"        # persisted scan cache + last join attempt

_store = None
_lock = threading.Lock()
_joining = threading.Event()    # a join is driving the radio right now
_quiet_until = 0.0              # monotonic; watcher keeps off the radio
_scanning = threading.Event()   # a background rescan is running right now


def get_lan_ip() -> str:
    """Best-effort LAN IP (no packets are actually sent).

    Lives here rather than in display.py so the web UI can show a push
    person the URL to type into Trio without importing pygame.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def hardware_id() -> str:
    """Stable identifier for GlucoCore device registration."""
    import uuid
    node = uuid.getnode()
    if (node >> 40) % 2 == 0:
        return f"mac-{node:012x}"
    host = socket.gethostname().split(".")[0]
    return f"host-{host}"


def init(store) -> None:
    """Give the module a store to persist scan/attempt state in."""
    global _store
    _store = store
    # A join that was in flight when the process died would otherwise
    # leave the display saying "trying to join…" forever.
    if store.get_params(STATE_KEY).get("state") == "joining":
        _save(state="failed", error="interrupted — the device restarted "
              "while connecting", detail="")


def _now_ms() -> int:
    return int(time.time() * 1000)


def state() -> dict:
    if _store is None:
        return {}
    return _store.get_params(STATE_KEY)


def _save(**fields) -> None:
    if _store is None:
        return
    with _lock:
        merged = {**_store.get_params(STATE_KEY), **fields}
        # replace (not merge) so falsy values such as a cleared error
        # actually take effect.
        _store.replace_params(STATE_KEY, merged)


def _redacted(args) -> str:
    """Command line for logs with the value after 'password' masked."""
    parts, mask_next = [], False
    for arg in args:
        parts.append("***" if mask_next else arg)
        mask_next = arg == "password"
    return "nmcli " + " ".join(parts)


def _nmcli(*args, timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["nmcli", *args], capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        # Never str() this exception: it embeds the full argv, and the
        # argv carries the Wi-Fi password.
        return -1, f"{_redacted(args)} timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "nmcli is not installed"


def available() -> bool:
    return shutil.which("nmcli") is not None


def _terse_fields(line: str) -> list[str]:
    """Split one `nmcli -t` row; it escapes ':' and '\\' inside values."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def connectivity() -> str:
    """'full' | 'limited' | 'portal' | 'none' | 'unknown'.

    Raspberry Pi OS ships with NetworkManager's connectivity checking
    disabled, which reports 'unknown' — fall back to checking for a
    default route so 'none' (and with it the setup hotspot) still works.
    """
    if not available():
        return "unknown"
    code, out = _nmcli("networking", "connectivity", "check")
    connected = out.splitlines()[-1].strip() if code == 0 and out else "unknown"
    if connected != "unknown":
        return connected
    try:
        proc = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=10,
        )
        return "limited" if proc.stdout.strip() else "none"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def hotspot_active() -> bool:
    code, out = _nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    return code == 0 and HOTSPOT_CONN in out.splitlines()


_hotspot_cache = (False, 0.0)


def hotspot_active_cached(ttl: float = 5.0) -> bool:
    """hotspot_active(), memoised.

    The web layer asks this on every request — including the captive
    portal's answer to a phone's connectivity probe — and each real call
    shells out to nmcli. GLUCOCUBE_FAKE_HOTSPOT=1 forces it on, which is
    the only way to exercise the setup-hotspot paths off-device; it is
    read from the environment, so it can never be set on an image.
    """
    global _hotspot_cache
    value, checked = _hotspot_cache
    if time.monotonic() - checked > ttl:
        import os
        if os.environ.get("GLUCOCUBE_FAKE_HOTSPOT") == "1":
            value = True
        else:
            try:
                value = available() and hotspot_active()
            except Exception:  # noqa: BLE001 - never fail a page render
                value = False
        _hotspot_cache = (value, time.monotonic())
    return value


def start_hotspot(password: str, prescan: bool = True) -> bool:
    # One last scan while the radio can still hear: the settings page
    # served over this hotspot has no other way to list networks.
    # Skipped on the recovery path after a failed join, where the radio
    # was scanned moments ago and getting the AP back matters more.
    if prescan:
        refresh_scan(force=True)
    code, out = _nmcli(
        "device", "wifi", "hotspot",
        "con-name", HOTSPOT_CONN, "ssid", HOTSPOT_SSID, "password", password,
    )
    if code == 0:
        log.info("Setup hotspot '%s' started", HOTSPOT_SSID)
        synclog.add("network", "system", f"setup hotspot '{HOTSPOT_SSID}' started")
    else:
        log.warning("Could not start hotspot: %s", _scrub(out, password))
        synclog.add("network", "system",
                    f"hotspot failed: {_scrub(out, password)}", ok=False)
        _save(hotspot_error=friendly_error(_scrub(out, password)))
    return code == 0


def stop_hotspot() -> None:
    _nmcli("connection", "down", HOTSPOT_CONN)
    _nmcli("connection", "delete", HOTSPOT_CONN)


def saved_wifi_profiles() -> bool:
    """True if any Wi-Fi connection has ever been configured."""
    code, out = _nmcli("-t", "-f", "TYPE,NAME", "connection", "show")
    if code != 0:
        return False
    return any(
        fields[0] == "802-11-wireless" and fields[1] != HOTSPOT_CONN
        for fields in (_terse_fields(line) for line in out.splitlines())
        if len(fields) >= 2
    )


def hotspot_client_connected() -> bool:
    """True once any device has joined the setup hotspot (ARP on 10.42.0.x)."""
    try:
        for line in open("/proc/net/arp").readlines()[1:]:
            parts = line.split()
            if (len(parts) >= 4 and parts[0].startswith("10.42.0.")
                    and parts[3] != "00:00:00:00:00:00"):
                return True
    except OSError:
        pass
    return False


def wifi_device_state() -> str:
    """State of the first Wi-Fi device: 'connected', 'disconnected', …"""
    code, out = _nmcli("-t", "-f", "TYPE,STATE", "device", "status", timeout=10)
    if code != 0:
        return "unknown"
    for line in out.splitlines():
        fields = _terse_fields(line)
        if len(fields) >= 2 and fields[0] == "wifi":
            return fields[1]
    return "unknown"


def wifi_scan(force: bool = False, timeout: int = 20) -> list[dict]:
    """Nearby networks, strongest first, deduplicated by SSID.

    Never asks for a rescan while the hotspot is up — in AP mode the
    request cannot be served and would simply block until it times out.
    """
    rescan = "no" if hotspot_active() else ("yes" if force else "auto")
    code, out = _nmcli(
        "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
        "--rescan", rescan, timeout=timeout,
    )
    if code != 0:
        log.info("Wi-Fi scan failed: %s", out)
        return []
    seen, networks = set(), []
    for line in out.splitlines():
        fields = _terse_fields(line)
        if len(fields) < 3 or not fields[0] or fields[0] == HOTSPOT_SSID:
            continue
        ssid = fields[0]
        if ssid in seen:
            continue
        seen.add(ssid)
        try:
            signal = int(fields[1])
        except ValueError:
            signal = 0
        networks.append({
            "ssid": ssid,
            "signal": signal,
            "secured": bool(fields[2] and fields[2] != "--"),
        })
    return sorted(networks, key=lambda n: -n["signal"])


def refresh_scan(force: bool = False) -> list[dict]:
    """Scan and cache the result; keeps the last good list on failure."""
    networks = wifi_scan(force=force)
    if networks:
        _save(networks=networks, scanned_at=_now_ms())
    return networks or cached_networks()


def scan_in_progress() -> bool:
    return _scanning.is_set()


def refresh_scan_async(force: bool = True) -> bool:
    """Kick off a rescan without blocking the caller.

    The settings page used to sleep four seconds inside the request
    handler waiting for results; on a phone over a hotspot that reads as
    a hung page. The page now polls for the answer instead.
    """
    if _scanning.is_set():
        return False

    def worker():
        try:
            refresh_scan(force=force)
        finally:
            _scanning.clear()

    _scanning.set()
    threading.Thread(target=worker, name="wifi-scan", daemon=True).start()
    return True


def cached_networks() -> list[dict]:
    """Last known scan — what the settings page renders from."""
    return state().get("networks") or []


def scan_age_seconds() -> float | None:
    scanned_at = state().get("scanned_at")
    if not scanned_at:
        return None
    return max(0.0, (_now_ms() - scanned_at) / 1000)


def _scrub(text: str, secret: str) -> str:
    """Remove a password from anything about to be stored or displayed."""
    if secret and len(secret) >= 4 and secret in text:
        return text.replace(secret, "***")
    return text


def friendly_error(raw: str) -> str:
    """Turn nmcli's output into something a person can act on."""
    text = (raw or "").strip()
    low = text.lower()
    if ("secrets were required" in low
            or "802.1x supplicant" in low
            or "took too long to authenticate" in low):
        return "wrong Wi-Fi password"
    if "no network with ssid" in low:
        return ("network not found — check the name, move the device closer, "
                "or tick 'hidden network'")
    if "not authorized" in low or "insufficient privileges" in low:
        return "the device is not allowed to change network settings (polkit)"
    if "psk" in low and "invalid" in low:
        return "password rejected (Wi-Fi passwords must be 8-63 characters)"
    if "timed out" in low or "timeout" in low:
        return "timed out while connecting"
    return text.replace("Error: ", "").strip() or "unknown error"


def _wait_for_station_mode(deadline: float) -> None:
    """After tearing down the AP the radio needs a moment to come back."""
    while time.time() < deadline:
        if wifi_device_state() in ("disconnected", "connected", "connecting"):
            return
        time.sleep(1)


def _rescan_for(ssid: str, deadline: float) -> bool:
    """Rescan until the target network shows up (nmcli needs it listed).

    Hard-bounded by ``deadline``: while this runs the device has neither
    a network nor the setup hotspot, so it must never become the reason
    the user is locked out.
    """
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        budget = max(5, int(deadline - time.time()))
        code, out = _nmcli("device", "wifi", "rescan", timeout=min(15, budget))
        if code != 0 and "not allowed" not in out.lower():
            log.info("Rescan attempt %d: %s", attempt, out)
        time.sleep(min(3, max(0.0, deadline - time.time())))
        networks = wifi_scan(timeout=min(10, max(5, int(deadline - time.time()))))
        if networks:
            _save(networks=networks, scanned_at=_now_ms())
        if any(n["ssid"] == ssid for n in networks):
            return True
    return False


def connect_wifi(ssid: str, password: str, hidden: bool = False) -> tuple[bool, str]:
    """Leave the hotspot (if up) and join the given network.

    Single-flight: a second attempt while one is in progress would drive
    the same radio, and the loser's cleanup would delete the profile the
    winner just created.
    """
    global _quiet_until
    if not _joining_acquire():
        return False, "another join is already in progress"
    try:
        return _connect_wifi(ssid, password, hidden)
    finally:
        # Keep the watcher off the radio a little longer so the caller
        # can bring the setup hotspot back without a race.
        _quiet_until = time.monotonic() + 20
        _joining.clear()


def _joining_acquire() -> bool:
    with _lock:
        if _joining.is_set():
            return False
        _joining.set()
        return True


def _connect_wifi(ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
    global _quiet_until
    # The watcher must not restart the hotspot while we drive the radio:
    # on a fresh device it would otherwise fire on its very next tick
    # (no saved profile => one failed check is enough) and abort the join.
    _quiet_until = time.monotonic() + 240
    _save(state="joining", ssid=ssid, error="", detail="",
          reboot_error="", hotspot_error="", attempted_at=_now_ms())
    synclog.add("network", "system", f"joining Wi-Fi '{ssid}'")

    if hotspot_active():
        stop_hotspot()
        _wait_for_station_mode(time.time() + 15)

    # nmcli can only join a network it can see (hidden ones excepted), and
    # the radio has just come out of AP mode with a stale or empty list.
    if not hidden and not _rescan_for(ssid, deadline=time.time() + 30):
        log.info("'%s' not in scan results; trying to connect anyway", ssid)

    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    # Worst case to here is ~105s of no network and no hotspot; the
    # caller brings the hotspot straight back afterwards.
    code, out = _nmcli(*args, timeout=60)

    # Belt and braces: nothing carrying the password reaches the log,
    # the database, or the settings page.
    out = _scrub(out, password)

    if code == 0:
        log.info("Joined Wi-Fi network '%s'", ssid)
        synclog.add("network", "system", f"joined Wi-Fi '{ssid}'")
        _save(state="ok", ssid=ssid, error="", detail="",
              reboot_error="", finished_at=_now_ms())
        return True, out

    reason = friendly_error(out)
    log.warning("Failed to join '%s': %s", ssid, out)
    synclog.add("network", "system", f"failed to join '{ssid}': {reason}", ok=False)
    _save(state="failed", ssid=ssid, error=reason, detail=out[:500],
          finished_at=_now_ms())
    # nmcli can leave the half-created profile behind (especially when the
    # timeout kills it); a saved bad profile would autoconnect-fight the
    # setup hotspot, so drop it.
    _nmcli("connection", "delete", "id", ssid)
    return False, reason


def reboot() -> bool:
    """Restart the device so everything comes up cleanly on the new network.

    Works as the unprivileged service user thanks to the polkit rule the
    installer/image drops in; anywhere else the refusal is logged (and the
    display still recovers on its own — it just takes a little longer).
    """
    log.info("Rebooting to apply network change")
    synclog.add("network", "system", "rebooting to apply Wi-Fi settings")
    try:
        proc = subprocess.run(
            ["systemctl", "reboot"], capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            err = (proc.stdout + proc.stderr).strip()
            log.warning("Reboot refused: %s", err)
            synclog.add("network", "system", f"reboot refused: {err}", ok=False)
            _save(reboot_error=friendly_error(err))
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("Reboot failed: %s", exc)
        synclog.add("network", "system", f"reboot failed: {exc}", ok=False)
        _save(reboot_error=str(exc))
        return False


class NetworkWatcher(threading.Thread):
    """Brings the setup hotspot up when the device has no network at all.

    On a fresh device (no Wi-Fi ever configured) the hotspot comes up on
    the first failed check so setup starts right away. Once a Wi-Fi
    network has been saved, three consecutive failures (~90s) are
    required so brief outages and router reboots don't tear down normal
    networking. 'none' means no connection whatsoever — LAN-only setups
    report 'limited' and are left alone.

    While the device is online it also keeps the scan cache warm, so the
    settings page has a network list to offer once the hotspot is up.
    """

    CHECK_SECONDS = 30
    FAILS_NEEDED = 3
    FIRST_CHECK_DELAY = 5
    SCAN_REFRESH_SECONDS = 300

    def __init__(self, hotspot_password: str):
        super().__init__(name="network-watcher", daemon=True)
        self.hotspot_password = hotspot_password
        self._fails = 0
        self._last_scan = 0.0
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        if not available():
            log.info("nmcli not found; Wi-Fi provisioning disabled")
            return
        self._stopping.wait(self.FIRST_CHECK_DELAY)
        while not self._stopping.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - never kill the watcher
                log.warning("Network watcher error: %s", exc)
            self._stopping.wait(self.CHECK_SECONDS)

    def _tick(self) -> None:
        # A join in progress has deliberately torn the hotspot down and
        # left the device with no connectivity — exactly the state this
        # watcher exists to "fix". Firing here would knock the radio back
        # into AP mode and abort the join, which on a fresh device (no
        # saved profile, so one failed check is enough) happens on the
        # very next tick and makes every join fail.
        if _joining.is_set() or time.monotonic() < _quiet_until:
            self._fails = 0
            return
        connected = connectivity()
        if hotspot_active():
            self._fails = 0  # we're in setup mode; stay until joined
            return
        if connected == "none":
            self._fails += 1
            needed = self.FAILS_NEEDED if saved_wifi_profiles() else 1
            if self._fails >= needed:
                start_hotspot(self.hotspot_password)
            return
        self._fails = 0
        # Online: keep a fresh list of neighbours for the next setup round.
        if time.monotonic() - self._last_scan > self.SCAN_REFRESH_SECONDS:
            self._last_scan = time.monotonic()
            refresh_scan(force=True)
