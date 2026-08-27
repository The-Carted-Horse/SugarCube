"""Pull-based data sources: shared poller machinery and dispatch.

Users whose device pushes Nightscout payloads at us need nothing here.
Users on pumps/services we must poll configure a "source" in config.json;
start_pollers() spawns the right poller thread per user.
"""

import logging
import socket
import threading
import urllib.error

from . import synclog
from .store import Store

log = logging.getLogger("glucocube.sources")

ERROR_BACKOFF_SECONDS = 300

# What separates "what went wrong" from "when it will try again" in a sync
# log line. Split on it rather than re-deriving the phrase.
RETRY_SUFFIX = " \u00b7 retry in "


def fault_phrase(exc: Exception) -> str:
    """A failure in words, for the sync log and the person's own page.

    What used to land there was `poll failed: HTTP Error 401: Unauthorized
    (retry in 300s)` — a line that answers "did it break?" and nothing
    else. The full exception and the backoff are still in the service log,
    where the person reading them wants them.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return f"Login rejected ({exc.code})"
        if exc.code == 404:
            return "That address answered, but not with readings (404)"
        if exc.code == 429:
            return "The source asked us to slow down (429)"
        return f"The source answered with an error ({exc.code})"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "The source did not answer in time"
    if isinstance(exc, (urllib.error.URLError, socket.gaierror, OSError)):
        return "Could not reach the source"
    if isinstance(exc, ValueError):
        return "The source sent something we could not read"
    # Anything unanticipated says whatever it came with: a phrase invented
    # here would replace the only information the failure carried.
    return str(exc).strip() or "Could not read from the source"


class BasePoller(threading.Thread):
    """Poll loop with error backoff; subclasses implement _poll_once()."""

    def __init__(self, kind: str, user: str, poll_seconds: int, store: Store):
        super().__init__(name=f"{kind}-{user}", daemon=True)
        self.kind = kind
        self.user = user
        self.poll_seconds = max(30, int(poll_seconds))
        self.store = store
        # Not _stop: Thread has a private method by that name, and shadowing
        # it makes join() raise TypeError once the thread has finished —
        # "'Event' object is not callable", from inside threading itself.
        self._stopping = threading.Event()
        # Woken by "refresh now" rather than waiting out the interval. It is
        # separate from _stop because a poke must not end the loop, and
        # stop() sets both so a stopping thread never sleeps on either.
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    def poke(self) -> None:
        """Poll now, rather than at the end of the current interval."""
        self._wake.set()

    def _sleep(self, delay: float) -> None:
        """Wait out the interval, unless something asks for a poll sooner."""
        self._wake.wait(delay)
        self._wake.clear()

    def _poll_once(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        log.info("[%s] %s poller started (every %ds)",
                 self.user, self.kind, self.poll_seconds)
        while not self._stopping.is_set():
            delay = self.poll_seconds
            try:
                self._poll_once()
            except Exception as exc:
                # Back off hard on auth errors so we never lock an account out.
                auth_error = (
                    isinstance(exc, urllib.error.HTTPError)
                    and exc.code in (401, 403)
                )
                delay = ERROR_BACKOFF_SECONDS if auth_error else min(
                    ERROR_BACKOFF_SECONDS, self.poll_seconds * 3
                )
                log.warning("[%s] %s poll failed: %s (retry in %ds)",
                            self.user, self.kind, exc, delay)
                # The phrase, then when it will try again — the log is the
                # only place that second half has to live, and the person's
                # own page trims it back off.
                synclog.add(self.kind, self.user,
                            f"{fault_phrase(exc)}{RETRY_SUFFIX}{delay}s",
                            ok=False)
            self._sleep(delay)


def start_pollers(users, store: Store, glucocore_token: str = "") -> list[BasePoller]:
    from .glucocore_poll import GlucoCorePoller
    from .nspull import NightscoutPoller
    from .tidepool import TidepoolPoller

    pollers = []
    for user in users:
        source = user.source or {}
        kind = source.get("type")
        poller = None
        if kind == "glucocore" and source.get("patient_id") and glucocore_token:
            poller = GlucoCorePoller(user.name, source, store, glucocore_token)
        elif kind == "tidepool" and source.get("email") and source.get("password"):
            poller = TidepoolPoller(user.name, source, store)
        elif kind == "nightscout" and source.get("url"):
            poller = NightscoutPoller(user.name, source, store)
        elif kind in ("tidepool", "nightscout", "glucocore"):
            log.warning("[%s] %s source is missing credentials; not polling",
                        user.name, kind)
        if poller:
            poller.start()
            pollers.append(poller)
    return pollers
