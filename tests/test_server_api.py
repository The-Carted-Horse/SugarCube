"""server.py — the Nightscout-compatible API, over real HTTP.

Trio talks to this, so it is tested the way Trio uses it: a socket, real
headers, real bodies. Anything less would not catch an auth header read
from the wrong place or a route that only works without ``.json``.
"""

import gzip
import hashlib
import json
import threading

import pytest

from glucocube import synclog
from glucocube.server import NightscoutServer, start_servers, stop_servers

from helpers import Client, free_port

SECRET = "trio-secret"
SHA1 = hashlib.sha1(SECRET.encode()).hexdigest()
AUTH = {"api-secret": SHA1}


@pytest.fixture
def api(store):
    """A running server for "Ada", secured with SECRET."""
    server = NightscoutServer(free_port(), "Ada", SECRET, store)
    # A short poll interval only shortens shutdown(); the app uses the default.
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    yield Client(server.server_address[1]), store, server
    server.shutdown()
    server.server_close()


@pytest.fixture
def open_api(store):
    """A server for a person with no API secret configured."""
    server = NightscoutServer(free_port(), "Bo", "", store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    yield Client(server.server_address[1]), store
    server.shutdown()
    server.server_close()


# --------------------------------------------------------------- status ----

def test_status_identifies_the_site_as_a_nightscout_one(api):
    client, _store, _server = api
    status, body = client.json("/api/v1/status.json")
    assert status == 200
    assert body["status"] == "ok"
    assert body["apiEnabled"] is True
    assert body["settings"]["units"] == "mg/dl"


@pytest.mark.parametrize("path", ["/api/v1/status", "/api/v1/status.json",
                                  "/status", "/api/v1/status/"])
def test_status_is_reachable_by_every_spelling_clients_use(api, path):
    client, _store, _server = api
    assert client.get(path)[0] == 200


def test_status_needs_no_secret(api):
    """Trio checks reachability before it has anything to send."""
    client, _store, _server = api
    assert client.get("/api/v1/status")[0] == 200


def test_an_unknown_path_is_a_json_404(api):
    client, _store, _server = api
    status, body = client.json("/api/v1/nothing-here")
    assert status == 404
    assert body["status"] == 404


# ----------------------------------------------------------------- auth ----

def test_a_write_without_a_secret_is_rejected(api):
    client, store, _server = api
    status, _headers, _body = client.request("POST", "/api/v1/entries.json",
                                             [{"sgv": 120}])
    assert status == 401
    assert store.get_entries("Ada", 10) == []


def test_a_write_with_the_wrong_secret_is_rejected(api):
    client, _store, _server = api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    [{"sgv": 120}],
                                    {"api-secret": hashlib.sha1(b"nope").hexdigest()})
    assert status == 401


def test_the_sha1_hash_of_the_secret_is_accepted(api):
    """What Trio actually sends."""
    client, store, _server = api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    [{"sgv": 120}], AUTH)
    assert status == 200
    assert store.get_entries("Ada", 10)[0]["sgv"] == 120


def test_the_plain_secret_is_accepted_too(api):
    """Some uploaders send it unhashed; both are the configured secret."""
    client, _store, _server = api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    [{"sgv": 120}], {"api-secret": SECRET})
    assert status == 200


def test_the_hash_is_accepted_in_upper_case(api):
    client, _store, _server = api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    [{"sgv": 120}],
                                    {"api-secret": SHA1.upper()})
    assert status == 200


def test_an_empty_configured_secret_leaves_the_api_open(open_api):
    """Documented behaviour — the installer always writes one."""
    client, store = open_api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    [{"sgv": 120}])
    assert status == 200
    assert store.get_entries("Bo", 10)[0]["sgv"] == 120


def test_verifyauth_reports_the_answer_without_writing_anything(api):
    client, _store, _server = api
    assert client.request("GET", "/api/v1/verifyauth", None, AUTH)[0] == 200
    assert client.get("/api/v1/verifyauth")[0] == 401


def test_reads_do_not_need_the_secret(api):
    """A LAN-only display; the secret guards writes."""
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    assert client.json("/api/v1/entries.json")[0] == 200


# ---------------------------------------------------------------- posts ----

def test_posted_entries_are_stored_and_echoed(api):
    client, store, _server = api
    docs = [{"sgv": 120, "date": 1_700_000_000_000, "direction": "Flat"},
            {"sgv": 118, "date": 1_700_000_300_000, "direction": "Flat"}]
    status, _h, body = client.request("POST", "/api/v1/entries.json", docs, AUTH)
    assert status == 200
    assert len(json.loads(body)) == 2
    assert len(store.get_entries("Ada", 10)) == 2


def test_a_single_document_may_be_posted_unwrapped(api):
    client, store, _server = api
    status, _h, _b = client.request("POST", "/api/v1/entries.json",
                                    {"sgv": 120, "date": 1}, AUTH)
    assert status == 200
    assert len(store.get_entries("Ada", 10)) == 1


