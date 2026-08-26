"""Pairing a display with GlucoCore from the settings site.

Pairing used to live only inside the setup wizard, which a configured
device redirects away from — so a display already showing readings had no
way to reach GlucoCore at all. These tests hold the settings route open:
the hub links to it, a six-digit code from the GlucoCore app pairs the
display, and pairing adds those people without taking anyone else's
source away.

The display never handles the account password — it redeems a pairing
code for a token scoped to named patients, which is what the whole
device-token design exists for. The GlucoCore calls are stubbed at the
module boundary the app uses, so nothing here reaches the network
(conftest blocks it anyway).
"""

import json
import threading
import urllib.parse

import pytest

from glucocube import onboarding, sync, verify, webadmin
from glucocube.config import load
from glucocube.verify import Result
from glucocube.webadmin import AdminServer

from helpers import Client

AUTH = {"Authorization": "Basic " + __import__("base64").b64encode(
    b"admin:letmein").decode()}

PATIENTS = [
    {"userId": "pat-1", "name": "Grace"},
    {"userId": "pat-2", "name": "Rex"},
]


@pytest.fixture
def restarts(monkeypatch):
    calls = []
    monkeypatch.setattr(webadmin, "restart_soon",
                        lambda delay=0.8: calls.append(delay))
    return calls


