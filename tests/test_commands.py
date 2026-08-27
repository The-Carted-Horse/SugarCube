"""commands.py — the buttons on GlucoCore's devices screen.

The queue has a delivery step and an acknowledgement step, which is what
lets that screen distinguish "the display never took it" from "it took it
and it failed". So what matters here is that every command is answered:
the ones this display does not do, the ones that raise, and the ones that
work. A command that is silently dropped looks exactly like a display
that has stopped.
"""

import json
import threading

import pytest

from glucocube import commands, glucocore
from glucocube.store import Store


class FakeService:
    """The queue, as far as the display can tell."""

    def __init__(self, queued=(), fail_ack=False):
        self.queued = list(queued)
        self.acks = []
        self.fail_ack = fail_ack
        self.collected = 0

    def list_commands(self, device_token, timeout=30):
        self.collected += 1
        queued, self.queued = self.queued, []
        return queued

    def ack_command(self, device_token, command_id, ok=True, detail="",
                    timeout=30):
        if self.fail_ack:
            raise RuntimeError("network went away")
        self.acks.append({"id": command_id, "ok": ok, "detail": detail})

    def install(self, monkeypatch):
        monkeypatch.setattr(glucocore, "list_commands", self.list_commands)
        monkeypatch.setattr(glucocore, "ack_command", self.ack_command)
        return self


@pytest.fixture
def service(monkeypatch):
    def make(queued=(), fail_ack=False):
        return FakeService(queued, fail_ack).install(monkeypatch)
    return make


def queued(name, command_id="cmd-1"):
    return {"id": command_id, "command": name, "payload": {},
            "createdAt": "2026-01-01T00:00:00.000Z"}


def test_a_command_runs_and_is_acknowledged(service):
    ran = []
    fake = service([queued("identify")])
    count = commands.run_pending("device-token",
                                 {"identify": lambda: ran.append(1) or "ok"})
    assert count == 1
    assert ran == [1]
    assert fake.acks == [{"id": "cmd-1", "ok": True, "detail": "ok"}]


def test_what_the_command_did_travels_back_with_it(service):
    """"Done" is not much of an answer on a screen in another building."""
    fake = service([queued("refresh")])
    commands.run_pending("device-token",
                         {"refresh": lambda: "polling 2 sources now"})
    assert fake.acks[0]["detail"] == "polling 2 sources now"


def test_a_command_that_raises_is_acknowledged_as_failed(service):
    fake = service([queued("clear_cache")])

    def boom():
        raise RuntimeError("the database is locked")

    commands.run_pending("device-token", {"clear_cache": boom})
    assert fake.acks[0]["ok"] is False
    assert "the database is locked" in fake.acks[0]["detail"]


def test_a_command_this_display_does_not_do_says_so(service):
    """Better than leaving it undelivered forever on somebody's screen."""
    fake = service([queued("identify")])
    commands.run_pending("device-token", {})
    assert fake.acks[0]["ok"] is False
    assert fake.acks[0]["detail"] == "this display does not do that"


def test_a_command_from_a_newer_glucocore_is_named_in_the_answer(service):
    fake = service([queued("teleport")])
    commands.run_pending("device-token", {})
    assert fake.acks[0]["ok"] is False
    assert "teleport" in fake.acks[0]["detail"]


def test_several_commands_all_run(service):
    ran = []
    fake = service([queued("identify", "a"), queued("refresh", "b")])
    count = commands.run_pending("device-token", {
        "identify": lambda: ran.append("identify"),
        "refresh": lambda: ran.append("refresh"),
    })
    assert count == 2
    assert ran == ["identify", "refresh"]
    assert [ack["id"] for ack in fake.acks] == ["a", "b"]


def test_one_command_failing_does_not_strand_the_next(service):
    ran = []

    def boom():
        raise RuntimeError("no")

    fake = service([queued("clear_cache", "a"), queued("refresh", "b")])
    commands.run_pending("device-token",
                         {"clear_cache": boom,
                          "refresh": lambda: ran.append("refresh")})
    assert ran == ["refresh"]
    assert [ack["ok"] for ack in fake.acks] == [False, True]


def test_a_command_with_no_id_is_skipped(service):
    """Nothing to acknowledge means nothing that can be resolved."""
    fake = service([{"command": "identify"}])
    ran = []
    commands.run_pending("device-token", {"identify": lambda: ran.append(1)})
    assert ran == []
    assert fake.acks == []


def test_a_lost_acknowledgement_is_not_a_crash(service):
    """The command ran; only the receipt was lost. Re-delivery covers it."""
    ran = []
    service([queued("identify")], fail_ack=True)
    count = commands.run_pending("device-token",
                                 {"identify": lambda: ran.append(1)})
    assert count == 1
    assert ran == [1]


