"""network.py — nmcli parsing and the messages a person actually reads.

The device is provisioned over its own hotspot with no other way in, so
"wrong Wi-Fi password" has to say that rather than quoting NetworkManager.
Nothing here shells out: ``_nmcli`` is stubbed throughout.
"""

import json
import subprocess

import pytest

from glucocube import network

# Captured before the autouse fixture stubs it, for the few tests that
# exercise the subprocess wrapper itself.
REAL_NMCLI = network._nmcli
REAL_HOTSPOT_CACHED = network.hotspot_active_cached


@pytest.fixture
def nmcli(monkeypatch):
    """Stub nmcli with canned answers keyed by the first argument."""
    def install(answers, present=True):
        calls = []

        def fake(*args, timeout=20):
            calls.append(args)
            for key, answer in answers.items():
                if key in " ".join(args):
                    return answer
            return (0, "")

        monkeypatch.setattr(network, "_nmcli", fake)
        monkeypatch.setattr(network, "available", lambda: present)
        return calls
    return install


# ------------------------------------------------------------ _redacted ----

def test_a_logged_command_line_masks_the_wifi_password():
    """nmcli's argv carries the password; it must never reach a log."""
    line = network._redacted(["device", "wifi", "connect", "Home",
                              "password", "hunter2hunter2"])
    assert "hunter2hunter2" not in line
    assert line.endswith("password ***")


def test_a_timed_out_command_is_reported_without_the_password(monkeypatch):
    """str(TimeoutExpired) embeds the whole argv, password included."""
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["nmcli", "device", "wifi", "connect", "Home",
                 "password", "hunter2hunter2"], timeout=20)

    monkeypatch.setattr(network.subprocess, "run", timeout)
    code, out = REAL_NMCLI("device", "wifi", "connect", "Home",
                           "password", "hunter2hunter2")
    assert code == -1
    assert "hunter2hunter2" not in out
    assert "timed out" in out


def test_a_missing_nmcli_is_reported_rather_than_raised(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("nmcli")

    monkeypatch.setattr(network.subprocess, "run", missing)
    assert REAL_NMCLI("device", "status") == (-1, "nmcli is not installed")


def test_nmcli_output_combines_both_streams(monkeypatch):
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="out\n", stderr="err\n"))
    assert REAL_NMCLI("device", "status") == (0, "out\nerr")


def test_scrub_removes_a_password_from_stored_text():
    assert network._scrub("psk hunter2 rejected", "hunter2") == "psk *** rejected"


def test_scrub_leaves_a_short_secret_alone():
    assert network._scrub("abc appears here", "abc") == "abc appears here"


# -------------------------------------------------------- _terse_fields ----

def test_terse_output_is_split_on_colons():
    assert network._terse_fields("Home:82:WPA2") == ["Home", "82", "WPA2"]


def test_a_colon_inside_an_ssid_is_not_a_separator():
    """nmcli escapes it; splitting naively renames the network."""
    assert network._terse_fields(r"Cafe\: Bar:71:WPA2") == \
        ["Cafe: Bar", "71", "WPA2"]


def test_an_escaped_backslash_survives():
    assert network._terse_fields(r"back\\slash:60:") == \
        ["back\\slash", "60", ""]


def test_an_empty_line_is_one_empty_field():
    assert network._terse_fields("") == [""]


# --------------------------------------------------------- connectivity ----

def test_connectivity_without_nmcli_is_unknown(nmcli):
    nmcli({}, present=False)
    assert network.connectivity() == "unknown"


def test_connectivity_reports_what_nmcli_says(nmcli):
    nmcli({"connectivity": (0, "full")})
    assert network.connectivity() == "full"


def test_connectivity_uses_the_last_line_of_the_answer(nmcli):
    nmcli({"connectivity": (0, "some warning\nlimited")})
    assert network.connectivity() == "limited"


def test_unknown_connectivity_falls_back_to_the_routing_table(nmcli, monkeypatch):
    """Raspberry Pi OS ships with NetworkManager's own check disabled."""
    nmcli({"connectivity": (0, "unknown")})
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="default via 192.168.1.1\n", stderr=""))
    assert network.connectivity() == "limited"


