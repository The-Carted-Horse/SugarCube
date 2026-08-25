"""Outbound push listener — GlucoCore Realtime (when available) + long-poll fallback."""

import json
import logging
import threading
import time

from . import glucocore, sync

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
    ):
        super().__init__(name="glucocore-push", daemon=True)
        self.config_path = config_path
        self.device_token = device_token
        self.store = store
        self.on_config = on_config
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._push_connected = False

    def stop(self) -> None:
        self._stop.set()

    def _last_version(self) -> int:
        return int(self.store.get_params(sync.LAST_VERSION_KEY).get("version") or 0)

    def _save_version(self, version: int) -> None:
        self.store.set_params(sync.LAST_VERSION_KEY, {"version": version})

    def _handle_config(self, remote: dict, version: int) -> None:
        if version <= self._last_version():
            return
        patient_names = {}
        for patient_id in remote.get("patientIds") or []:
            patient_names[patient_id] = remote.get("patientNames", {}).get(
                patient_id, patient_id,
            )
        config = sync.apply_remote_config(
            self.config_path, remote, version, patient_names=patient_names,
        )
        self._save_version(version)
        self.on_config(config)

    def _heartbeat(self) -> None:
        try:
            glucocore.heartbeat(self.device_token, {
                "pushConnected": self._push_connected,
                "firmwareVersion": "glucocube",
            })
        except Exception as exc:
            log.debug("heartbeat failed: %s", exc)

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
        while not self._stop.is_set():
            try:
                creds = glucocore.get_realtime_token(self.device_token)
                if not creds.get("available"):
                    self._push_connected = False
                    self._stop.wait(backoff)
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
                while not self._stop.is_set():
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
                            elif inner.get("type") == "revoked":
                                log.warning("device revoked by GlucoCore")
                    except Exception:
                        break
                ws.close()
            except Exception as exc:
                self._push_connected = False
                log.warning("realtime connection failed: %s", exc)
            self._stop.wait(backoff)

    def run(self) -> None:
        try:
            current = glucocore.get_config(self.device_token)
            self._save_version(int(current.get("version") or 0))
        except Exception as exc:
            log.warning("could not fetch initial config version: %s", exc)

        threading.Thread(target=self._realtime_loop, name="glucocore-rt", daemon=True).start()
        last_hb = 0.0
        while not self._stop.is_set():
            try:
                self._long_poll_once()
            except Exception as exc:
                log.warning("config long-poll failed: %s", exc)
                self._stop.wait(5)
            now = time.time()
            if now - last_hb >= self.poll_interval:
                self._heartbeat()
                last_hb = now


def start_push_listener(config_path, glucocore_block, store, on_config) -> PushListener | None:
    token = (glucocore_block or {}).get("device_token")
    if not token:
        return None
    listener = PushListener(config_path, token, store, on_config)
    listener.start()
    return listener
