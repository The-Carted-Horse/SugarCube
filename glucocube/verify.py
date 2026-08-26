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

from . import glucocore, nspull, tidepool

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


def _network_message(exc: Exception, secret: str, what: str, *,
                     address: str = "") -> Result:
    """Why a check failed, in words the person reading them can act on.

    `address` is the address the app dialled when the person did not type
    it — GlucoCore's, which is built into the app. Telling someone to
    check the spelling of an address they were never shown sends them
    hunting for a typo they cannot have made; naming it, and saying it is
    not theirs to correct, is the difference between a dead end and a
    fault report somebody can act on.
    """
    detail = _scrub(f"{type(exc).__name__}: {exc}", secret)
    if isinstance(exc, urllib.error.HTTPError):
        if 300 <= exc.code < 400:
            # The client follows a permanent redirect that stays on the
            # service. One that reaches here went somewhere else, or never
            # settled — neither is something a person can fix by retyping.
            where = exc.headers.get("Location") if exc.headers else ""
            return Result(False, f"{what} is redirecting this device"
                                 + (f" to {where}" if where else "")
                                 + ", and it will not follow that. The"
                                   " address it is set to use may be wrong.",
                          detail)
        if exc.code in (401, 403):
            return Result(False, f"{what} rejected those credentials.", detail)
        if exc.code == 404:
            if address:
                return Result(False, f"{address} answered, but not where "
                                     f"{what} was expected — the service may "
                                     "have moved.", detail)
            return Result(False, "That address answered, but it is not a "
                                 "Nightscout site.", detail)
        return Result(False, f"{what} answered with an error ({exc.code}).",
                      detail)
    if isinstance(exc, ssl.SSLError):
        return Result(False, "The site's HTTPS certificate could not be "
                             "verified.", detail)
    if isinstance(exc, (urllib.error.URLError, socket.gaierror, OSError)):
        if address:
            return Result(False, f"Could not reach {what} at {address}. "
                                 "Check this device is online — the address "
                                 "is built in, so there is nothing to correct "
                                 "here.", detail)
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


def glucocore_session(email: str, password: str,
                      timeout: float = DEFAULT_TIMEOUT) -> tuple[Result, dict]:
    """Sign in, and hand back the session token and the patients on it.

    Pairing this way needs the token it just proved works. Signing in
    twice — once to check, once to keep — would trip the throttle above
    and, worse, could succeed the first time and fail the second, so the
    check and the thing being checked are one call.
    """
    email = (email or "").strip()
    if not email or not password:
        return Result(False, "Enter your GlucoCore email and password."), {}
    session: dict = {}

    def run() -> Result:
        try:
            token, userid = glucocore.login(email, password,
                                            timeout=timeout * 0.8)
            patients = glucocore.list_patients(token, userid,
                                               timeout=timeout * 0.5)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result = _network_message(exc, password, "GlucoCore",
                                      address=glucocore.GLUCOCORE_BASE)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
                return Result(False, "That email or password did not work.",
                              result.detail)
            return result
        session.update(token=token, userid=userid, patients=list(patients))
        count = len(patients)
        if count == 0:
            return Result(True, "Signed in, but no patients are visible yet.")
        return Result(True, f"Signed in — {count} "
                            f"patient{'s' if count != 1 else ''} available.")

    result = _guarded(f"glucocore:{email}", run, timeout)
    # _bounded abandons a worker that overran, so a late success must not
    # leak out as a session nobody is waiting for any more.
    return (result, session) if result.ok else (result, {})


def glucocore_register(token: str, name: str, hardware_id: str,
                       patient_ids: list, display: dict | None = None,
                       timeout: float = DEFAULT_TIMEOUT) -> tuple[Result, dict]:
    """Create this display in GlucoCore, and hand back what it said.

    Shaped like `glucocore_claim` on purpose: both ways of pairing end
    with a device, its config and a token, so the page that writes them
    down does not need to know which way it was.
    """
    if not patient_ids:
        return Result(False, "Choose at least one person to show."), {}
    registered: dict = {}
    config = {"patientIds": list(patient_ids), "display": dict(display or {}),
              "perPatient": {}}

    def run() -> Result:
        try:
            answer = glucocore.register_device(
                token, name, hardware_id, list(patient_ids), config=config,
                timeout=timeout * 0.9)
        except Exception as exc:  # noqa: BLE001
            return _network_message(exc, token, "GlucoCore",
                                    address=glucocore.GLUCOCORE_BASE)
        if not answer.get("deviceToken"):
            return Result(False, "GlucoCore accepted this display but sent "
                                 "no token for it.")
        registered.update(answer)
        return Result(True, "Paired.")

    result = _guarded(f"glucocore-register:{hardware_id}", run, timeout)
    return (result, registered) if result.ok else (result, {})


CODE_LENGTH = 6


def glucocore_claim(code: str, hardware_id: str, name: str = "",
                    timeout: float = DEFAULT_TIMEOUT) -> tuple[Result, dict]:
    """Redeem a pairing code, and hand back what the service gave us.

    GlucoCore answers a bad code, an expired one, one already claimed and
    one refused for going too fast all identically — deliberately, so an
    endpoint anybody can reach cannot be used to tell them apart. This
    repeats that rather than guessing at which happened: what the reader
    needs is the one instruction that covers every case, which is to mint
    a fresh code.
    """
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != CODE_LENGTH:
        return Result(False, f"A pairing code is {CODE_LENGTH} digits."), {}
    if not hardware_id:
        return Result(False, "This device has no hardware id to pair with."), {}
    claimed: dict = {}

    def run() -> Result:
        try:
            answer = glucocore.claim(code, hardware_id, name,
                                     timeout=timeout * 0.9)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            result = _network_message(exc, code, "GlucoCore",
                                      address=glucocore.GLUCOCORE_BASE)
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 400:
                return Result(False, "That code was not accepted. A code "
                                     "lasts ten minutes and works once — "
                                     "make a new one in GlucoCore and enter "
                                     "it here.", result.detail)
            return result
        if not answer.get("deviceToken"):
            return Result(False, "GlucoCore accepted the code but sent no "
                                 "token for this display.")
        claimed.update(answer)
        return Result(True, "Paired.")

    # Throttled on the code: a display retrying a code it has already spent
    # must not spend the caller's rate limit on the service, which answers
    # a refusal there exactly as it answers a wrong code.
    result = _guarded(f"glucocore-claim:{code}", run, timeout)
    return (result, claimed) if result.ok else (result, {})


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
