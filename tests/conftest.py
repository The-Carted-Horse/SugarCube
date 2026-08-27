"""Shared fixtures.

Two rules hold for the whole suite, and they are enforced here rather than
left to each test to remember:

* **No network.** Every outbound call in the app goes through
  ``urllib.request.urlopen``; the autouse fixture below replaces it with
  something that raises, so a test that forgets to stub a poller fails
  loudly instead of reaching Tidepool or GitHub from CI.
* **No nmcli.** ``network`` shells out to NetworkManager. The runner has
  none, but a developer's machine might, so it is stubbed to the same
  "not installed" answer everywhere.

Local HTTP servers are exercised with ``http.client`` on purpose: it does
not go through urlopen, so the block above stays in force even in the
end-to-end tests.
"""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import Client, free_port  # noqa: E402,F401  (re-exported)

from glucocube import network, synclog, verify  # noqa: E402
from glucocube.store import Store  # noqa: E402


class NetworkUsed(AssertionError):
    """Raised when a test reaches for the real internet."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        target = args[0] if args else "?"
        url = getattr(target, "full_url", target)
        raise NetworkUsed(f"test tried to open {url}")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    return blocked


@pytest.fixture(autouse=True)
def no_nmcli(monkeypatch):
    """NetworkManager is absent, as it is on any non-Pi machine."""
    monkeypatch.setattr(network, "_nmcli",
                        lambda *a, **k: (-1, "nmcli is not installed"))
    monkeypatch.setattr(network, "available", lambda: False)
    monkeypatch.setattr(network, "_hotspot_cache", (False, 0.0), raising=False)
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: False)


@pytest.fixture(autouse=True)
def clean_module_state(monkeypatch):
    """Module-level state that would otherwise leak between tests."""
    monkeypatch.setattr(verify, "_last_attempt", {})
    monkeypatch.setattr(network, "_store", None)
    synclog._entries.clear()
    yield
    synclog._entries.clear()


@pytest.fixture
def store():
    db = Store(":memory:")
    yield db
    db.close()


@pytest.fixture
def config_path(tmp_path):
    """A minimal, valid config.json — the shape load() expects."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [
            {"name": "Ada", "port": 1337, "api_secret": "ada-secret"},
            {"name": "Bo", "port": 1338, "api_secret": ""},
        ],
        "display": {"low": 70, "high": 180},
        "database": "glucocube.db",
        "admin": {"port": 8080, "password": "letmein"},
    }, indent=2))
    return path


@pytest.fixture
def client_factory():
    return Client