def test_a_service_that_cannot_be_reached_is_not_a_crash(monkeypatch):
    """This runs in a loop whose next pass is worth more than a traceback."""
    def boom(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(glucocore, "list_commands", boom)
    assert commands.run_pending("device-token", {}) == 0


def test_a_long_detail_is_trimmed_to_what_the_service_stores(service):
    fake = service([queued("refresh")])
    commands.run_pending("device-token", {"refresh": lambda: "x" * 1000})
    assert len(fake.acks[0]["detail"]) == 300


# ------------------------------------------------ what the actions do ----
#
# Built in __main__ against the running pieces, so they are exercised the
# same way: real store, real pollers, and the process-exit stubbed out.

@pytest.fixture
def runtime(config_path, monkeypatch):
    from glucocube import webadmin
    from glucocube.config import load
    from glucocube.sources import BasePoller

    restarts = []
    monkeypatch.setattr(webadmin, "restart_soon",
                        lambda delay=0.8: restarts.append(delay))

    class Poller(BasePoller):
        def __init__(self, store):
            super().__init__("test", "Ada", 600, store)
            self.pokes = threading.Semaphore(0)

        def poke(self):
            self.pokes.release()

        def _poll_once(self):
            pass

    store = Store(":memory:")
    config = load(config_path)
    pollers = [Poller(store)]
    from glucocube.__main__ import command_actions
    yield command_actions(config, store, pollers), store, pollers, restarts
    store.close()


def test_identify_asks_the_screen_to_flash(runtime):
    from glucocube.config import IDENTIFY_KEY
    actions, store, _pollers, _restarts = runtime
    detail = actions["identify"]()
    assert "flashing" in detail
    assert store.get_params(IDENTIFY_KEY)["until"] > 0


def test_restart_leaves_time_for_the_answer_to_get_out(runtime):
    actions, _store, _pollers, restarts = runtime
    actions["restart"]()
    assert restarts and restarts[0] >= 1


def test_refresh_pokes_every_poller(runtime):
    actions, _store, pollers, _restarts = runtime
    detail = actions["refresh"]()
    assert pollers[0].pokes.acquire(timeout=1)
    assert "polling 1 source now" == detail


def test_refresh_on_a_push_only_display_says_there_is_nothing_to_fetch(
        config_path):
    from glucocube.config import load
    from glucocube.__main__ import command_actions
    store = Store(":memory:")
    try:
        actions = command_actions(load(config_path), store, [])
        assert "nothing to fetch" in actions["refresh"]()
    finally:
        store.close()


def test_clearing_the_cache_drops_readings_and_fetches_again(runtime):
    actions, store, pollers, _restarts = runtime
    store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    detail = actions["clear_cache"]()
    assert store.snapshot("Ada").sgv is None
    assert "dropped 1 stored rows" in detail
    assert pollers[0].pokes.acquire(timeout=1)


def test_clearing_the_cache_keeps_the_therapy_settings(runtime):
    """They are read from a profile, not readings — and the forecast needs them."""
    actions, store, _pollers, _restarts = runtime
    store.set_params("Ada", {"isf": 50})
    actions["clear_cache"]()
    assert store.get_params("Ada") == {"isf": 50}


def test_check_update_reports_what_the_check_found(runtime, monkeypatch):
    from glucocube import updater
    actions, _store, _pollers, _restarts = runtime
    monkeypatch.setattr(updater, "check_and_maybe_force",
                        lambda store, channel: {"available": True,
                                                "latest": "v2.1.0"})
    assert "v2.1.0 is available" == actions["check_update"]()


def test_check_update_names_the_channel_when_there_is_nothing_new(
        runtime, monkeypatch):
    from glucocube import updater
    actions, _store, _pollers, _restarts = runtime
    monkeypatch.setattr(updater, "check_and_maybe_force",
                        lambda store, channel: {})
    assert "up to date on the standard channel" == actions["check_update"]()


def test_every_command_glucocore_can_send_has_an_action(runtime):
    """The five are a fixed list; a gap would be a button that does nothing."""
    actions, _store, _pollers, _restarts = runtime
    assert set(actions) == set(commands.KNOWN)


def test_the_actions_answer_the_queue_end_to_end(runtime, service):
    actions, store, _pollers, _restarts = runtime
    fake = service([queued("identify")])
    commands.run_pending("device-token", actions)
    assert fake.acks[0]["ok"] is True
    assert "flashing" in fake.acks[0]["detail"]
    assert json.dumps(store.get_params("__identify"))
