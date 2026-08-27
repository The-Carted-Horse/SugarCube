"""GlucoCore cloud client — login, registration, config sync, heartbeat."""

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("glucocube.glucocore")

# The service's own address. Nobody types this — it is not a setting, and
# a device that cannot resolve it cannot pair at all, so it is worth being
# sure of: app.glucocore.com, which this once dialled, does not exist, and
# the apex answers every POST with a 308 to the name below. _open follows
# that redirect anyway; being configured with the canonical name saves
# every call a round trip.
GLUCOCORE_BASE = os.environ.get("GLUCOCORE_BASE",
                                "https://www.glucocore.app").rstrip("/")
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


def login(email: str, password: str, timeout: float = 30) -> tuple[str, str]:
    """Sign in as the account holder. The other way to pair a display.

    A code is the safer path — it leaves no account credential anywhere
    near a device on a wall — but it is also six digits read off one
    screen and typed into another, and somebody standing at the display
    with their own password is entitled to just use it.
    """
    creds = base64.b64encode(f"{email}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{GLUCOCORE_BASE}/auth/login",
        method="POST",
        headers={"Authorization": f"Basic {creds}"},
        data=b"",
    )
    with _open(req, timeout) as resp:
        token = resp.headers.get(SESSION_HEADER)
        userid = (json.loads(resp.read()) or {}).get("userid")
    if not token or not userid:
        raise RuntimeError("GlucoCore login gave no session token/userid")
    return token, userid


def list_patients(token: str, user_id: str, timeout: float = 30) -> list[dict]:
    payload = _request("GET", f"/v1/users/{user_id}/accessible_patients",
                       token=token, timeout=timeout)
    return list(payload.get("patients") or [])


def register_device(token: str, name: str, hardware_id: str,
                    patient_ids: list[str], config: dict | None = None,
                    timeout: float = 60) -> dict:
    """Create this display in GlucoCore, as the signed-in account holder.

    Answers exactly as a claim does — the device, its config and a token
    scoped to those people — so both ways of pairing end in one place.
    """
    body = {"name": name, "hardwareId": hardware_id, "patientIds": patient_ids}
    if config:
        body["config"] = config
    return _request("POST", "/v1/sugar_cubes", token=token, body=body,
                    timeout=timeout)


def request_pairing(hardware_id: str, name: str = "",
                    timeout: float = 30) -> dict:
    """Ask to be paired, with nothing to authenticate the asking.

    Answers with the request id — which is what goes on this display's own
    screen as a QR code — and a secret, which does not. Whoever scans the
    code approves it from an account they are already signed in to, and
    `collect_pairing` below is how the token gets here.
    """
    body: dict = {"hardwareId": hardware_id}
    if name:
        body["name"] = name
    return _request("POST", "/v1/sugar_cubes/requests", body=body,
                    timeout=timeout)


def collect_pairing(request_id: str, secret: str,
                    timeout: float = 30) -> dict:
    """Collect the token, if the request has been approved yet.

    A POST because the secret travels in the body: in a query string it
    would be in access logs and browser history, and it is worth a device
    token. Answers `{"approved": false}` for a request nobody has approved,
    one that does not exist, and a wrong secret alike.
    """
    return _request("POST", f"/v1/sugar_cubes/requests/{request_id}/token",
                    body={"secret": secret}, timeout=timeout)


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


def fetch_wallpaper(device_token: str, wallpaper_id: str, etag: str = "",
                    timeout: float = 60) -> tuple[bytes | None, str]:
    """The bytes behind one background, or None if the cached copy still fits.

    Not through _request: that decodes JSON, and this is a JPEG. The same
    shape fetch_patient_data uses, for the same reason.

    A background never changes under its id — a different picture is a
    different id — so the ETag is exact rather than advisory, and sending
    it back is what stops a display re-pulling two megabytes every time a
    threshold moves. Answers (None, etag) on a 304, which is the whole
    point of asking.
    """
    headers = {SESSION_HEADER: device_token, "Accept": "image/*"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(
        f"{GLUCOCORE_BASE}/v1/sugar_cubes/me/wallpapers/{wallpaper_id}",
        headers=headers,
    )
    try:
        with _open(req, timeout) as resp:
            return resp.read(), resp.headers.get("ETag") or etag
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            exc.close()
            return None, etag
        raise


def fetch_patient_data(device_token: str, patient_id: str,
                       start_date: str, timeout: float = 60) -> list[dict]:
    path = (
        f"/data/{patient_id}?type=cbg,bolus,food,dosingDecision&startDate={start_date}"
    )
    headers = {SESSION_HEADER: device_token, "Accept": "application/json"}
    req = urllib.request.Request(f"{GLUCOCORE_BASE}{path}", headers=headers)
    with _open(req, timeout) as resp:
        return json.loads(resp.read()) or []
