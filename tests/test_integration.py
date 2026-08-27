"""End-to-end: start the real application and use it.

Everything else in this suite calls into the app in-process. These tests
run ``python -m glucocube`` the way systemd does — config file, servers,
pollers, web admin, display — and then talk to it over sockets, which is
the only way to catch a startup order problem or a module that only fails
when it is imported for real.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import Client, free_port

ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 60.0


def env_for_headless() -> dict:
    env = {**os.environ,
           "SDL_VIDEODRIVER": "dummy",     # what the shipped image uses too
           "SDL_AUDIODRIVER": "dummy",
           "GLUCOCUBE_TOUCH": "off",
           "PYTHONPATH": str(ROOT)}
    env.pop("INVOCATION_ID", None)          # no self-updating in a test
    return env


def write_config(tmp_path: Path, **overrides) -> Path:
    config = {
        "users": [{"name": "Ada", "port": free_port(), "api_secret": "ada-secret"},
                  {"name": "Bo", "port": free_port(), "api_secret": "bo-secret"}],
        "display": {"fullscreen": False, "width": 800, "height": 480},
        "database": str(tmp_path / "glucocube.db"),
        "admin": {"port": free_port(), "password": "pw1234"},
    }
    config.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def wait_for_port(port: int, process: subprocess.Popen,
                  timeout: float = STARTUP_TIMEOUT) -> None:
    """Wait until the server on this port answers.

    A whole HTTP request rather than a bare connect-and-close: the latter
    leaves the handler writing a "bad request syntax" 400 into a socket
    that has already gone, which prints a broken-pipe traceback into the
    test output for no reason. Any status counts as up — the web admin
    answers 401 until it is given a password.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode(errors="replace")
            raise AssertionError(f"the app exited early:\n{output}")
        try:
            Client(port).get("/api/health.json")
            return
        except (OSError, http.client.HTTPException):
            time.sleep(0.2)
    raise AssertionError(f"nothing listening on {port} after {timeout:.0f}s")


