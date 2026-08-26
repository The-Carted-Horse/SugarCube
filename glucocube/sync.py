"""Apply remote GlucoCore config to local config.json."""

import json
import logging
import secrets
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("glucocube.sync")

LAST_VERSION_KEY = "__glucocore_config_version"


# What the device takes out of a remote display block. Everything else
# GlucoCore can say is not applied at all — a rotation interval means
# nothing to a screen that shows everyone at once, and this deliberately
# does not sound alarms — and `unapplied_display_keys` is what says so out
# loud, rather than leaving somebody to wonder why a setting they changed
# did nothing. Every name here must be a DisplayConfig field: config.load
# builds it with **display, so an unknown key stops the device booting.
DISPLAY_KEYS = ("timezone", "units", "low", "high", "urgent_low",
                "urgent_high", "stale_minutes", "brightness",
                "night_brightness", "night_from_hour", "night_to_hour")


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


def write_pairing(config_path, device: dict, device_token: str,
                  hardware_id: str, *, admin_port: int = 80,
                  store=None) -> list[str]:
    """Write down a pairing, however it was made, and say who it added.

    Three ways in — a code typed at the display, an account signed in to,
    a QR somebody scanned — and one place that turns the answer into
    config.json, so a display cannot end up shaped differently depending
    on which way it was paired.
    """
    from . import onboarding

    path = Path(config_path)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = {}
    remote = device.get("config") or {}
    patient_ids = [str(pid) for pid in (remote.get("patientIds") or []) if pid]
    if not patient_ids:
        raise ValueError("that pairing has nobody on it")

    users = onboarding.keep_local_users(raw.get("users") or [])
    taken = {(user.get("name") or "").casefold() for user in users}
    added = []
    for patient_id in patient_ids:
        name = unique_name(patient_label(remote, patient_id), taken)
        taken.add(name.casefold())
        added.append(name)
        users.append({
            "name": name,
            "port": None,
            "api_secret": secrets.token_hex(12),
            "source": {"type": "glucocore", "patient_id": patient_id,
                       "poll_seconds": 60},
        })

    config_mod.assign_ports(users, reserved={admin_port})
    raw["users"] = users
    # The bands and the zone arrive with the token, so they are applied now
    # rather than waiting for the first push.
    raw.setdefault("display", {}).update(
        display_from_remote(raw.get("display") or {}, remote))
    raw["glucocore"] = {
        "device_id": device.get("id") or "",
        "device_token": device_token,
        "hardware_id": hardware_id,
        "name": device.get("name") or "",
    }
    config_mod.write_atomic(raw, path)
    if store is not None:
        # Config versions count from the device this pairing just created, so
        # a number left over from an earlier one would make the first push
        # from GlucoCore look like old news and be ignored.
        store.replace_params(LAST_VERSION_KEY, {})
    log.info("Paired with GlucoCore as %r (%d patients)",
             raw["glucocore"]["name"], len(patient_ids))
    return added


def unique_name(name: str, taken) -> str:
    """A display name nothing else is using.

    Everything in the database is keyed by the name, so two people sharing
    one would read each other's readings.
    """
    name = (name or "").strip() or "Unnamed"
    if name.casefold() not in taken:
        return name
    for suffix in range(2, 100):
        candidate = f"{name} {suffix}"
        if candidate.casefold() not in taken:
            return candidate
    return f"{name} {secrets.token_hex(2)}"


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
                  if key not in DISPLAY_KEYS and value not in (None, ""))