def test_no_default_route_means_no_network(nmcli, monkeypatch):
    nmcli({"connectivity": (0, "unknown")})
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="", stderr=""))
    assert network.connectivity() == "none"


def test_connectivity_survives_a_system_without_ip(nmcli, monkeypatch):
    nmcli({"connectivity": (0, "unknown")})

    def missing(*args, **kwargs):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(network.subprocess, "run", missing)
    assert network.connectivity() == "unknown"


# ------------------------------------------------------------ wifi_scan ----

SCAN = "\n".join([
    "Home:82:WPA2",
    "Cafe:41:WPA1 WPA2",
    "Open Guest:55:",
    "Home:38:WPA2",             # the same network on another band
    ":90:WPA2",                 # a hidden network
    "GlucoCube-Setup:99:WPA2",  # our own hotspot
])


def test_a_scan_is_deduplicated_and_sorted_by_signal(nmcli):
    nmcli({"device wifi list": (0, SCAN)})
    networks = network.wifi_scan()
    assert [n["ssid"] for n in networks] == ["Home", "Open Guest", "Cafe"]


def test_an_open_network_is_marked_as_such(nmcli):
    nmcli({"device wifi list": (0, SCAN)})
    by_ssid = {n["ssid"]: n for n in network.wifi_scan()}
    assert by_ssid["Open Guest"]["secured"] is False
    assert by_ssid["Home"]["secured"] is True


def test_our_own_hotspot_is_not_offered_to_join(nmcli):
    nmcli({"device wifi list": (0, SCAN)})
    assert network.HOTSPOT_SSID not in [n["ssid"] for n in network.wifi_scan()]


def test_a_failed_scan_is_an_empty_list_not_an_exception(nmcli):
    nmcli({"device wifi list": (1, "Error: device not ready")})
    assert network.wifi_scan() == []


def test_a_junk_signal_does_not_break_the_list(nmcli):
    nmcli({"device wifi list": (0, "Home:not-a-number:WPA2")})
    assert network.wifi_scan() == [{"ssid": "Home", "signal": 0,
                                    "secured": True}]


def test_no_rescan_is_requested_while_the_hotspot_is_up(nmcli, monkeypatch):
    """In AP mode the radio cannot scan; asking just blocks until timeout."""
    calls = nmcli({"connection show": (0, network.HOTSPOT_CONN),
                   "device wifi list": (0, "")})
    network.wifi_scan(force=True)
    scan_call = [c for c in calls if "list" in c][0]
    assert "--rescan" in scan_call
    assert scan_call[scan_call.index("--rescan") + 1] == "no"


def test_a_forced_rescan_is_requested_otherwise(nmcli):
    calls = nmcli({"connection show": (0, "other-connection"),
                   "device wifi list": (0, "")})
    network.wifi_scan(force=True)
    scan_call = [c for c in calls if "list" in c][0]
    assert scan_call[scan_call.index("--rescan") + 1] == "yes"


# ------------------------------------------------------- friendly_error ----

@pytest.mark.parametrize("raw, expected", [
    ("Error: 802-11-wireless-security.psk: Secrets were required",
     "wrong Wi-Fi password"),
    ("Error: Connection activation failed: (7) Secrets were required",
     "wrong Wi-Fi password"),
    ("Error: device took too long to authenticate", "wrong Wi-Fi password"),
    ("Error: No network with SSID 'Home' found.", "network not found"),
    ("Error: Not authorized to control networking.", "not allowed"),
    ("Error: property psk is invalid: has to be 8 characters", "8-63"),
    ("nmcli device wifi connect timed out after 60s", "timed out"),
])
def test_nmcli_errors_become_something_a_person_can_act_on(raw, expected):
    assert expected in network.friendly_error(raw)


