"""GlucoCore cloud client — login, registration, config sync, heartbeat."""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("glucocube.glucocore")

# The service's own address. Nobody types this — it is not a setting, and
# a device that cannot resolve it cannot pair at all, so it is worth being
# sure of: app.glucocore.com, which this used to dial, does not exist.
GLUCOCORE_BASE = os.environ.get("GLUCOCORE_BASE", "https://glucocore.app").rstrip("/")
SESSION_HEADER = "x-tidepool-session-token"


MAX_REDIRECTS = 3


def _same_site(before: str, after: str) -> bool:
    """Whether a redirect stays somewhere this request's headers may go.

    Every call here carries a session or device token in a header, so
    following a redirect off the service and taking the token along would
    hand it to whoever the Location pointed at. Same host, or the same
    host with www in front of it or taken off — which is the redirect
    this exists for — and never off HTTPS onto plain HTTP.
    """
    old, new = urllib.parse.urlsplit(before), urllib.parse.urlsplit(after)
    if not new.scheme or not new.netloc:
        return False
    if old.scheme == "https" and new.scheme != "https":
        return False
    bare = (old.hostname or "").removeprefix("www.")
    return (new.hostname or "").removeprefix("www.") == bare


def _open(req, timeout: float):
    """urlopen, but a POST follows a permanent redirect like a GET does.

    urllib refuses this by design: `redirect_request` raises for anything
    that is not GET or HEAD on a 307 or 308, because following one means
    re-sending the body. That is a reasonable default for a browser-shaped
    library and useless here — a service behind apex-to-www answers every
    POST with a 308, and every one of them surfaced as "GlucoCore answered
    with an error (308)".
    """
    for _ in range(MAX_REDIRECTS):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # A GET was already followed inside urlopen; only a POST or
            # PUT on 307/308 gets this far.
            if exc.code not in (307, 308):
                raise
            target = urllib.parse.urljoin(req.full_url,
                                          exc.headers.get("Location") or "")
            if not _same_site(req.full_url, target):
                log.warning("refusing to follow a %s from %s off the service",
                            exc.code, req.full_url)
                raise
            exc.close()
            log.info("%s redirects to %s — following it, and %s should be "
                     "the address this device is configured with",
                     req.full_url, target, urllib.parse.urlsplit(target).netloc)
            req = urllib.request.Request(
                target, method=req.get_method(), data=req.data,
                headers=dict(req.header_items()),
            )
    raise urllib.error.URLError(f"more than {MAX_REDIRECTS} redirects")


def _request(method: str, path: str, *, token: str | None = None,
             body: dict | None = None, timeout: float = 60) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers[SESSION_HEADER] = token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GLUCOCORE_BASE}{path}", method=method, headers=headers, data=data,
    )
    with _open(req, timeout) as resp:
        raw = resp.read()
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def claim(code: str, hardware_id: str, name: str = "",
          timeout: float = 60) -> dict:
    """Redeem a pairing code for this display's own token.

    The account mints a six-digit code in GlucoCore and reads it out to
    the display. This is the only call on that face with nothing to
    authenticate with, which is the point of it: the alternative is a Pi
    on a shelf that has been told somebody's password.

    The answer carries the device — its config included, so who to show
    and their ranges arrive with the token — and the token itself.
    """
    body: dict = {"code": code, "hardwareId": hardware_id}
    if name:
        body["name"] = name
    return _request("POST", "/v1/sugar_cubes/claim", body=body, timeout=timeout)


def get_config(device_token: str, timeout: float = 30) -> dict:
    return _request("GET", "/v1/sugar_cubes/me/config", token=device_token, timeout=timeout)


def wait_config(device_token: str, since_version: int, timeout: int = 55) -> dict:
    path = (
        f"/v1/sugar_cubes/me/config/wait?since_version={since_version}&timeout={timeout}"
    )
    return _request("GET", path, token=device_token, timeout=timeout + 10)


def get_realtime_token(device_token: str, timeout: float = 30) -> dict:
    return _request("POST", "/v1/sugar_cubes/me/realtime_token",
                    token=device_token, timeout=timeout)


def list_commands(device_token: str, timeout: float = 30) -> list[dict]:
    """Collect what has been queued for this display, marking it delivered.

    Collecting and acknowledging are two steps on purpose: "the display
    took the instruction and was never heard from again" is then a state
    somebody can see on the devices screen, rather than a silence.
    """
    payload = _request("GET", "/v1/sugar_cubes/me/commands",
                       token=device_token, timeout=timeout)
    return list(payload.get("commands") or [])


def ack_command(device_token: str, command_id: str, ok: bool = True,
                detail: str = "", timeout: float = 30) -> None:
    body: dict = {"id": command_id, "ok": ok}
    if detail:
        body["detail"] = detail[:300]
    _request("POST", "/v1/sugar_cubes/me/commands", token=device_token,
             body=body, timeout=timeout)


def heartbeat(device_token: str, state: dict, timeout: float = 30) -> None:
    _request("POST", "/v1/sugar_cubes/me/heartbeat", token=device_token,
             body=state, timeout=timeout)


def fetch_patient_data(device_token: str, patient_id: str,
                       start_date: str, timeout: float = 60) -> list[dict]:
    path = (
        f"/data/{patient_id}?type=cbg,bolus,food,dosingDecision&startDate={start_date}"
    )
    headers = {SESSION_HEADER: device_token, "Accept": "application/json"}
    req = urllib.request.Request(f"{GLUCOCORE_BASE}{path}", headers=headers)
    with _open(req, timeout) as resp:
        return json.loads(resp.read()) or []
