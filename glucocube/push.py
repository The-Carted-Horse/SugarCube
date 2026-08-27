"""Outbound push listener — GlucoCore Realtime (when available) + long-poll fallback."""

import json
import logging
import threading
import time

from . import commands, glucocore, sync

log = logging.getLogger("glucocube.push")


class PushListener(threading.Thread):
    """Maintains config sync channels and invokes on_config when config changes."""

    def __init__(
        self,
        config_path: str,
        device_token: str,
        store,
        on_config,
        poll_interval: float = 60,
        actions: dict | None = None,
    ):
        super().__init__(name="glucocore-push", daemon=True)
        self.config_path = config_path
        self.device_token = device_token
        self.store = store
        self.on_config = on_config
        self.poll_interval = poll_interval
        # What the buttons on GlucoCore's devices screen actually do here.
        # Empty means a display that collects its commands and reports that
        # it does none of them, which is still better than one that leaves
        # them undelivered forever.
        self.actions = actions or {}
        self._stopping = threading.Event()
        self._push_connected = False

    def stop(self) -> None:
        self._stopping.set()

    def _last_version(self) -> int:
        return int(self.store.get_params(sync.LAST_VERSION_KEY).get("version") or 0)

    def _save_version(self, version: int) -> None:
        self.store.set_params(sync.LAST_VERSION_KEY, {"version": version})

    def _handle_config(self, remote: dict, version: int) -> None:
        if version <= self._last_version():
            return
        # Names come out of the config's own `perPatient` labels. This used
        # to read a `patientNames` map that the service has never sent, so
        # every person on a pushed config was named by their patient id.
        config = sync.apply_remote_config(self.config_path, remote, version,
                                          store=self.store)
        self._save_version(version)
        self.on_config(config)

    def _heartbeat(self) -> None:
        try:
            glucocore.heartbeat(self.device_token, {
                "pushConnected": self._push_connected,
                "firmwareVersion": _version(),
                # Beside the version GlucoCore holds, this answers the
                # question the devices screen is really asking: has what
                # was saved there actually reached the thing on the wall?
                "configVersion": self._last_version(),
            })
        except Exception as exc:
            log.debug("heartbeat failed: %s", exc)

    def _run_commands(self) -> None:
        commands.run_pending(self.device_token, self.actions)

    def _long_poll_once(self) -> None:
        since = self._last_version()
        result = glucocore.wait_config(self.device_token, since, timeout=55)
        if result.get("changed"):
            version = int(result.get("version") or since)
            config = result.get("config") or {}
            self._handle_config(config, version)

    def _realtime_loop(self) -> None:
        try:
            import websocket  # type: ignore[import-untyped]
        except ImportError:
            log.info("websocket-client not installed; using long-poll only")
            return

        backoff = 5
        while not self._stopping.is_set():
            try:
                creds = glucocore.get_realtime_token(self.device_token)
                if not creds.get("available"):
                    self._push_connected = False
                    self._stopping.wait(backoff)
                    backoff = min(backoff * 2, 120)
                    continue
                backoff = 5
                base = creds["supabaseUrl"].replace("https://", "wss://")
                url = (
                    f"{base}/realtime/v1/websocket?apikey={creds['supabaseAnonKey']}"
                    f"&vsn=1.0.0"
                )
                channel = creds["channel"]
                ws = websocket.create_connection(url, timeout=30)
                join = {
                    "topic": f"realtime:{channel}",
                    "event": "phx_join",
                    "payload": {"config": {"broadcast": {"self": True}}},
                    "ref": "1",
                }
                ws.send(json.dumps(join))
                self._push_connected = True
                ws.settimeout(55)
                while not self._stopping.is_set():
                    try:
                        message = ws.recv()
                        if not message:
                            break
                        payload = json.loads(message)
                        if payload.get("event") == "broadcast":
                            inner = (payload.get("payload") or {}).get("payload") or {}
                            if inner.get("type") == "config_updated":
                                self._handle_config(
                                    inner.get("config") or {},
                                    int(inner.get("version") or 0),
                                )
                            elif inner.get("type") == "command":
                                # The broadcast says one is waiting; the
                                # queue is still what hands it over, so
                                # that collecting and acknowledging stay
                                # the only path a command travels.
                                self._run_commands()
                            elif inner.get("type") == "revoked":
                                log.warning("device revoked by GlucoCore")
                    except Exception:
                        break
                ws.close()
            except Exception as exc:
                self._push_connected = False
                log.warning("realtime connection failed: %s", exc)
            self._stopping.wait(backoff)

    def run(self) -> None:
        try:
            current = glucocore.get_config(self.device_token)
            self._save_version(int(current.get("version") or 0))
        except Exception as exc:
            log.warning("could not fetch initial config version: %s", exc)

        threading.Thread(target=self._realtime_loop, name="glucocore-rt", daemon=True).start()
        last_hb = 0.0
        while not self._stopping.is_set():
            try:
                self._long_poll_once()
            except Exception as exc:
                log.warning("config long-poll failed: %s", exc)
                self._stopping.wait(5)
            now = time.time()
            if now - last_hb >= self.poll_interval:
                self._heartbeat()
                # Riding along with the heartbeat rather than on a timer of
                # its own: a display with no realtime channel then picks its
                # commands up within the minute, and one with a channel has
                # already run them.
                self._run_commands()
                last_hb = now


def _version() -> str:
    """What this display says it is running, on the devices screen."""
    from .updater import current_version
    try:
        return current_version()
    except Exception:  # noqa: BLE001 - a version is not worth a crash
        return "glucocube"


def start_push_listener(config_path, glucocore_block, store, on_config,
                        actions: dict | None = None) -> PushListener | None:
    token = (glucocore_block or {}).get("device_token")
    if not token:
        return None
    listener = PushListener(config_path, token, store, on_config,
                            actions=actions)
    listener.start()
    return listener