def test_a_gzipped_body_is_decompressed(api):
    """Trio compresses larger uploads."""
    client, store, _server = api
    raw = gzip.compress(json.dumps([{"sgv": 120, "date": 1}]).encode())
    status, _h, _b = client.request(
        "POST", "/api/v1/entries.json", raw,
        {**AUTH, "Content-Type": "application/json",
         "Content-Encoding": "gzip"})
    assert status == 200
    assert len(store.get_entries("Ada", 10)) == 1


def test_a_malformed_body_is_a_400_not_a_crash(api):
    client, _store, _server = api
    status, _h, body = client.request("POST", "/api/v1/entries.json",
                                      b"{not json", AUTH)
    assert status == 400
    assert json.loads(body)["status"] == 400


def test_an_empty_body_is_accepted_as_nothing(api):
    client, _store, _server = api
    status, _h, body = client.request("POST", "/api/v1/entries.json", b"", AUTH)
    assert status == 200
    assert json.loads(body) == []


def test_treatments_are_stored(api):
    client, store, _server = api
    client.request("POST", "/api/v1/treatments.json",
                   [{"eventType": "Bolus", "insulin": 2.5,
                     "created_at": "2024-03-01T12:00:00Z"}], AUTH)
    assert store.snapshot("Ada").last_bolus == 2.5


def test_devicestatus_is_stored_with_its_iob(api):
    client, store, _server = api
    client.request("POST", "/api/v1/devicestatus.json",
                   [{"created_at": "2024-03-01T12:00:00Z",
                     "openaps": {"iob": {"iob": 1.5}}}], AUTH)
    assert store.get_devicestatus("Ada", 10)[0]["openaps"]["iob"]["iob"] == 1.5


def test_a_put_updates_like_a_post(api):
    """Trio uses PUT for edits; the store upserts either way."""
    client, store, _server = api
    doc = {"_id": "t1", "eventType": "Carb Correction", "carbs": 20,
           "created_at": "2024-03-01T12:00:00Z"}
    client.request("POST", "/api/v1/treatments.json", [doc], AUTH)
    client.request("PUT", "/api/v1/treatments.json",
                   [{**doc, "carbs": 35}], AUTH)
    treatments = store.get_treatments("Ada", 10)
    assert len(treatments) == 1 and treatments[0]["carbs"] == 35


def test_a_posted_profile_is_readable_but_not_persisted(api):
    client, _store, _server = api
    profile = [{"defaultProfile": "Default", "store": {"Default": {"dia": 6}}}]
    client.request("POST", "/api/v1/profile.json", profile, AUTH)
    assert client.json("/api/v1/profile.json")[1] == profile


def test_posting_to_an_unknown_path_is_a_404(api):
    client, _store, _server = api
    assert client.request("POST", "/api/v1/nope", [{}], AUTH)[0] == 404


# ----------------------------------------------------------------- gets ----

def test_entries_come_back_newest_first(api):
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 100, "date": 1_700_000_000_000},
                              {"sgv": 110, "date": 1_700_000_300_000}])
    _status, body = client.json("/api/v1/entries.json")
    assert [doc["sgv"] for doc in body] == [110, 100]


def test_the_count_parameter_limits_the_answer(api):
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 100 + i, "date": 1_700_000_000_000 + i}
                              for i in range(30)])
    assert len(client.json("/api/v1/entries.json?count=5")[1]) == 5


@pytest.mark.parametrize("count", ["nonsense", "-4", "0"])
def test_a_junk_count_falls_back_to_something_sane(api, count):
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 100 + i, "date": 1_700_000_000_000 + i}
                              for i in range(30)])
    status, body = client.json(f"/api/v1/entries.json?count={count}")
    assert status == 200
    assert 1 <= len(body) <= 30


def test_a_huge_count_is_capped(api):
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 100, "date": 1_700_000_000_000 + i}
                              for i in range(50)])
    assert len(client.json("/api/v1/entries.json?count=999999")[1]) == 50


@pytest.mark.parametrize("path", ["/api/v1/entries", "/api/v1/entries.json",
                                  "/api/v1/entries/sgv",
                                  "/api/v1/entries/sgv.json"])
def test_every_entries_spelling_reads_the_same_data(api, path):
    client, store, _server = api
    store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    assert client.json(path)[1][0]["sgv"] == 120


def test_one_persons_server_never_shows_the_others_data(api):
    """Each person gets their own port; the port is the identity."""
    client, store, _server = api
    store.add_entries("Bo", [{"sgv": 200, "date": 1_700_000_000_000}])
    assert client.json("/api/v1/entries.json")[1] == []


def test_screen_png_is_a_404_before_the_display_has_drawn(api, monkeypatch):
    client, _store, _server = api
    monkeypatch.setattr("glucocube.server.SCREEN_PNG", "/nonexistent/screen.png")
    assert client.get("/screen.png")[0] == 404


