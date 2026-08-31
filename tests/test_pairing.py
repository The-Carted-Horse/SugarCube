"""pairing.py — the display asking to be paired, and waiting to be.

The thread that does this runs on a display nobody has set up yet, so
what matters is that it keeps a live request going without supervision:
it renews before the request lapses, it survives a service that is not
answering, and the secret it holds never leaves it.
"""

import threading
import time

import pytest

from glucocube import glucocore, pairing, sync


@pytest.fixture
def service(monkeypatch):
    """GlucoCore, as far as an unpaired display can tell."""

    class Fake:
        def __init__(self):
            self.asked = []
            self.collected = []
            self.approve_after = None      # collections before it is approved
            self.device = {"id": "dev-42", "name": "Kitchen display",
                           "config": {"patientIds": ["pat-1"],
                                      "display": {"low": 75},
                                      "perPatient": {"pat-1": {"label": "Grace"}}}}
            self.fail_ask = None
            self.serial = 0

        def request_pairing(self, hardware_id, name="", timeout=30):
            if self.fail_ask:
                raise self.fail_ask
            self.serial += 1
            self.asked.append({"hardware_id": hardware_id, "name": name})
            return {
                "id": f"req-{self.serial}",
                "secret": f"secret-{self.serial}",
                "approveUrl": f"https://www.glucocore.app/devices/add?request=req-{self.serial}",
                "expiresAt": _iso(time.time() + 600),
            }

        def collect_pairing(self, request_id, secret, timeout=30):
            self.collected.append({"id": request_id, "secret": secret})
            if (self.approve_after is not None
                    and len(self.collected) > self.approve_after):
                return {"approved": True, "device": self.device,
                        "deviceToken": "device-token"}
            return {"approved": False}

    fake = Fake()
    monkeypatch.setattr(glucocore, "request_pairing", fake.request_pairing)
    monkeypatch.setattr(glucocore, "collect_pairing", fake.collect_pairing)
    monkeypatch.setattr(pairing, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(pairing, "ERROR_BACKOFF_SECONDS", 0.01)
    return fake


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def waiter(config_path, store, **kwargs):
    paired = threading.Event()
    thread = pairing.PairingWaiter(str(config_path), store,
                                   paired.set, **kwargs)
    return thread, paired


def run_until(thread, done, seconds=3):
    thread.start()
    try:
        return done.wait(seconds)
    finally:
        thread.stop()
        thread.join(timeout=2)


# ------------------------------------------------------------- asking ----

def test_a_display_with_no_pairing_asks_for_one(config_path, store, service):
    thread, paired = waiter(config_path, store)
    service.approve_after = 1
    assert run_until(thread, paired)
    assert service.asked[0]["hardware_id"]
    assert service.asked[0]["name"]


def test_what_it_is_asking_is_readable_but_the_secret_is_not(config_path,
                                                             store, service):
    thread, paired = waiter(config_path, store)
    thread.start()
    try:
        for _ in range(200):
            if pairing.public_state(store).get("approve_url"):
                break
            time.sleep(0.01)
        state = pairing.public_state(store)
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert state["request_id"] == "req-1"
    assert "devices/add?request=req-1" in state["approve_url"]
    assert "secret" not in state


def test_it_asks_again_before_the_request_lapses(config_path, store, service,
                                                 monkeypatch):
    """A QR code on a wall that quietly stopped working is worse than none."""
    monkeypatch.setattr(pairing, "RENEW_MARGIN_SECONDS", 10_000)
    thread, paired = waiter(config_path, store)
    thread.start()
    try:
        for _ in range(300):
            if len(service.asked) >= 2:
                break
            time.sleep(0.01)
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert len(service.asked) >= 2, "a lapsing request is replaced"


def test_a_request_still_good_is_not_replaced(config_path, store, service):
    thread, _paired = waiter(config_path, store)
    thread.start()
    try:
        time.sleep(0.3)
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert len(service.asked) == 1
    assert len(service.collected) > 1, "it keeps asking whether it is approved"


def test_a_service_that_is_not_answering_is_not_a_crash(config_path, store,
                                                        service):
    service.fail_ask = OSError("Name or service not known")
    thread, _paired = waiter(config_path, store)
    thread.start()
    try:
        for _ in range(200):
            if pairing.public_state(store).get("error"):
                break
            time.sleep(0.01)
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert thread.is_alive() is False
    # The page has something to say rather than a spinner that never ends.
    assert "Name or service not known" in pairing.public_state(store)["error"]


# ---------------------------------------------------------- collecting ----

def test_an_approved_request_is_written_down(config_path, store, service):
    service.approve_after = 1
    thread, paired = waiter(config_path, store)
    assert run_until(thread, paired)

    from glucocube.config import load
    config = load(config_path)
    assert config.glucocore.device_token == "device-token"
    assert config.glucocore.name == "Kitchen display"
    assert [u.name for u in config.users
            if (u.source or {}).get("type") == "glucocore"] == ["Grace"]
    assert config.display.low == 75


def test_the_request_is_forgotten_once_it_is_spent(config_path, store,
                                                   service):
    service.approve_after = 0
    thread, paired = waiter(config_path, store)
    assert run_until(thread, paired)
    assert pairing.public_state(store) == {}


def test_it_stops_asking_once_it_is_paired(config_path, store, service):
    service.approve_after = 0
    thread, paired = waiter(config_path, store)
    assert run_until(thread, paired)
    before = len(service.collected)
    time.sleep(0.1)
    assert len(service.collected) == before


def test_an_approval_with_no_token_is_not_a_pairing(config_path, store,
                                                    service, monkeypatch):
    monkeypatch.setattr(glucocore, "collect_pairing",
                        lambda *a, **k: {"approved": True, "device": {}})
    thread, paired = waiter(config_path, store)
    thread.start()
    try:
        assert paired.wait(0.5) is False
    finally:
        thread.stop()
        thread.join(timeout=2)
    assert "glucocore" not in config_path.read_text()


def test_people_fed_by_an_uploader_survive_being_scanned(config_path, store,
                                                         service):
    """Same rule as the other two ways in — pairing adds, it does not clear."""
    service.approve_after = 0
    thread, paired = waiter(config_path, store)
    assert run_until(thread, paired)
    from glucocube.config import load
    assert "Ada" in [user.name for user in load(config_path).users]


# ---------------------------------------------------- starting it at all ----

def test_a_paired_display_does_not_ask(config_path, store, service):
    from glucocube.config import load
    import json
    raw = json.loads(config_path.read_text())
    raw["glucocore"] = {"device_token": "already-paired"}
    config_path.write_text(json.dumps(raw))
    started = pairing.start_waiter(load(config_path), str(config_path), store,
                                   lambda: None)
    assert started is None
    assert service.asked == []


def test_an_unpaired_display_starts_asking(config_path, store, service):
    from glucocube.config import load
    started = pairing.start_waiter(load(config_path), str(config_path), store,
                                   lambda: None)
    assert started is not None
    started.stop()
    started.join(timeout=2)


# ------------------------------------------------------- shared ending ----

def test_all_three_ways_of_pairing_write_the_same_thing(config_path, store):
    """The QR, the code and the sign-in all end in write_pairing."""
    device = {"id": "dev-9", "name": "Hall", "config": {
        "patientIds": ["pat-1"], "display": {"high": 190},
        "perPatient": {"pat-1": {"label": "Grace"}}}}
    added = sync.write_pairing(config_path, device, "device-token", "mac-abc",
                               admin_port=8080, store=store)
    assert added == ["Grace"]

    from glucocube.config import load
    config = load(config_path)
    assert config.glucocore.device_id == "dev-9"
    assert config.glucocore.hardware_id == "mac-abc"
    assert config.display.high == 190
    assert store.get_params(sync.LAST_VERSION_KEY) == {}


def test_a_pairing_with_nobody_on_it_is_refused(config_path, store):
    with pytest.raises(ValueError, match="nobody on it"):
        sync.write_pairing(config_path, {"config": {"patientIds": []}},
                           "device-token", "mac-abc")