@pytest.fixture
def running_app(tmp_path):
    """The app running headless, with the servers and the web admin up."""
    config_path = write_config(tmp_path)
    config = json.loads(config_path.read_text())
    process = subprocess.Popen(
        [sys.executable, "-m", "glucocube", "--no-display",
         "--config", str(config_path)],
        cwd=str(ROOT), env=env_for_headless(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_for_port(config["admin"]["port"], process)
        yield config, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


# ------------------------------------------------------------ screenshot ----

def test_the_app_renders_one_frame_and_exits(tmp_path):
    """``--screenshot`` is what a developer (and this test) uses to look."""
    config_path = write_config(tmp_path)
    out = tmp_path / "screen.png"
    result = subprocess.run(
        [sys.executable, "-m", "glucocube", "--demo", "--config",
         str(config_path), "--screenshot", str(out)],
        cwd=str(ROOT), env=env_for_headless(), capture_output=True, timeout=120)

    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    assert out.exists()
    pygame = pytest.importorskip("pygame")
    assert pygame.image.load(str(out)).get_size() == (800, 480)


def test_a_missing_config_is_created_rather_than_fatal(tmp_path):
    """A fresh image has no config; it must boot into the setup flow."""
    config_path = tmp_path / "config.json"
    out = tmp_path / "screen.png"
    result = subprocess.run(
        [sys.executable, "-m", "glucocube", "--demo", "--config",
         str(config_path), "--screenshot", str(out)],
        cwd=str(ROOT), env=env_for_headless(), capture_output=True, timeout=120)

    assert result.returncode == 0, result.stderr.decode()
    assert config_path.exists()
    created = json.loads(config_path.read_text())
    assert len(created["users"]) == 2
    assert created["admin"]["password"]


# ------------------------------------------------------------- servicing ----

def test_the_web_admin_answers(running_app):
    config, _process = running_app
    client = Client(config["admin"]["port"])
    status, body = client.json("/api/health.json",
                               headers={"Authorization": "Basic " + _basic()})
    assert status == 200
    assert body["ok"] is True


def test_the_web_admin_asks_for_the_password(running_app):
    config, _process = running_app
    assert Client(config["admin"]["port"]).get("/settings")[0] == 401


def test_each_person_gets_their_own_nightscout_port(running_app):
    config, _process = running_app
    for user in config["users"]:
        status, body = Client(user["port"]).json("/api/v1/status.json")
        assert status == 200
        assert body["name"] == "glucocube"


def test_an_upload_reaches_the_dashboard(running_app):
    """The whole path: Trio posts, the store keeps it, the web app shows it."""
    config, _process = running_app
    ada = config["users"][0]
    import hashlib

    now_ms = int(time.time() * 1000)
    status, _headers, _body = Client(ada["port"]).request(
        "POST", "/api/v1/entries.json",
        [{"sgv": 123, "date": now_ms, "direction": "Flat", "type": "sgv"}],
        {"api-secret": hashlib.sha1(b"ada-secret").hexdigest(),
         "Content-Type": "application/json"})
    assert status == 200

    _status, dashboard = Client(config["admin"]["port"]).json(
        "/api/dashboard.json", headers={"Authorization": "Basic " + _basic()})
    ada_panel = next(u for u in dashboard["users"] if u["name"] == "Ada")
    assert ada_panel["sgv"] == 123
    assert ada_panel["direction"] == "Flat"


def test_an_upload_shows_up_in_the_sync_log(running_app):
    config, _process = running_app
    import hashlib

    Client(config["users"][0]["port"]).request(
        "POST", "/api/v1/entries.json",
        [{"sgv": 123, "date": int(time.time() * 1000)}],
        {"api-secret": hashlib.sha1(b"ada-secret").hexdigest(),
         "Content-Type": "application/json"})
    _status, log = Client(config["admin"]["port"]).json(
        "/api/log.json", headers={"Authorization": "Basic " + _basic()})
    assert any(entry["user"] == "Ada" for entry in log["entries"])


def test_data_written_by_one_run_is_there_for_the_next(tmp_path):
    """The database is the only state that survives a restart."""
    config_path = write_config(tmp_path)
    config = json.loads(config_path.read_text())
    import hashlib

    for expected in (None, 150):
        process = subprocess.Popen(
            [sys.executable, "-m", "glucocube", "--no-display",
             "--config", str(config_path)],
            cwd=str(ROOT), env=env_for_headless(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            wait_for_port(config["users"][0]["port"], process)
            client = Client(config["users"][0]["port"])
            if expected is None:
                client.request(
                    "POST", "/api/v1/entries.json",
                    [{"sgv": 150, "date": int(time.time() * 1000)}],
                    {"api-secret": hashlib.sha1(b"ada-secret").hexdigest(),
                     "Content-Type": "application/json"})
            else:
                _status, entries = client.json("/api/v1/entries.json?count=1")
                assert entries[0]["sgv"] == expected
        finally:
            process.terminate()
            process.wait(timeout=15)


def test_a_pull_person_gets_no_listener(tmp_path):
    """Their port stays reserved in config.json, but nothing binds it."""
    config_path = write_config(tmp_path)
    config = json.loads(config_path.read_text())
    config["users"][1]["source"] = {"type": "nightscout",
                                    "url": "https://ns.example.invalid",
                                    "poll_seconds": 3600}
    config_path.write_text(json.dumps(config))

    process = subprocess.Popen(
        [sys.executable, "-m", "glucocube", "--no-display",
         "--config", str(config_path)],
        cwd=str(ROOT), env=env_for_headless(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_for_port(config["users"][0]["port"], process)
        with socket.socket() as sock:
            sock.settimeout(2)
            assert sock.connect_ex(("127.0.0.1", config["users"][1]["port"])) != 0
    finally:
        process.terminate()
        process.wait(timeout=15)


def test_a_config_the_loader_rejects_stops_the_app_loudly(tmp_path):
    """Better a failed start than a device running an unintended config."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"users": [
        {"name": "Ada", "port": 1337}, {"name": "Bo", "port": 1337}]}))
    result = subprocess.run(
        [sys.executable, "-m", "glucocube", "--no-display",
         "--config", str(config_path)],
        cwd=str(ROOT), env=env_for_headless(), capture_output=True, timeout=60)
    assert result.returncode != 0
    assert b"unique port" in result.stdout + result.stderr


def _basic() -> str:
    import base64
    return base64.b64encode(b"admin:pw1234").decode()