def test_an_unrecognised_error_is_passed_through_without_the_prefix():
    assert network.friendly_error("Error: something odd") == "something odd"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_no_error_text_still_says_something(raw):
    assert network.friendly_error(raw) == "unknown error"


# ------------------------------------------------------------ hotspot ----

def test_the_hotspot_is_detected_by_its_connection_name(nmcli):
    nmcli({"connection show": (0, f"other\n{network.HOTSPOT_CONN}")})
    assert network.hotspot_active() is True


def test_no_hotspot_when_nmcli_lists_something_else(nmcli):
    nmcli({"connection show": (0, "preconfigured")})
    assert network.hotspot_active() is False


def test_saved_profiles_ignore_our_own_hotspot(nmcli):
    """A device whose only "saved network" is its own hotspot has none."""
    nmcli({"connection show": (0, f"802-11-wireless:{network.HOTSPOT_CONN}")})
    assert network.saved_wifi_profiles() is False


def test_a_real_saved_profile_is_found(nmcli):
    nmcli({"connection show": (0, f"802-11-wireless:{network.HOTSPOT_CONN}\n"
                                  "802-11-wireless:Home\n"
                                  "802-3-ethernet:Wired")})
    assert network.saved_wifi_profiles() is True


# -------------------------------------------------------------- state ----

def test_the_join_state_is_persisted_for_after_the_reboot(store):
    """The display and the setup page both read it once the device is back."""
    network.init(store)
    network._save(state="failed", error="wrong Wi-Fi password", ssid="Home")
    state = network.state()
    assert state["state"] == "failed"
    assert state["error"] == "wrong Wi-Fi password"


def test_state_without_a_store_is_empty():
    assert network.state() == {}


def test_the_cached_scan_survives_a_failed_rescan(store, nmcli):
    network.init(store)
    nmcli({"device wifi list": (0, "Home:82:WPA2")})
    network.refresh_scan()
    nmcli({"device wifi list": (1, "Error: not ready")})
    assert [n["ssid"] for n in network.refresh_scan()] == ["Home"]


def test_the_scan_age_is_reported(store, nmcli):
    network.init(store)
    assert network.scan_age_seconds() is None
    nmcli({"device wifi list": (0, "Home:82:WPA2")})
    network.refresh_scan()
    assert 0 <= network.scan_age_seconds() < 60


def test_get_lan_ip_always_answers_something():
    """It must never raise on a device with no network at all."""
    assert network.get_lan_ip().count(".") == 3


# ------------------------------------------------------------- joining ----

@pytest.fixture
def joining(store, monkeypatch, nmcli):
    """A device ready to be handed Wi-Fi credentials, with nmcli stubbed."""
    network.init(store)
    monkeypatch.setattr(network, "_wait_for_station_mode", lambda deadline: None)
    monkeypatch.setattr(network, "_rescan_for", lambda ssid, deadline: True)
    monkeypatch.setattr(network, "_quiet_until", 0.0, raising=False)
    network._joining.clear()
    yield nmcli
    network._joining.clear()


def test_a_successful_join_is_recorded_for_after_the_reboot(joining):
    joining({"device wifi connect": (0, "successfully activated")})
    ok, _detail = network.connect_wifi("Home", "hunter2hunter2")
    assert ok is True
    state = network.state()
    assert state["state"] == "ok"
    assert state["ssid"] == "Home"


def test_a_failed_join_records_why_in_words_a_person_can_use(joining):
    joining({"device wifi connect": (4, "Error: Secrets were required")})
    ok, reason = network.connect_wifi("Home", "wrongpassword")
    assert ok is False
    assert reason == "wrong Wi-Fi password"
    assert network.state()["error"] == "wrong Wi-Fi password"


def test_a_failed_join_deletes_the_half_made_profile(joining):
    """A saved bad profile autoconnect-fights the setup hotspot."""
    calls = joining({"device wifi connect": (4, "Error: Secrets were required")})
    network.connect_wifi("Home", "wrongpassword")
    assert any(args[:2] == ("connection", "delete") and "Home" in args
               for args in calls)


