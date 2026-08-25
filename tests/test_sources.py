"""sources.py — the poll loop's error handling, and which poller starts.

The backoff is the safety property here: a poller that keeps retrying a
rejected login every minute will get the account locked, so an auth error
has to back off hard and stay backed off.
"""

import urllib.error

import pytest

from glucocube import synclog
from glucocube.config import UserConfig
from glucocube.sources import ERROR_BACKOFF_SECONDS, BasePoller, start_pollers


class RecordingPoller(BasePoller):
    """A poller whose loop runs exactly once and records how long it waits."""

    def __init__(self, store, outcomes, poll_seconds=60):
        super().__init__("test", "Ada", poll_seconds, store)
        self.outcomes = list(outcomes)
        self.waits = []
        self.polls = 0

    def _poll_once(self):
        self.polls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def _wait(self, delay):
        self.waits.append(delay)
        self._stop.set()
        return True


@pytest.fixture
def run_once(monkeypatch):
    """Run a poller's loop body once, capturing the delay it chose."""
    def runner(poller):
        monkeypatch.setattr(poller._stop, "wait", poller._wait)
        poller.run()
        return poller.waits[-1]
    return runner


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "no", {}, None)


# --------------------------------------------------------------- backoff ----

def test_a_healthy_poll_waits_the_configured_interval(store, run_once):
    poller = RecordingPoller(store, [None], poll_seconds=60)
    assert run_once(poller) == 60
    assert poller.polls == 1


@pytest.mark.parametrize("code", [401, 403])
def test_an_auth_failure_backs_off_hard(store, run_once, code):
    """Retrying a rejected login every minute gets the account locked."""
    poller = RecordingPoller(store, [http_error(code)], poll_seconds=60)
    assert run_once(poller) == ERROR_BACKOFF_SECONDS


def test_another_failure_backs_off_more_gently(store, run_once):
    poller = RecordingPoller(store, [http_error(500)], poll_seconds=60)
    assert run_once(poller) == 180


def test_the_gentle_backoff_is_capped(store, run_once):
    poller = RecordingPoller(store, [OSError("down")], poll_seconds=200)
    assert run_once(poller) == ERROR_BACKOFF_SECONDS


def test_a_failure_is_reported_in_the_sync_log(store, run_once):
    run_once(RecordingPoller(store, [RuntimeError("upstream is down")]))
    entry = synclog.recent()[0]
    assert entry["ok"] is False
    assert "upstream is down" in entry["message"]
    assert "retry in" in entry["message"]


def test_a_failing_poll_does_not_kill_the_thread(store, run_once):
    """The loop has to survive whatever the far end does."""
    poller = RecordingPoller(store, [ValueError("nonsense")])
    run_once(poller)
    assert poller.polls == 1


def test_stop_ends_the_loop(store):
    poller = RecordingPoller(store, [])
    poller.stop()
    poller.run()
    assert poller.polls == 0


def test_the_base_poller_demands_an_implementation(store):
    poller = BasePoller("test", "Ada", 60, store)
    with pytest.raises(NotImplementedError):
        poller._poll_once()


@pytest.mark.parametrize("configured, expected", [
    (60, 60), (30, 30), (5, 30), (0, 30), (-10, 30), ("90", 90),
])
def test_the_poll_interval_has_a_floor(store, configured, expected):
    assert BasePoller("t", "Ada", configured, store).poll_seconds == expected


def test_a_poller_is_a_daemon_thread_named_for_its_person(store):
    """A stuck poller must never keep the process alive at shutdown."""
    poller = BasePoller("tidepool", "Ada", 60, store)
    assert poller.daemon is True
    assert poller.name == "tidepool-Ada"


# --------------------------------------------------------- start_pollers ----

class DummyPoller:
    started = []

    def __init__(self, kind, user, source, store):
        self.kind, self.user, self.source, self.store = kind, user, source, store

    def start(self):
        DummyPoller.started.append((self.kind, self.user))


@pytest.fixture
def dummy_pollers(monkeypatch):
    import glucocube.nspull as nspull_mod
    import glucocube.tidepool as tidepool_mod

    DummyPoller.started = []
    monkeypatch.setattr(tidepool_mod, "TidepoolPoller",
                        lambda user, source, store:
                            DummyPoller("tidepool", user, source, store))
    monkeypatch.setattr(nspull_mod, "NightscoutPoller",
                        lambda user, source, store:
                            DummyPoller("nightscout", user, source, store))
    return DummyPoller


def user(name="Ada", source=None) -> UserConfig:
    return UserConfig(name=name, port=1337, api_secret="s", source=source)


def test_a_tidepool_source_starts_a_tidepool_poller(store, dummy_pollers):
    pollers = start_pollers([user(source={"type": "tidepool",
                                          "email": "c@example.invalid",
                                          "password": "pw"})], store)
    assert dummy_pollers.started == [("tidepool", "Ada")]
    assert len(pollers) == 1


def test_a_nightscout_source_starts_a_nightscout_poller(store, dummy_pollers):
    start_pollers([user(source={"type": "nightscout",
                                "url": "https://ns.example.invalid"})], store)
    assert dummy_pollers.started == [("nightscout", "Ada")]


def test_a_push_person_gets_no_poller(store, dummy_pollers):
    assert start_pollers([user(), user("Bo", source={})], store) == []
    assert dummy_pollers.started == []


@pytest.mark.parametrize("source", [
    {"type": "tidepool", "email": "c@example.invalid"},   # no password
    {"type": "tidepool", "password": "pw"},               # no email
    {"type": "tidepool"},
    {"type": "nightscout"},                               # no url
    {"type": "nightscout", "url": ""},
])
def test_a_source_missing_its_credentials_does_not_start(store, dummy_pollers,
                                                         source):
    """Better a warning in the log than a thread failing every minute."""
    assert start_pollers([user(source=source)], store) == []
    assert dummy_pollers.started == []


def test_an_unknown_source_type_is_ignored(store, dummy_pollers):
    assert start_pollers([user(source={"type": "dexcom-share"})], store) == []


def test_each_person_gets_their_own_poller(store, dummy_pollers):
    start_pollers([
        user("Ada", {"type": "tidepool", "email": "a@example.invalid",
                     "password": "pw"}),
        user("Bo", {"type": "nightscout", "url": "https://ns.example.invalid"}),
    ], store)
    assert dummy_pollers.started == [("tidepool", "Ada"), ("nightscout", "Bo")]
