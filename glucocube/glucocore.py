"""GlucoCore cloud client — login, registration, config sync, heartbeat."""

import base64
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


def login(email: str, password: str, timeout: float = 30) -> tuple[str, str]:
    creds = base64.b64encode(f"{email}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{GLUCOCORE_BASE}/auth/login",
        method="POST",
        headers={"Authorization": f"Basic {creds}"},
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        token = resp.headers.get(SESSION_HEADER)
        userid = (json.loads(resp.read()) or {}).get("userid")
    if not token or not userid:
        raise RuntimeError("GlucoCore login gave no session token/userid")
    return token, userid


def signup(email: str, password: str, name: str = "", timeout: float = 30) -> dict:
    return _request(
        "POST",
        "/v1/signup",
        body={"email": email, "password": password, "confirmation": password, "name": name},
        timeout=timeout,
    )


def list_patients(token: str, user_id: str, timeout: float = 30) -> list[dict]:
    payload = _request("GET", f"/v1/users/{user_id}/accessible_patients",
                       token=token, timeout=timeout)
    return list(payload.get("patients") or [])


def register_device(token: str, name: str, hardware_id: str,
                    patient_ids: list[str], config: dict | None = None,
                    timeout: float = 60) -> dict:
    body = {"name": name, "hardwareId": hardware_id, "patientIds": patient_ids}
    if config:
        body["config"] = config
    return _request("POST", "/v1/sugar_cubes", token=token, body=body, timeout=timeout)


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