def test_the_password_never_reaches_the_stored_state_or_the_log(joining):
    from glucocube import synclog

    password = "hunter2hunter2"
    joining({"device wifi connect": (4, f"Error: psk {password} invalid")})
    network.connect_wifi("Home", password)
    assert password not in json.dumps(network.state())
    assert password not in json.dumps(synclog.recent())


def test_a_hidden_network_is_joined_without_waiting_for_a_scan(joining,
                                                               monkeypatch):
    monkeypatch.setattr(network, "_rescan_for",
                        lambda ssid, deadline: pytest.fail("should not rescan"))
    calls = joining({"device wifi connect": (0, "ok")})
    network.connect_wifi("Invisible", "hunter2hunter2", hidden=True)
    connect = [args for args in calls if args[:3] == ("device", "wifi", "connect")][0]
    assert connect[-2:] == ("hidden", "yes")


def test_the_hotspot_is_torn_down_before_joining(joining):
    calls = joining({"connection show": (0, network.HOTSPOT_CONN),
                     "device wifi connect": (0, "ok")})
    network.connect_wifi("Home", "hunter2hunter2")
    assert any(args[:2] == ("connection", "down") for args in calls)


def test_only_one_join_runs_at_a_time(joining):
    """The loser's cleanup would delete the profile the winner just made."""
    joining({"device wifi connect": (0, "ok")})
    network._joining.set()
    try:
        ok, reason = network.connect_wifi("Home", "hunter2hunter2")
    finally:
        network._joining.clear()
    assert ok is False
    assert "already in progress" in reason


def test_starting_the_hotspot_names_it_for_the_qr_code(store, nmcli,
                                                        monkeypatch):
    network.init(store)
    monkeypatch.setattr(network, "refresh_scan", lambda force=False: [])
    calls = nmcli({"device wifi hotspot": (0, "")})
    assert network.start_hotspot("hotpass") is True
    hotspot = [args for args in calls if "hotspot" in args][0]
    assert network.HOTSPOT_SSID in hotspot
    assert "hotpass" in hotspot


def test_a_hotspot_that_will_not_start_is_reported_without_its_password(
        store, nmcli, monkeypatch):
    network.init(store)
    monkeypatch.setattr(network, "refresh_scan", lambda force=False: [])
    nmcli({"device wifi hotspot": (4, "Error: hotpasshotpass rejected")})
    assert network.start_hotspot("hotpasshotpass") is False
    assert "hotpasshotpass" not in json.dumps(network.state())
    assert network.state()["hotspot_error"]


def test_the_hotspot_state_is_cached_between_page_renders(nmcli, monkeypatch):
    """Every request asks; each real answer costs an nmcli invocation."""
    calls = nmcli({"connection show": (0, network.HOTSPOT_CONN)})
    monkeypatch.setattr(network, "available", lambda: True)
    monkeypatch.setattr(network, "_hotspot_cache", (False, 0.0), raising=False)
    assert REAL_HOTSPOT_CACHED() is True
    before = len(calls)
    assert REAL_HOTSPOT_CACHED() is True
    assert len(calls) == before


def test_the_hotspot_can_be_faked_for_development(monkeypatch):
    """The only way to exercise the setup-portal paths off-device."""
    monkeypatch.setenv("GLUCOCUBE_FAKE_HOTSPOT", "1")
    monkeypatch.setattr(network, "_hotspot_cache", (False, 0.0), raising=False)
    assert REAL_HOTSPOT_CACHED() is True


