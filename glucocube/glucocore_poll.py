"""GlucoCore data poller — reads patient data with a device token."""

import logging
import urllib.error
from datetime import datetime, timedelta, timezone

from . import glucocore, synclog
from .sources import BasePoller
from .store import Store
from .tidepool import params_from_pumpsettings, transform

log = logging.getLogger("glucocube.glucocore_poll")


class GlucoCorePoller(BasePoller):
    def __init__(self, user: str, source: dict, store: Store, device_token: str):
        super().__init__("glucocore", user, source.get("poll_seconds", 60), store)
        self.patient_id = source["patient_id"]
        self.device_token = device_token
        self._settings_countdown = 0

    def _fetch(self) -> list[dict]:
        start = (
            datetime.now(timezone.utc) - timedelta(hours=6)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return glucocore.fetch_patient_data(
            self.device_token, self.patient_id, start,
        )

    def _poll_once(self) -> None:
        try:
            docs = self._fetch()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                synclog.add("glucocore", self.user,
                            "device token rejected — re-pair in GlucoCore", ok=False)
            raise
        entries, treatments, devicestatus = transform(docs)
        if entries:
            self.store.add_entries(self.user, entries)
        if treatments:
            self.store.add_treatments(self.user, treatments)
        if devicestatus:
            self.store.add_devicestatus(self.user, devicestatus)
        if self._settings_countdown <= 0:
            self._settings_countdown = 15
            try:
                settings_docs = glucocore.fetch_patient_data(
                    self.device_token,
                    self.patient_id,
                    (datetime.now(timezone.utc) - timedelta(days=365)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z",
                    ),
                )
                settings = [d for d in settings_docs if d.get("type") == "pumpSettings"]
                params = params_from_pumpsettings(settings[-1:] if settings else [])
                if params:
                    self.store.set_params(self.user, params)
            except Exception as exc:
                log.debug("[%s] pumpSettings fetch failed: %s", self.user, exc)
        self._settings_countdown -= 1
        synclog.add(
            "glucocore",
            self.user,
            f"pulled {len(entries)} readings, {len(treatments)} treatments",
        )
