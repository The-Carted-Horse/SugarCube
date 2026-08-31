"""The display's side of GlucoCore's command queue.

Somebody standing in front of a wall display cannot do much to it — there
is no keyboard, and the settings page needs a phone and the password.
GlucoCore's devices screen offers five buttons instead, and this is what
answers them.

Collecting and acknowledging are two separate calls, which is what makes
"the display took the instruction and was never heard from again" a state
the devices screen can show rather than a silence. So every command is
acknowledged — the failures too, with the reason, because a command that
failed for a knowable reason is worth more on that screen than one that
merely never came back.

Every command GlucoCore can send is idempotent or harmless, because a
queue with a delivery step and an acknowledgement step will occasionally
deliver twice. Nothing here destroys anything that cannot come back: the
readings a cleared cache drops are re-fetched on the next poll.
"""

import logging

from . import glucocore

log = logging.getLogger("glucocube.commands")

# What this display does with each. A name absent from the actions it is
# given is acknowledged as not-done rather than dropped: a newer GlucoCore
# may know commands this version does not, and the devices screen should
# say so instead of showing an instruction that hangs.
KNOWN = ("identify", "restart", "refresh", "clear_cache", "check_update")


def run_pending(device_token: str, actions: dict) -> int:
    """Collect what is queued, do it, and say what happened. Returns the count.

    Never raises: this runs inside a background loop whose next pass is
    more useful than a traceback.
    """
    try:
        commands = glucocore.list_commands(device_token)
    except Exception as exc:  # noqa: BLE001 - the next pass tries again
        log.debug("could not collect commands: %s", exc)
        return 0

    for command in commands:
        command_id = str(command.get("id") or "")
        name = str(command.get("command") or "")
        if not command_id:
            continue
        ok, detail = _run_one(name, actions)
        log.info("command %s: %s", name, detail or ("done" if ok else "failed"))
        try:
            glucocore.ack_command(device_token, command_id, ok, detail)
        except Exception as exc:  # noqa: BLE001
            # The command ran; only the receipt was lost. Re-delivery is
            # what the queue does about that, and every command is safe to
            # run twice by design.
            log.warning("could not acknowledge %s: %s", name, exc)
    return len(commands)


def _run_one(name: str, actions: dict) -> tuple[bool, str]:
    action = actions.get(name)
    if action is None:
        if name in KNOWN:
            return False, "this display does not do that"
        return False, f"unknown command {name!r}"
    try:
        detail = action()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return False, f"{type(exc).__name__}: {exc}"[:300]
    return True, str(detail or "")[:300]