def test_screen_png_is_served_once_the_display_has_drawn(api, monkeypatch, tmp_path):
    client, _store, _server = api
    png = tmp_path / "screen.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr("glucocube.server.SCREEN_PNG", str(png))
    status, headers, body = client.get("/screen.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")


# -------------------------------------------------------------- deletes ----

def test_a_treatment_can_be_deleted_by_id(api):
    client, store, _server = api
    store.add_treatments("Ada", [{"_id": "t1", "insulin": 2,
                                  "created_at": 1_700_000_000_000}])
    status, _h, body = client.request("DELETE", "/api/v1/treatments/t1",
                                      None, AUTH)
    assert status == 200
    assert json.loads(body)["n"] == 1
    assert store.get_treatments("Ada", 10) == []


def test_deleting_an_unknown_treatment_reports_nothing_removed(api):
    client, _store, _server = api
    _status, _h, body = client.request("DELETE", "/api/v1/treatments/nope",
                                       None, AUTH)
    assert json.loads(body)["n"] == 0


def test_a_delete_without_the_secret_is_rejected(api):
    client, store, _server = api
    store.add_treatments("Ada", [{"_id": "t1", "insulin": 2}])
    assert client.request("DELETE", "/api/v1/treatments/t1")[0] == 401
    assert len(store.get_treatments("Ada", 10)) == 1


def test_a_bulk_delete_is_acknowledged_but_changes_nothing(api):
    """Trio issues these on startup; refusing them stalls its uploader."""
    client, store, _server = api
    store.add_treatments("Ada", [{"_id": "t1", "insulin": 2}])
    status, _h, body = client.request("DELETE",
                                      "/api/v1/treatments?find[created_at]=x",
                                      None, AUTH)
    assert status == 200
    assert json.loads(body) == {"n": 0, "ok": 1}
    assert len(store.get_treatments("Ada", 10)) == 1


# -------------------------------------------------------------- synclog ----

def test_a_push_is_recorded_in_the_sync_log(api):
    client, _store, _server = api
    client.request("POST", "/api/v1/entries.json",
                   [{"sgv": 120, "date": 1}], AUTH)
    entry = synclog.recent()[0]
    assert entry["source"] == "push"
    assert entry["user"] == "Ada"
    assert "1 readings" in entry["message"]


# -------------------------------------------------------- start_servers ----

def test_start_servers_opens_one_port_per_push_user(store):
    from glucocube.config import UserConfig

    users = [UserConfig(name="Ada", port=free_port(), api_secret="a"),
             UserConfig(name="Bo", port=free_port(), api_secret="b")]
    servers = start_servers(users, store)
    try:
        assert len(servers) == 2
        for server, user in zip(servers, users):
            assert Client(server.server_address[1]).get("/api/v1/status")[0] == 200
    finally:
        stop_servers(servers)


@pytest.mark.parametrize("source", [
    {"type": "tidepool", "email": "a@example.invalid", "password": "x"},
    {"type": "nightscout", "url": "https://ns.example.invalid"},
])
def test_no_listener_is_opened_for_a_pull_only_person(store, source):
    """They are never shown a port, so nothing should be listening on it."""
    from glucocube.config import UserConfig

    port = free_port()
    servers = start_servers(
        [UserConfig(name="Ada", port=port, api_secret="a", source=source)],
        store)
    try:
        assert servers == []
        with pytest.raises(OSError):
            Client(port).get("/api/v1/status")
    finally:
        stop_servers(servers)


def test_also_push_reopens_the_listener_for_a_pull_person(store):
    from glucocube.config import UserConfig

    users = [UserConfig(name="Ada", port=free_port(), api_secret="a",
                        source={"type": "nightscout",
                                "url": "https://ns.example.invalid",
                                "also_push": True})]
    servers = start_servers(users, store)
    try:
        assert len(servers) == 1
        assert Client(servers[0].server_address[1]).get("/api/v1/status")[0] == 200
    finally:
        stop_servers(servers)


def test_the_server_answers_over_ipv4_and_ipv6(api):
    """mDNS clients often pick the v6 address; a v4-only listener refuses."""
    import socket

    client, _store, server = api
    if server.address_family != socket.AF_INET6:
        pytest.skip("no IPv6 on this machine")
    assert client.get("/api/v1/status")[0] == 200
    assert Client(client.port, host="::1").get("/api/v1/status")[0] == 200


def test_a_second_store_is_not_needed_for_a_second_user(store):
    """One store, two servers, no cross-talk."""
    from glucocube.config import UserConfig

    users = [UserConfig(name="Ada", port=free_port(), api_secret=""),
             UserConfig(name="Bo", port=free_port(), api_secret="")]
    servers = start_servers(users, store)
    try:
        Client(servers[0].server_address[1]).post(
            "/api/v1/entries.json", [{"sgv": 120, "date": 1}])
        assert Client(servers[1].server_address[1]).json(
            "/api/v1/entries.json")[1] == []
        assert store.snapshot("Ada").sgv == 120
    finally:
        stop_servers(servers)
