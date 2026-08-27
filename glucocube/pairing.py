"""The display's side of being paired by scanning it.

An unpaired display asks GlucoCore to be paired, and puts the answer on
its own screen as a QR code. Somebody scans it with a phone that is
already signed in, chooses who the display may show, and approves; this
collects the token that approval created and writes it down.

It runs whether or not anybody has the settings page open, because the
whole point is a code on the wall that somebody walks up to and scans.

Two things it must get right. The request lives ten minutes, so it is
renewed before it lapses — a QR code on a wall that quietly stopped
working is worse than no QR code. And the secret that collects the token
is never rendered anywhere: it lives in the store beside the request id,
and only the id goes on the screen.
"""

import logging
import socket
import threading
import time

from . import glucocore, network, sync

log = logging.getLogger("glucocube.pairing")

# Params key holding the request in flight. The secret is in here; nothing
# that renders a page may read it out.
STATE_KEY = "__pairing"

POLL_SECONDS = 4
# Renewed with a minute to spare, so a code somebody is walking towards
# does not lapse between the scan and the approval.
RENEW_MARGIN_SECONDS = 60
ERROR_BACKOFF_SECONDS = 30


def public_state(store) -> dict:
    """What a page or the screen may know: never the secret.

    A display that has not managed to ask yet still has something to say —
    the reason — and that is the case a page most needs to explain, so it
    is not folded into the same empty answer as "nothing going on here".
    """
    state = store.get_params(STATE_KEY)
    if not (state.get("request_id") or state.get("error")):
        return {}
    return {
        "request_id": state.get("request_id", ""),
        "approve_url": state.get("approve_url", ""),
        "expires_at": state.get("expires_at", 0),
        "error": state.get("error", ""),
    }


def clear(store) -> None:
    store.replace_params(STATE_KEY, {})


class PairingWaiter(threading.Thread):
    """Keeps a live pairing request, and collects the token it earns."""

    def __init__(self, config_path, store, on_paired, admin_port: int = 80):
        super().__init__(name="glucocore-pairing", daemon=True)
        self.config_path = config_path
        self.store = store
        self.on_paired = on_paired
        self.admin_port = admin_port
        # Not _stop: Thread has a private method by that name, and shadowing
        # it makes join() raise TypeError once the thread has finished.
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    def run(self) -> None:
        log.info("waiting to be paired; a QR code is on the screen")
        while not self._stopping.is_set():
            try:
                state = self._live_request()
                if state and self._collect(state):
                    return
            except Exception as exc:  # noqa: BLE001 - the next pass retries
                log.warning("pairing request failed: %s", exc)
                self._note_error(str(exc))
                self._stopping.wait(ERROR_BACKOFF_SECONDS)
                continue
            self._stopping.wait(POLL_SECONDS)

    # ---- the request ----

    def _live_request(self) -> dict:
        state = self.store.get_params(STATE_KEY)
        if state.get("request_id") and not self._lapsing(state):
            return state
        return self._ask()

    @staticmethod
    def _lapsing(state: dict) -> bool:
        expires = float(state.get("expires_at") or 0)
        return expires - time.time() * 1000 < RENEW_MARGIN_SECONDS * 1000

    def _ask(self) -> dict:
        # The hostname is a suggestion, not a setting: whoever approves the
        # request is shown it and can type over it. "glucocube" beats an
        # empty box on a phone.
        answer = glucocore.request_pairing(network.hardware_id(),
                                           socket.gethostname().split(".")[0])
        if not answer.get("id") or not answer.get("secret"):
            raise RuntimeError("GlucoCore did not answer with a request")
        state = {
            "request_id": answer["id"],
            "secret": answer["secret"],
            "approve_url": answer.get("approveUrl") or "",
            "expires_at": _millis(answer.get("expiresAt")),
            "error": "",
        }
        self.store.replace_params(STATE_KEY, state)
        log.info("asking to be paired: %s", state["approve_url"])
        return state

    def _note_error(self, message: str) -> None:
        # replace, not merge: set_params drops falsy values, so an error
        # cleared on the next good pass would not stick.
        state = dict(self.store.get_params(STATE_KEY))
        state["error"] = message[:200]
        self.store.replace_params(STATE_KEY, state)

    # ---- the answer ----

    def _collect(self, state: dict) -> bool:
        answer = glucocore.collect_pairing(state["request_id"],
                                           state["secret"])
        if not answer.get("approved"):
            return False
        device = answer.get("device") or {}
        token = answer.get("deviceToken") or ""
        if not token:
            raise RuntimeError("approved, but no token came with it")
        added = sync.write_pairing(self.config_path, device, token,
                                   network.hardware_id(),
                                   admin_port=self.admin_port,
                                   store=self.store)
        clear(self.store)
        log.info("paired; showing %s", ", ".join(added))
        self.on_paired()
        return True


def _millis(when) -> int:
    """An ISO timestamp as epoch milliseconds, or zero if it is not one."""
    if not when:
        return 0
    try:
        from datetime import datetime
        text = str(when).replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def start_waiter(config, config_path, store, on_paired) -> "PairingWaiter | None":
    """Wait to be paired, but only on a display that is not paired already."""
    if config.glucocore and config.glucocore.device_token:
        return None
    waiter = PairingWaiter(config_path, store, on_paired,
                           admin_port=config.admin_port)
    waiter.start()
    return waiter
