"""Apply remote GlucoCore config to local config.json."""

import json
import logging
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("glucocube.sync")

LAST_VERSION_KEY = "__glucocore_config_version"


# What the device reads out of a remote display block. Everything else
# GlucoCore can say — the brightness pair and its night window, rotation,
# the alert flags — is either applied elsewhere or not applied at all;
# `unapplied_display_keys` is what says which, out loud, rather than
# leaving somebody to wonder why a setting they changed did nothing.
DISPLAY_KEYS = ("timezone", "units", "low", "high", "urgent_low",
                "urgent_high", "stale_minutes")


def display_from_remote(display: dict, remote: dict) -> dict:
    """The local display settings a remote config replaces.

    One rule, used by the pairing that arrives with a config and by every
    push after it, so a display cannot end up reading one of them
    differently from the other.
    """
    merged = dict(display)
    remote_display = remote.get("display") or {}
    for key in DISPLAY_KEYS:
        if key in remote_display and remote_display[key] not in (None, ""):
            merged[key] = remote_display[key]
    return merged


def patient_label(remote: dict, patient_id: str) -> str:
    """What GlucoCore calls this person on this display."""
    per = (remote.get("perPatient") or {}).get(patient_id) or {}
    return str(per.get("label") or "").strip() or patient_id


def apply_remote_config(
    config_path: str | Path,
    remote: dict,
    version: int,
    *,
    store=None,
) -> config_mod.Config:
    """Merge a GlucoCore config push into config.json. Returns the new Config.

    `store`, when given, is what carries a person's readings across a
    rename: every table is keyed by the display name, so a label changed
    in GlucoCore would otherwise read on the wall as somebody who has
    never had a reading.
    """
    path = Path(config_path)
    raw = json.loads(path.read_text())
    display = display_from_remote(raw.get("display") or {}, remote)

    patient_ids = list(remote.get("patientIds") or [])
    per_patient = remote.get("perPatient") or {}
    glucocore_block = dict(raw.get("glucocore") or {})

    # People fed by an uploader, Tidepool or a Nightscout site are none of
    # GlucoCore's business: a config push says who to pull from GlucoCore,
    # not who is allowed on the display. Wiping them here is what made
    # pairing an all-or-nothing choice.
    existing = list(raw.get("users") or [])
    users = [u for u in existing
             if (u.get("source") or {}).get("type") != "glucocore"]
    by_patient = {(u.get("source") or {}).get("patient_id"): u
                  for u in existing
                  if (u.get("source") or {}).get("type") == "glucocore"}

    import secrets
    for patient_id in patient_ids:
        # A person's name on the display is GlucoCore's `label` — "what to
        # call them on screen, when the account name is not the household
        # name". Without one there is only the patient id, which is not a
        # name anybody would choose to see on a wall.
        name = patient_label(remote, patient_id)
        thresholds = (per_patient.get(patient_id) or {}).get("thresholds") or {}
        # Ports and push secrets stay put across pushes. They are stable
        # identities on the network, not values to reroll every time
        # GlucoCore changes a threshold.
        prior = by_patient.get(patient_id) or {}
        users.append({
            "name": name,
            "port": prior.get("port"),
            "api_secret": prior.get("api_secret") or secrets.token_hex(12),
            "source": {
                "type": "glucocore",
                "patient_id": patient_id,
                "poll_seconds": 60,
            },
            **({"thresholds": thresholds} if thresholds else {}),
        })

    if not patient_ids:
        raise ValueError("remote config has no patients")

    config_mod.assign_ports(users, reserved={raw.get("admin", {}).get("port", 80)})
    if store is not None:
        for user in users:
            patient_id = (user.get("source") or {}).get("patient_id")
            previous = (by_patient.get(patient_id) or {}).get("name")
            if previous and previous != user["name"]:
                log.info("GlucoCore renamed %r to %r", previous, user["name"])
                store.rename_user(previous, user["name"])
    raw["users"] = users
    raw["display"] = display
    raw["glucocore"] = glucocore_block

    config = config_mod.write_atomic(raw, path)
    if display.get("timezone"):
        config_mod.apply_timezone(str(display["timezone"]))
    unapplied = unapplied_display_keys(remote)
    if unapplied:
        # Said once per push, at info, because the alternative is somebody
        # changing a setting in GlucoCore and watching the wall ignore it
        # with nothing anywhere to say why.
        log.info("GlucoCore config %s carries settings this display does not "
                 "apply: %s", version, ", ".join(unapplied))
    log.info("Applied GlucoCore config version %s (%d patients)", version, len(users))
    return config


def unapplied_display_keys(remote: dict) -> list[str]:
    """Settings GlucoCore sent that this display does nothing with.

    Not a warning about a broken device — a display that shows everyone at
    once has no use for a rotation interval, and this one deliberately does
    not sound alarms. It is here so that "I changed it and nothing
    happened" has an answer in the log.
    """
    remote_display = remote.get("display") or {}
    return sorted(key for key, value in remote_display.items()
                  if key not in DISPLAY_KEYS
                  and key not in APPLIED_ELSEWHERE
                  and value not in (None, ""))


# Read by the display rather than written into config.json's display block.
APPLIED_ELSEWHERE = frozenset({"brightness", "night_brightness",
                               "night_from_hour", "night_to_hour"})
