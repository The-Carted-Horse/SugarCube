"""captive.py — making a phone open the setup page by itself.

The redirect must fire for the connectivity probe a phone makes on joining
the hotspot, and must *not* fire for the setup page it then loads, or the
page would redirect to itself.
"""

import pytest

from glucocube import captive, network
from glucocube.config import Config


class FakeHandler:
    """Just enough handler for ``maybe_handle``."""

    def __init__(self, host="", config=None):
        self.headers = {"Host": host}
        self.sent = []
        self.server = type("Server", (), {"config": config or Config(
            admin_port=80, admin_password="pw1234")})()

    def _send(self, body, ctype, code=200, extra=None):
        self.sent.append({"body": body, "ctype": ctype, "code": code,
                          "extra": extra or {}})


@pytest.fixture
def hotspot_up(monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)


# ---------------------------------------------------------- is_own_host ----

@pytest.mark.parametrize("host", [
    "", "localhost", network.HOTSPOT_ADDR, "glucocube.local",
    "glucocube.local:8080", "192.168.1.50", "10.42.0.1:80",
])
def test_requests_addressed_to_the_device_are_left_alone(host):
    assert captive.is_own_host(host) is True


@pytest.mark.parametrize("host", ["[fe80::1]", "fe80::1"])
def test_an_ipv6_literal_host_is_not_recognised_as_our_own(host):
    """Documenting a known limitation rather than wishing it away.

    The header is split on ':' before the address is parsed, so an IPv6
    literal never survives to ``ip_address``. Harmless in the only place
    this code runs: the setup hotspot hands out IPv4 only, and it is the
    sole condition under which any of this is active.
    """
    assert captive.is_own_host(host) is False


@pytest.mark.parametrize("host", [
    "connectivitycheck.gstatic.com", "captive.apple.com",
    "www.msftconnecttest.com", "example.com:443",
])
def test_requests_addressed_to_the_internet_are_the_portal_probes(host):
    assert captive.is_own_host(host) is False


# --------------------------------------------------------------- target ----

def test_the_redirect_carries_the_password_so_no_login_is_needed():
    """A captive browser has neither a cookie nor the QR code's key."""
    config = Config(admin_port=80, admin_password="abc123")
    assert captive.target(config) == \
        f"http://{network.HOTSPOT_ADDR}/setup?key=abc123"


def test_the_redirect_omits_a_key_when_there_is_no_password():
    config = Config(admin_port=80, admin_password="")
    assert captive.target(config) == f"http://{network.HOTSPOT_ADDR}/setup"


def test_a_non_default_admin_port_is_in_the_redirect():
    config = Config(admin_port=8080, admin_password="")
    assert captive.target(config).startswith(
        f"http://{network.HOTSPOT_ADDR}:8080/")


# --------------------------------------------------------- maybe_handle ----

def test_nothing_is_intercepted_without_the_hotspot():
    handler = FakeHandler(host="connectivitycheck.gstatic.com")
    assert captive.maybe_handle(handler, "/generate_204") is False
    assert handler.sent == []


@pytest.mark.parametrize("path", sorted(captive.PROBE_PATHS))
def test_every_platforms_connectivity_probe_is_redirected(hotspot_up, path):
    handler = FakeHandler(host="whatever.example.com")
    assert captive.maybe_handle(handler, path) is True
    sent = handler.sent[0]
    assert sent["code"] == 302
    assert sent["extra"]["Location"].endswith("/setup?key=pw1234")


def test_a_request_for_a_site_on_the_internet_is_redirected(hotspot_up):
    """Anything the phone asks for lands on setup while the hotspot is up."""
    handler = FakeHandler(host="example.com")
    assert captive.maybe_handle(handler, "/anything") is True


@pytest.mark.parametrize("path", ["/setup", "/setup/wifi", "/screen.png",
                                  "/fonts/JetBrainsMono-Bold.ttf",
                                  "/api/wifi.json"])
def test_the_portal_page_and_what_it_needs_are_served_normally(hotspot_up, path):
    """Redirecting these would send the setup page to itself."""
    handler = FakeHandler(host="example.com")
    assert captive.maybe_handle(handler, path) is False


def test_a_request_addressed_to_the_device_is_served_normally(hotspot_up):
    handler = FakeHandler(host=network.HOTSPOT_ADDR)
    assert captive.maybe_handle(handler, "/settings") is False


def test_the_redirect_body_also_links_to_setup(hotspot_up):
    """For a browser that shows the page rather than following the 302."""
    handler = FakeHandler(host="example.com")
    captive.maybe_handle(handler, "/generate_204")
    body = handler.sent[0]["body"].decode()
    assert "Set up GlucoCube" in body
    assert handler.sent[0]["extra"]["Location"] in body


def test_a_head_request_gets_the_headers_without_a_body(hotspot_up):
    handler = FakeHandler(host="example.com")
    captive.maybe_handle(handler, "/connecttest.txt", body=False)
    assert handler.sent[0]["body"] == b""
    assert handler.sent[0]["code"] == 302


def test_active_follows_the_cached_hotspot_state(monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert captive.active() is True
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: False)
    assert captive.active() is False