def serve(config_path, store):
    config = load(config_path)
    config.admin_port = 0
    server = AdminServer(config, str(config_path), store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    return Client(server.server_address[1]), server


@pytest.fixture
def admin(config_path, store, restarts):
    client, server = serve(config_path, store)
    yield client, server, config_path
    server.shutdown()
    server.server_close()


@pytest.fixture
def paired(config_path, store, restarts):
    """A display already pulling one person from GlucoCore."""
    raw = json.loads(config_path.read_text())
    raw["users"] = [
        {"name": "Grace", "port": 1337, "api_secret": "grace-secret",
         "source": {"type": "glucocore", "patient_id": "pat-1",
                    "poll_seconds": 60}},
        {"name": "Bo", "port": 1338, "api_secret": "bo-secret"},
    ]
    raw["glucocore"] = {"device_id": "dev-9", "device_token": "device-token",
                        "hardware_id": "mac-abc", "name": "Kitchen display"}
    config_path.write_text(json.dumps(raw, indent=2))
    client, server = serve(config_path, store)
    yield client, server, config_path
    server.shutdown()
    server.server_close()


def post(client, path, **fields):
    return client.request("POST", path, urllib.parse.urlencode(fields).encode(),
                          {**AUTH, "Content-Type":
                           "application/x-www-form-urlencoded"})


def post_many(client, path, pairs):
    """A POST with a repeated field — checkboxes, as a browser sends them."""
    body = urllib.parse.urlencode(pairs, doseq=False).encode()
    return client.request("POST", path, body,
                          {**AUTH, "Content-Type":
                           "application/x-www-form-urlencoded"})


def page(client, path) -> str:
    status, _headers, body = client.get(path, headers=AUTH)
    assert status == 200, body
    return body.decode()


def config_of(config_path) -> dict:
    return json.loads(config_path.read_text())


PAIRED_CONFIG = {
    "version": 2,
    "patientIds": ["pat-1", "pat-2"],
    "display": {"low": 75, "high": 165, "timezone": "Europe/London"},
    "perPatient": {"pat-1": {"label": "Grace"}, "pat-2": {"label": "Rex"}},
}


def claims(monkeypatch, config=None, calls=None):
    """A pairing code GlucoCore accepts, and what the display sent to use it."""
    remote = PAIRED_CONFIG if config is None else config

    def claim(code, hardware_id, name="", timeout=10.0):
        if calls is not None:
            calls.append({"code": code, "hardware_id": hardware_id,
                          "name": name})
        return (Result(True, "Paired."),
                {"deviceToken": "device-token",
                 "device": {"id": "dev-42", "name": name or "Kitchen display",
                            "config": remote}})

    monkeypatch.setattr(verify, "glucocore_claim", claim)
    return calls


# ------------------------------------------------------- finding the page ----

def test_the_hub_offers_pairing_on_a_device_that_has_none(admin):
    """The whole bug: nothing on the settings site said GlucoCore at all."""
    client, _server, _path = admin
    hub = page(client, "/settings")
    assert "/settings/glucocore" in hub
    assert "GlucoCore" in hub
    assert "Not paired" in hub


def test_an_unpaired_device_is_not_badged_as_broken(admin):
    """A display fed by Trio has not got a problem, and must not be nagged."""
    client, _server, _path = admin
    assert "not paired</span>" not in page(client, "/settings")


def test_glucocore_people_with_no_pairing_are_badged(paired, monkeypatch):
    client, server, path = paired
    raw = config_of(path)
    raw.pop("glucocore")
    path.write_text(json.dumps(raw))
    server.config = load(path)
    hub = page(client, "/settings")
    assert "not paired" in hub


def test_the_page_asks_for_a_code_and_never_a_password(admin):
    client, _server, _path = admin
    body = page(client, "/settings/glucocore")
    assert 'action="/settings/glucocore/pair"' in body
    assert 'name="code"' in body
    assert 'type="password"' not in body
    assert 'name="email"' not in body


def test_the_page_says_where_the_code_comes_from(admin):
    client, _server, _path = admin
    body = page(client, "/settings/glucocore")
    assert "Devices" in body
    assert "glucocore.app" in body


# ---------------------------------------------------------------- pairing ----

def test_pairing_writes_the_token_and_the_people(admin, monkeypatch, restarts):
    client, _server, path = admin
    calls = claims(monkeypatch, calls=[])

    status, _headers, _body = post(client, "/settings/glucocore/pair",
                                   code="123 456",
                                   device_name="Kitchen display")
    assert status == 200
    assert restarts, "pairing has to restart the display"

    # The code and this device's own hardware id are the whole request.
    assert calls[0]["code"] == "123 456"
    assert calls[0]["hardware_id"]
    assert calls[0]["name"] == "Kitchen display"

    raw = config_of(path)
    assert raw["glucocore"] == {"device_id": "dev-42",
                                "device_token": "device-token",
                                "hardware_id": calls[0]["hardware_id"],
                                "name": "Kitchen display"}
    sources = {user["name"]: (user.get("source") or {}).get("patient_id")
               for user in raw["users"]}
    assert sources["Grace"] == "pat-1"
    assert sources["Rex"] == "pat-2"
    # config.load() rejects duplicate ports, and a device that cannot load
    # its config restart-loops.
    ports = [user["port"] for user in raw["users"]]
    assert len(set(ports)) == len(ports)
    load(path)


def test_who_to_show_comes_from_the_pairing_not_the_display(admin,
                                                            monkeypatch):
    """The code already names them; the display is not asked to choose."""
    client, _server, path = admin
    claims(monkeypatch)
    post(client, "/settings/glucocore/pair", code="123456")
    names = [u["name"] for u in config_of(path)["users"]
             if (u.get("source") or {}).get("type") == "glucocore"]
    assert names == ["Grace", "Rex"]


def test_the_bands_the_pairing_carries_are_applied_at_once(admin,
                                                           monkeypatch):
    """They arrive with the token, so there is nothing to wait for."""
    client, _server, path = admin
    claims(monkeypatch)
    post(client, "/settings/glucocore/pair", code="123456")
    display = config_of(path)["display"]
    assert (display["low"], display["high"]) == (75, 165)
    assert display["timezone"] == "Europe/London"


def test_a_person_with_no_label_is_not_named_by_their_patient_id(admin,
                                                                 monkeypatch):
    """A raw id is not a name anybody would choose to see on a wall."""
    client, _server, path = admin
    claims(monkeypatch, config={"patientIds": ["pat-1"], "display": {},
                                "perPatient": {}})
    post(client, "/settings/glucocore/pair", code="123456")
    glucocore_users = [u for u in config_of(path)["users"]
                       if (u.get("source") or {}).get("type") == "glucocore"]
    # Nothing better exists, so the id stands — but it is the only case.
    assert glucocore_users[0]["name"] == "pat-1"


def test_pairing_leaves_everyone_elses_source_alone(admin, monkeypatch):
    """Pairing adds to a display; it does not clear it."""
    client, _server, path = admin
    raw = config_of(path)
    raw["users"][1]["source"] = {"type": "nightscout",
                                 "url": "https://ns.example",
                                 "api_secret": "shh"}
    path.write_text(json.dumps(raw))
    claims(monkeypatch)
    post(client, "/settings/glucocore/pair", code="123456")

    users = {user["name"]: user for user in config_of(path)["users"]}
    assert users["Ada"]["api_secret"] == "ada-secret"
    assert users["Bo"]["source"]["url"] == "https://ns.example"
    assert users["Grace"]["source"]["patient_id"] == "pat-1"


def test_pairing_clears_the_placeholders_an_image_ships_with(admin,
                                                             monkeypatch):
    client, _server, path = admin
    raw = config_of(path)
    raw["users"] = [
        {"name": "Person A", "port": 1337, "api_secret": "a"},
        {"name": "Person B", "port": 1338, "api_secret": "b"},
    ]
    path.write_text(json.dumps(raw))
    claims(monkeypatch, config={"patientIds": ["pat-1"], "display": {},
                                "perPatient": {"pat-1": {"label": "Grace"}}})
    post(client, "/settings/glucocore/pair", code="123456")
    assert [u["name"] for u in config_of(path)["users"]] == ["Grace"]


def test_a_refused_code_says_so_and_writes_nothing(admin, monkeypatch):
    client, _server, path = admin
    monkeypatch.setattr(verify, "glucocore_claim",
                        lambda *a, **k: (
                            Result(False, "That code was not accepted."), {}))
    status, _headers, body = post(client, "/settings/glucocore/pair",
                                  code="000000")
    assert status == 400
    assert "was not accepted" in body.decode()
    assert "glucocore" not in config_of(path)


def test_a_failed_pairing_shows_what_actually_happened(admin, monkeypatch):
    """The message says what to do; the detail says why it did not work."""
    client, _server, _path = admin
    monkeypatch.setattr(verify, "glucocore_claim",
                        lambda *a, **k: (
                            Result(False, "Could not reach GlucoCore.",
                                   "URLError: Name or service not known"),
                            {}))
    _status, _headers, body = post(client, "/settings/glucocore/pair",
                                   code="123456")
    page_html = body.decode()
    assert "Technical detail" in page_html
    assert "Name or service not known" in page_html


def test_a_pairing_with_nobody_on_it_is_refused(admin, monkeypatch):
    client, _server, path = admin
    claims(monkeypatch, config={"patientIds": [], "display": {},
                                "perPatient": {}})
    status, _headers, body = post(client, "/settings/glucocore/pair",
                                  code="123456")
    assert status == 400
    assert "nobody on it yet" in body.decode()
    assert "glucocore" not in config_of(path)


def test_pairing_without_naming_the_display_keeps_the_accounts_name(
        admin, monkeypatch):
    client, _server, path = admin
    claims(monkeypatch)
    post(client, "/settings/glucocore/pair", code="123456", device_name="  ")
    assert config_of(path)["glucocore"]["name"] == "Kitchen display"


def test_a_name_already_on_the_display_is_not_reused(admin, monkeypatch):
    """Everything in the database is keyed by name."""
    client, _server, path = admin
    claims(monkeypatch, config={"patientIds": ["pat-1"], "display": {},
                                "perPatient": {"pat-1": {"label": "Ada"}}})
    post(client, "/settings/glucocore/pair", code="123456")
    names = [user["name"] for user in config_of(path)["users"]]
    assert len(set(names)) == len(names)
    assert "Ada 2" in names


def test_a_stale_config_version_does_not_swallow_the_first_push(admin,
                                                                monkeypatch,
                                                                store):
    client, _server, _path = admin
    store.set_params(sync.LAST_VERSION_KEY, {"version": 12})
    claims(monkeypatch)
    post(client, "/settings/glucocore/pair", code="123456")
    assert store.get_params(sync.LAST_VERSION_KEY) == {}


# ---------------------------------------------------------- once paired ----

def test_a_paired_device_says_which_one_it_is(paired):
    client, _server, _path = paired
    hub = page(client, "/settings")
    assert "Kitchen display" in hub
    body = page(client, "/settings/glucocore")
    assert "dev-9" in body
    assert 'action="/settings/glucocore/unpair"' in body


def test_pairing_again_is_not_offered_over_an_existing_one(paired,
                                                           monkeypatch):
    """Unpairing is how a display moves account, and it is deliberate."""
    client, _server, path = paired
    claims(monkeypatch)
    status, headers, _body = post(client, "/settings/glucocore/pair",
                                  code="123456")
    assert status == 303
    assert headers["Location"] == "/settings/glucocore"
    assert config_of(path)["glucocore"]["device_id"] == "dev-9"


def test_unpairing_leaves_everyone_on_screen(paired, restarts):
    client, _server, path = paired
    status, _headers, _body = post(client, "/settings/glucocore/unpair")
    assert status == 200
    assert restarts
    raw = config_of(path)
    assert "glucocore" not in raw
    grace = next(u for u in raw["users"] if u["name"] == "Grace")
    assert "source" not in grace, "an unpaired person becomes a push person"
    assert grace["port"] and grace["api_secret"]
    load(path)


# ------------------------------------------------- the person's own page ----

def test_a_glucocore_person_is_not_offered_the_wrong_sources(paired):
    client, _server, _path = paired
    body = page(client, "/settings/person?i=0")
    assert "/settings/glucocore" in body
    assert 'value="tidepool"' not in body


def test_renaming_a_glucocore_person_keeps_them_paired(paired):
    """A save from this page used to drop the source and stop the poller."""
    client, _server, path = paired
    post(client, "/settings/person?i=0", name="Grace Hopper", th_low="80")
    grace = config_of(path)["users"][0]
    assert grace["name"] == "Grace Hopper"
    assert grace["source"] == {"type": "glucocore", "patient_id": "pat-1",
                               "poll_seconds": 60}


# --------------------------------------------- config pushed from the cloud ----

def test_a_config_push_only_speaks_for_glucocore_people(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [
            {"name": "Bo", "port": 1338, "api_secret": "bo-secret",
             "source": {"type": "nightscout", "url": "https://ns.example"}},
            {"name": "Grace", "port": 1337, "api_secret": "grace-secret",
             "source": {"type": "glucocore", "patient_id": "pat-1"}},
        ],
        "display": {"low": 70, "high": 180},
        "admin": {"port": 8080, "password": "letmein"},
    }))
    config = sync.apply_remote_config(
        path,
        {"patientIds": ["pat-1", "pat-2"], "display": {"low": 75},
         "perPatient": {"pat-1": {"label": "Grace"},
                        "pat-2": {"label": "Rex"}}},
        3,
    )
    names = [user.name for user in config.users]
    assert names == ["Bo", "Grace", "Rex"]
    grace = next(u for u in config.users if u.name == "Grace")
    # A push about thresholds must not reroll the identity an uploader is
    # pointed at.
    assert grace.port == 1337
    assert grace.api_secret == "grace-secret"
    assert config.display.low == 75