def test_a_failing_probe_never_breaks_a_page_render(monkeypatch):
    monkeypatch.delenv("GLUCOCUBE_FAKE_HOTSPOT", raising=False)
    monkeypatch.setattr(network, "available", lambda: True)
    monkeypatch.setattr(network, "hotspot_active",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(network, "_hotspot_cache", (False, 0.0), raising=False)
    assert REAL_HOTSPOT_CACHED() is False


# ------------------------------------------------------------- watcher ----

class Watched(network.NetworkWatcher):
    """A watcher whose one tick is observable."""

    def __init__(self):
        super().__init__("hotpass")
        self.started = []

    def _start(self, password):
        self.started.append(password)
        return True


@pytest.fixture
def watcher(monkeypatch, store):
    network.init(store)
    monkeypatch.setattr(network, "_quiet_until", 0.0, raising=False)
    network._joining.clear()
    watched = Watched()
    monkeypatch.setattr(network, "start_hotspot",
                        lambda password, prescan=True: watched._start(password))
    return watched


def test_a_fresh_device_opens_the_setup_hotspot_at_once(watcher, monkeypatch):
    """No saved network: there is nothing to wait for."""
    monkeypatch.setattr(network, "connectivity", lambda: "none")
    monkeypatch.setattr(network, "hotspot_active", lambda: False)
    monkeypatch.setattr(network, "saved_wifi_profiles", lambda: False)
    watcher._tick()
    assert watcher.started == ["hotpass"]


def test_a_configured_device_rides_out_a_brief_outage(watcher, monkeypatch):
    """A router reboot must not tear down normal networking."""
    monkeypatch.setattr(network, "connectivity", lambda: "none")
    monkeypatch.setattr(network, "hotspot_active", lambda: False)
    monkeypatch.setattr(network, "saved_wifi_profiles", lambda: True)
    for _ in range(network.NetworkWatcher.FAILS_NEEDED - 1):
        watcher._tick()
    assert watcher.started == []
    watcher._tick()
    assert watcher.started == ["hotpass"]


def test_a_recovered_network_resets_the_count(watcher, monkeypatch):
    states = iter(["none", "none", "full", "none"])
    monkeypatch.setattr(network, "connectivity", lambda: next(states))
    monkeypatch.setattr(network, "hotspot_active", lambda: False)
    monkeypatch.setattr(network, "saved_wifi_profiles", lambda: True)
    monkeypatch.setattr(network, "refresh_scan", lambda force=False: [])
    for _ in range(4):
        watcher._tick()
    assert watcher.started == []


def test_a_lan_only_device_is_left_alone(watcher, monkeypatch):
    """"limited" is a network without internet, not a device with none."""
    monkeypatch.setattr(network, "connectivity", lambda: "limited")
    monkeypatch.setattr(network, "hotspot_active", lambda: False)
    monkeypatch.setattr(network, "refresh_scan", lambda force=False: [])
    for _ in range(5):
        watcher._tick()
    assert watcher.started == []


def test_the_watcher_stays_off_the_radio_during_a_join(watcher, monkeypatch):
    """Firing here would knock the radio into AP mode and abort the join."""
    monkeypatch.setattr(network, "connectivity",
                        lambda: pytest.fail("must not probe during a join"))
    network._joining.set()
    try:
        watcher._tick()
    finally:
        network._joining.clear()
    assert watcher.started == []


def test_the_watcher_leaves_an_active_hotspot_up(watcher, monkeypatch):
    """Setup is in progress; tearing it down would strand the phone."""
    monkeypatch.setattr(network, "connectivity", lambda: "none")
    monkeypatch.setattr(network, "hotspot_active", lambda: True)
    watcher._tick()
    assert watcher.started == []


def test_the_watcher_does_nothing_without_nmcli(watcher):
    """A dev machine: every probe degrades to a no-op."""
    watcher.run()
    assert watcher.started == []


def test_a_failing_tick_does_not_kill_the_watcher(watcher, monkeypatch):
    monkeypatch.setattr(network, "connectivity",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(network, "available", lambda: True)
    watcher._stopping.set()      # one pass through the loop, then out
    watcher.run()                # must not raise


# -------------------------------------------------------------- reboot ----

def test_a_refused_reboot_is_recorded_rather_than_raised(store, monkeypatch):
    network.init(store)
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 1, stdout="", stderr="Not authorized"))
    assert network.reboot() is False
    assert network.state()["reboot_error"]


def test_a_reboot_that_works_says_so(store, monkeypatch):
    network.init(store)
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="", stderr=""))
    assert network.reboot() is True
