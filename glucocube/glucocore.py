"""GlucoCore cloud client — login, registration, config sync, heartbeat."""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("glucocube.glucocore")

# The service's own address. Nobody types this — it is not a setting, and
# a device that cannot resolve it cannot pair at all, so it is worth being
# sure of: app.glucocore.com, which this used to dial, does not exist.
GLUCOCORE_BASE = os.environ.get("GLUCOCORE_BASE", "https://glucocore.app").rstrip("/")
SESSION_HEADER = "x-tidepool-session-token"


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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()) or []
