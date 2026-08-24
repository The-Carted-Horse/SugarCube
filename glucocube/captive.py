"""Captive-portal answers, so joining the setup hotspot opens setup.

A phone that joins a Wi-Fi network immediately fetches a known URL to
check whether the network really reaches the internet. Answering that
check with a redirect instead of the expected reply is what makes the
phone pop its "Sign in to network" sheet. Paired with the wildcard DNS
this project installs for NetworkManager's shared mode, that turns
joining `GlucoCube-Setup` into the setup page opening by itself —
replacing a second QR code scanned off the device's screen.

Everything here is inert unless the setup hotspot is actually up.
"""

import ipaddress
import logging

from . import network
from .config import admin_url

log = logging.getLogger("glucocube.captive")

# What each platform fetches to decide whether it is behind a portal.
PROBE_PATHS = frozenset({
    "/generate_204", "/gen_204",                     # Android, Chrome OS
    "/mobile/status.php",                            # older Android
    "/hotspot-detect.html", "/hotspotdetect.html",   # iOS, macOS
    "/library/test/success.html",                    # iOS, older
    "/success.txt", "/canonical.html",               # Firefox, NetworkManager
    "/ncsi.txt", "/connecttest.txt", "/redirect",    # Windows
    "/nmcheck.gnome.org",                            # GNOME
})

# Paths that must answer normally even from a captive browser, or the
# portal page itself would redirect to itself.
ALWAYS_SERVE = ("/setup", "/screen.png", "/fonts/", "/api/")


def active() -> bool:
    return network.hotspot_active_cached()


def is_own_host(header: str) -> bool:
    """Is this request addressed to the device rather than the internet?"""
    host = (header or "").split(":")[0].strip().strip("[]").lower()
    if host in ("", "localhost", network.HOTSPOT_ADDR):
        return True
    if host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def target(config) -> str:
    path = "/setup"
    if config.admin_password:
        # A captive browser carries neither a cookie nor the QR link, so
        # the key has to travel in the URL.
        path += f"?key={config.admin_password}"
    return admin_url(network.HOTSPOT_ADDR, config.admin_port, path)


def maybe_handle(handler, path: str, body: bool = True) -> bool:
    """Answer this request as a portal redirect, or leave it alone.

    Must be called before the authorization check: a 401 with a
    WWW-Authenticate header makes a phone's captive browser show a
    username box instead of the setup page — exactly the dead end the
    portal exists to remove.
    """
    if not active():
        return False
    if path.startswith(ALWAYS_SERVE):
        return False
    if path not in PROBE_PATHS and is_own_host(handler.headers.get("Host", "")):
        return False
    where = target(handler.server.config)
    payload = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="0;url={where}">'
        "<title>Set up GlucoCube</title></head><body>"
        f'<p><a href="{where}">Set up GlucoCube</a></p></body></html>'
    ).encode() if body else b""
    handler._send(payload, "text/html; charset=utf-8", 302, {"Location": where})
    return True