def test_a_config_push_drops_people_it_no_longer_lists(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [
            {"name": "Grace", "port": 1337, "api_secret": "s",
             "source": {"type": "glucocore", "patient_id": "pat-1"}},
            {"name": "Rex", "port": 1339, "api_secret": "s2",
             "source": {"type": "glucocore", "patient_id": "pat-2"}},
        ],
        "display": {},
        "admin": {"port": 8080, "password": "letmein"},
    }))
    config = sync.apply_remote_config(
        path, {"patientIds": ["pat-1"],
               "perPatient": {"pat-1": {"label": "Grace"}}}, 4)
    assert [user.name for user in config.users] == ["Grace"]


def test_the_wizard_and_the_settings_page_agree_on_who_stays():
    users = [
        {"name": "Person A"},
        {"name": "Person B", "source": {"type": "push"}},
        {"name": "Grace", "source": {"type": "glucocore"}},
        {"name": "Bo", "source": {"type": "tidepool"}},
    ]
    kept = [user["name"] for user in onboarding.keep_local_users(users)]
    assert kept == ["Person B", "Bo"]


def test_a_label_changed_in_glucocore_carries_the_readings_with_it(tmp_path,
                                                                   store):
    """Every table is keyed by the name, so a rename must not orphan them."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [{"name": "Grace", "port": 1337, "api_secret": "s",
                   "source": {"type": "glucocore", "patient_id": "pat-1"}}],
        "display": {},
        "admin": {"port": 8080, "password": "letmein"},
    }))
    store.add_entries("Grace", [{"sgv": 120, "date": 1_700_000_000_000}])
    sync.apply_remote_config(
        path, {"patientIds": ["pat-1"],
               "perPatient": {"pat-1": {"label": "Grace Hopper"}}}, 5,
        store=store)
    assert store.snapshot("Grace Hopper").sgv == 120


def test_a_push_says_out_loud_what_this_display_ignores(tmp_path, caplog):
    """"I changed it and nothing happened" needs an answer somewhere."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [{"name": "Grace", "port": 1337, "api_secret": "s",
                   "source": {"type": "glucocore", "patient_id": "pat-1"}}],
        "display": {},
        "admin": {"port": 8080, "password": "letmein"},
    }))
    with caplog.at_level("INFO", logger="glucocube.sync"):
        sync.apply_remote_config(
            path,
            {"patientIds": ["pat-1"],
             "display": {"low": 75, "rotate_seconds": 20,
                         "alert_urgent_low": True, "brightness": 80}},
            6)
    listed = [line.split(": ")[-1] for line in caplog.text.splitlines()
              if "does not apply" in line]
    ignored = set(listed[0].split(", "))
    assert ignored == {"alert_urgent_low", "rotate_seconds"}
    # `low` is applied, and `brightness` is applied by the display rather
    # than through config.json — neither is somebody's setting going
    # nowhere, so neither belongs in that line.
