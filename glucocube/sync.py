"""Apply remote GlucoCore config to local config.json."""

import json
import logging
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("glucocube.sync")

LAST_VERSION_KEY = "__glucocore_config_version"


def apply_remote_config(
    config_path: str | Path,
    remote: dict,
    version: int,
    *,
    patient_names: dict[str, str] | None = None,
) -> config_mod.Config:
    """Merge a GlucoCore config push into config.json. Returns the new Config."""
    path = Path(config_path)
    raw = json.loads(path.read_text())
    display = dict(raw.get("display") or {})
    remote_display = remote.get("display") or {}

    for key in ("timezone", "units", "low", "high", "urgent_low", "urgent_high", "stale_minutes"):
        if key in remote_display and remote_display[key] not in (None, ""):
            display[key] = remote_display[key]

    patient_ids = list(remote.get("patientIds") or [])
    per_patient = remote.get("perPatient") or {}
    names = patient_names or {}
    glucocore_block = dict(raw.get("glucocore") or {})

    users = []
    import secrets
    for patient_id in patient_ids:
        name = names.get(patient_id) or patient_id
        thresholds = (per_patient.get(patient_id) or {}).get("thresholds") or {}
        users.append({
            "name": name,
            "port": None,
            "api_secret": secrets.token_hex(12),
            "source": {
                "type": "glucocore",
                "patient_id": patient_id,
                "poll_seconds": 60,
            },
            **({"thresholds": thresholds} if thresholds else {}),
        })

    if not users:
        raise ValueError("remote config has no patients")

    config_mod.assign_ports(users, reserved={raw.get("admin", {}).get("port", 80)})
    raw["users"] = users
    raw["display"] = display
    raw["glucocore"] = glucocore_block

    config = config_mod.write_atomic(raw, path)
    if display.get("timezone"):
        config_mod.apply_timezone(str(display["timezone"]))
    log.info("Applied GlucoCore config version %s (%d patients)", version, len(users))
    return config
