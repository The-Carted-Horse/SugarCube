"""synclog.py — the ring buffer behind /log."""

import threading

from glucocube import synclog


def test_an_entry_carries_who_what_and_when():
    synclog.add("push", "Ada", "received 3 readings")
    entry = synclog.recent()[0]
    assert (entry["source"], entry["user"]) == ("push", "Ada")
    assert entry["message"] == "received 3 readings"
    assert entry["ok"] is True
    assert entry["ts"] > 0


def test_failures_are_marked():
    synclog.add("tidepool", "Ada", "poll failed", ok=False)
    assert synclog.recent()[0]["ok"] is False


def test_the_newest_entry_comes_first():
    for i in range(5):
        synclog.add("push", "Ada", f"message {i}")
    assert [e["message"] for e in synclog.recent()] == \
        [f"message {i}" for i in reversed(range(5))]


def test_the_limit_is_honoured():
    for i in range(20):
        synclog.add("push", "Ada", f"message {i}")
    assert len(synclog.recent(5)) == 5


def test_the_buffer_does_not_grow_without_bound():
    """It runs for months on a device with 1GB of RAM."""
    for i in range(_capacity() + 50):
        synclog.add("push", "Ada", f"message {i}")
    assert len(synclog.recent(limit=10_000)) == _capacity()


def test_the_oldest_entries_are_the_ones_dropped():
    for i in range(_capacity() + 10):
        synclog.add("push", "Ada", f"message {i}")
    messages = {e["message"] for e in synclog.recent(limit=10_000)}
    assert "message 0" not in messages
    assert f"message {_capacity() + 9}" in messages


def test_recent_returns_a_copy_that_cannot_be_mutated_into_the_log():
    synclog.add("push", "Ada", "one")
    snapshot = synclog.recent()
    snapshot.clear()
    assert len(synclog.recent()) == 1


def test_concurrent_writers_lose_nothing():
    """Every poller and every push handler writes here from its own thread."""
    def writer(index):
        for i in range(50):
            synclog.add("push", f"user{index}", f"{index}-{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(synclog.recent(limit=10_000)) == 200


def _capacity() -> int:
    return synclog._entries.maxlen
