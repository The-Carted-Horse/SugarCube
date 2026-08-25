"""__main__.py — the pieces of startup that can be tested in process.

The whole entry point is exercised for real in test_integration.py; what
is here is the demo seeding (which is what a developer sees when they run
``--demo``) and the boot-time display wait.
"""

import time

import pytest

from glucocube import __main__ as main_mod
from glucocube.__main__ import seed_demo_data, wait_for_connected_display
from glucocube.config import UserConfig
from glucocube.predict import predict

USERS = [UserConfig(name="Ada", port=1337, api_secret="a"),
         UserConfig(name="Bo", port=1338, api_secret="b")]


def test_demo_data_fills_every_panel(store):
    seed_demo_data(store, USERS)
    for user in USERS:
        snap = store.snapshot(user.name)
        assert snap.sgv is not None
        assert len(snap.history) > 30
        assert snap.iob is not None
        assert snap.last_bolus and snap.last_carbs


def test_demo_data_is_fresh_enough_to_draw(store):
    """A stale demo would render the "no data" state instead of the layout."""
    seed_demo_data(store, USERS)
    now_ms = int(time.time() * 1000)
    for user in USERS:
        snap = store.snapshot(user.name)
        assert now_ms - snap.sgv_date < 6 * 60 * 1000


def test_demo_data_exercises_both_forecast_paths(store):
    """One person carries a device curve, the other falls back to ours."""
    seed_demo_data(store, USERS)
    sources = {predict(store.snapshot(user.name))[2] for user in USERS}
    assert sources == {"device", "est"}


def test_demo_data_is_the_same_every_run(store):
    """Seeded randomness: a screenshot from --demo is comparable to the last."""
    seed_demo_data(store, USERS)
    first = [entry["sgv"] for entry in store.get_entries("Ada", 100)]
    second_store = type(store)(":memory:")
    try:
        seed_demo_data(second_store, USERS)
        second = [entry["sgv"] for entry in second_store.get_entries("Ada", 100)]
    finally:
        second_store.close()
    assert first == second


def test_the_two_demo_people_do_not_look_identical(store):
    seed_demo_data(store, USERS)
    assert store.snapshot("Ada").sgv != store.snapshot("Bo").sgv


@pytest.fixture
def instant_sleep(monkeypatch):
    """The wait polls once a second; the tests need the shape, not the wait."""
    monkeypatch.setattr(main_mod.time, "sleep", lambda seconds: None)


def test_waiting_for_the_panel_gives_up_rather_than_hanging(monkeypatch,
                                                            instant_sleep):
    """Early in boot the panel may still be probing; the app tries anyway."""
    monkeypatch.setattr("glob.glob", lambda pattern: [])
    started = time.monotonic()
    assert wait_for_connected_display(timeout=0.2) is False
    assert time.monotonic() - started < 10


def test_a_connected_panel_is_noticed(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("connected\n")
    monkeypatch.setattr("glob.glob", lambda pattern: [str(status)])
    assert wait_for_connected_display(timeout=5) is True


def test_a_disconnected_panel_is_not_mistaken_for_a_connected_one(monkeypatch,
                                                                  tmp_path,
                                                                  instant_sleep):
    status = tmp_path / "status"
    status.write_text("disconnected\n")
    monkeypatch.setattr("glob.glob", lambda pattern: [str(status)])
    assert wait_for_connected_display(timeout=0.2) is False


def test_an_unreadable_status_file_is_skipped(monkeypatch, tmp_path,
                                              instant_sleep):
    monkeypatch.setattr("glob.glob", lambda pattern: [str(tmp_path / "gone")])
    assert wait_for_connected_display(timeout=0.2) is False


@pytest.mark.parametrize("flag", ["--demo", "--windowed", "--no-display",
                                  "--screenshot", "--config"])
def test_the_documented_flags_exist(flag):
    """The README and the systemd unit both name these."""
    from glucocube.__main__ import main
    import inspect

    assert flag.lstrip("-").replace("-", "_") in inspect.getsource(main)
