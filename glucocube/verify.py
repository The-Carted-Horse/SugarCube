"""One-shot credential checks for the setup wizard and the settings page.

Built deliberately on ``tidepool.login()`` and ``nspull.probe()`` — the
same calls the pollers make. A green tick here has to mean the poller
will work, and that is only true if both take one code path.

Everything is bounded. These run inside an HTTP handler on a Raspberry Pi
talking to somebody else's server, so a slow remote must never hold a
phone's page open, and a person jabbing "Test connection" must never turn
into a burst of failed logins against their account.
"""

import logging
import socket
import ssl
import threading
import time
import urllib.error
from dataclasses import dataclass

from . import nspull, tidepool

log = logging.getLogger("glucocube.verify")

DEFAULT_TIMEOUT = 10.0
MIN_INTERVAL = 3.0          # per identity; Tidepool locks accounts out
MAX_CONCURRENT = 2

_slots = threading.Semaphore(MAX_CONCURRENT)
_last_attempt: dict[str, float] = {}
_last_lock = threading.Lock()


@dataclass(frozen=True)
class Result:
    ok: bool
    message: str            # shown to the user; never contains the secret
    detail: str = ""        # technical, tucked behind a disclosure

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "detail": self.detail}


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _throttle(identity: str) -> float:
    """Seconds the caller must wait before retrying this identity."""
    with _last_lock:
        last = _last_attempt.get(identity, 0.0)
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            return wait
        _last_attempt[identity] = time.monotonic()
        return 0.0


def _bounded(fn, timeout: float) -> Result:
    """Hard wall-clock bound on a blocking network call.

    urlopen's timeout bounds each socket operation, not the whole
    exchange — a redirect chain or a slow drip can outlast it. The
    abandoned worker is a daemon thread holding one socket and dies with
    the process.
    """
    box: list[Result] = []
    worker = threading.Thread(target=lambda: box.append(fn()),
                              name="verify", daemon=True)
    worker.start()
    worker.join(timeout)
    if not box:
        return Result(False, f"No answer within {timeout:.0f} seconds — the "
                             "service may be slow, or unreachable from here.")
    return box[0]


def _guarded(identity: str, fn, timeout: float) -> Result:
    wait = _throttle(identity)
    if wait > 0:
        return Result(False, f"Just tried that — wait {wait:.0f}s and "
                             "try again.")
    if not _slots.acquire(blocking=False):
        return Result(False, "Another test is still running — try again in "
                             "a moment.")
    try:
        return _bounded(fn, timeout)
    finally:
        _slots.release()


def _network_message(exc: Exception, secret: str, what: str) -> Result:
    detail = _scrub(f"{type(exc).__name__}: {exc}", secret)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return Result(False, f"{what} rejected those credentials.", detail)
        if exc.code == 404:
            return Result(False, "That address answered, but it is not a "
                                 "Nightscout site.", detail)
        return Result(False, f"{what} answered with an error ({exc.code}).",
                      detail)
    if isinstance(exc, ssl.SSLError):
        return Result(False, "The site's HTTPS certificate could not be "
                             "verified.", detail)
    if isinstance(exc, (urllib.error.URLError, socket.gaierror, OSError)):
        return Result(False, "Could not reach that address — check the "
                             "spelling, and that this device is online.",
                      detail)
    return Result(False, "Could not check that: see the detail below.", detail)


def tidepool_login(email: str, password: str,
                   timeout: float = DEFAULT_TIMEOUT) -> Result:
    email = (email or "").strip()
    if not email or not password:
        return Result(False, "Enter the Tidepool email and password first.")

    def run() -> Result:
        try:
            token, userid = tidepool.login(email, password, timeout * 0.8)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return _network_message(exc, password, "Tidepool")
        try:
            docs = tidepool.latest_cbg(token, userid, timeout * 0.8)
        except Exception:  # noqa: BLE001 - login worked, that is the answer
            return Result(True, "Signed in to Tidepool.")
        if not docs:
            return Result(True, "Signed in, but Tidepool has no glucose "
                                "readings yet for this account.")
        return Result(True, "Signed in to Tidepool, and readings are there.")

    return _guarded(f"tidepool:{email}", run, timeout)


def nightscout_site(url: str, key: str,
                    timeout: float = DEFAULT_TIMEOUT) -> Result:
    url = (url or "").strip()
    if not url:
        return Result(False, "Enter the Nightscout address first.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def run() -> Result:
        try:
            mode, entries = nspull.probe(url, key, timeout * 0.8)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result = _network_message(exc, key, "The site")
            if (isinstance(exc, urllib.error.HTTPError)
                    and exc.code in (401, 403)):
                return Result(False, "The site rejected that key. Check it is "
                                     "the API secret or an access token, not "
                                     "the site's own password.", result.detail)
            return result
        style = {"sha1": "API secret", "token": "access token",
                 "raw": "API secret", "none": "no key"}.get(mode, mode)
        if not entries:
            return Result(True, f"Connected ({style} accepted), but the site "
                                "has no recent readings.")
        return Result(True, f"Connected — {style} accepted and readings are "
                            "there.")

    return _guarded(f"nightscout:{url}", run, timeout)


def source(config: dict, timeout: float = DEFAULT_TIMEOUT) -> Result:
    """Check whichever kind of source this is."""
    kind = (config or {}).get("type")
    if kind == "tidepool":
        return tidepool_login(config.get("email", ""),
                              config.get("password", ""), timeout)
    if kind == "nightscout":
        return nightscout_site(config.get("url", ""),
                               config.get("api_secret") or config.get("token")
                               or "", timeout)
    return Result(True, "Nothing to test — this person's device uploads to "
                        "GlucoCube directly.")
