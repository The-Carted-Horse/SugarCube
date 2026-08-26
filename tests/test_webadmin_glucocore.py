"""Pairing a display with GlucoCore from the settings site.

Pairing used to live only inside the setup wizard, which a configured
device redirects away from — so a display already showing readings had no
way to reach GlucoCore at all. These tests hold the settings route open:
the hub links to it, a sign-in leads to a choice of people, and pairing
adds those people without taking anyone else's source away.

The GlucoCore calls are stubbed at the module boundary the app uses, so
nothing here reaches the network (conftest blocks it anyway).
"""

import json
import threading
import urllib.parse

import pytest

from glucocube import glucocore, onboarding, sync, verify, webadmin
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


def signed_in(monkeypatch, patients=PATIENTS):
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda email, password, timeout=10.0: (
                            Result(True, "Signed in."),
                            {"token": "session-token", "userid": "u-1",
                             "patients": patients}))


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


def test_the_page_asks_for_the_account_first(admin):
    client, _server, _path = admin
    body = page(client, "/settings/glucocore")
    assert 'action="/settings/glucocore/signin"' in body
    assert 'name="email"' in body and 'name="password"' in body


# ------------------------------------------------------------- signing in ----

def test_a_refused_sign_in_says_so_and_keeps_nothing(admin, monkeypatch):
    client, _server, _path = admin
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda *a, **k: (
                            Result(False, "That email or password did not "
                                          "work."), {}))
    status, _headers, body = post(client, "/settings/glucocore/signin",
                                  email="a@b.c", password="nope")
    assert status == 400
    assert "did not work" in body.decode()
    assert page(client, "/settings/glucocore").count(
        'action="/settings/glucocore/signin"') == 1


def test_a_failed_sign_in_shows_what_actually_happened(admin, monkeypatch):
    """The message says what to do; the detail says why it did not work."""
    client, _server, _path = admin
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda *a, **k: (
                            Result(False, "Could not reach GlucoCore.",
                                   "URLError: Name or service not known"),
                            {}))
    _status, _headers, body = post(client, "/settings/glucocore/signin",
                                   email="a@b.c", password="pw")
    page_html = body.decode()
    assert "Could not reach GlucoCore." in page_html
    assert "Technical detail" in page_html
    assert "Name or service not known" in page_html


def test_signing_in_leads_to_a_choice_of_people(admin, monkeypatch):
    client, _server, path = admin
    signed_in(monkeypatch)
    status, headers, _body = post(client, "/settings/glucocore/signin",
                                  email="a@b.c", password="hunter2")
    assert status == 303
    assert headers["Location"] == "/settings/glucocore"
    # Nothing is written until the pairing itself.
    assert "glucocore" not in config_of(path)
    body = page(client, "/settings/glucocore")
    assert "Grace" in body and "Rex" in body
    assert 'action="/settings/glucocore/pair"' in body


def test_an_expired_sign_in_is_not_offered(admin, monkeypatch, store):
    client, _server, _path = admin
    signed_in(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    draft = store.get_params(webadmin.AdminHandler.PAIR_KEY)
    draft["started_at"] -= webadmin.AdminHandler.PAIR_TTL_MS + 1000
    store.replace_params(webadmin.AdminHandler.PAIR_KEY, draft)
    assert 'action="/settings/glucocore/signin"' in page(client,
                                                         "/settings/glucocore")


def test_discarding_a_sign_in_drops_the_token(admin, monkeypatch, store):
    client, _server, _path = admin
    signed_in(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/cancel")
    assert not store.get_params(webadmin.AdminHandler.PAIR_KEY).get("token")


# ---------------------------------------------------------------- pairing ----

def register(monkeypatch, calls=None):
    def fake(token, name, hardware_id, patient_ids, config=None, timeout=60):
        if calls is not None:
            calls.append({"token": token, "name": name,
                          "hardware_id": hardware_id,
                          "patient_ids": list(patient_ids), "config": config})
        return {"deviceToken": "device-token", "device": {"id": "dev-42"}}

    monkeypatch.setattr(glucocore, "register_device", fake)


def test_pairing_writes_the_token_and_the_people(admin, monkeypatch, restarts):
    client, _server, path = admin
    signed_in(monkeypatch)
    calls = []
    register(monkeypatch, calls)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")

    status, _headers, _body = post_many(client, "/settings/glucocore/pair", [
        ("device_name", "Kitchen display"),
        ("patient_ids", "pat-1"),
        ("patient_ids", "pat-2"),
    ])
    assert status == 200
    assert restarts, "pairing has to restart the display"

    raw = config_of(path)
    assert raw["glucocore"] == {"device_id": "dev-42",
                                "device_token": "device-token",
                                "hardware_id": calls[0]["hardware_id"],
                                "name": "Kitchen display"}
    sources = {user["name"]: (user.get("source") or {}).get("patient_id")
               for user in raw["users"]}
    assert sources["Grace"] == "pat-1"
    assert sources["Rex"] == "pat-2"
    assert calls[0]["patient_ids"] == ["pat-1", "pat-2"]
    # config.load() rejects duplicate ports, and a device that cannot load
    # its config restart-loops.
    ports = [user["port"] for user in raw["users"]]
    assert len(set(ports)) == len(ports)
    load(path)


def test_pairing_leaves_everyone_elses_source_alone(admin, monkeypatch):
    """Pairing adds to a display; it does not clear it."""
    client, _server, path = admin
    raw = config_of(path)
    raw["users"][1]["source"] = {"type": "nightscout",
                                 "url": "https://ns.example",
                                 "api_secret": "shh"}
    path.write_text(json.dumps(raw))
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/pair", device_name="Hall",
         patient_ids="pat-1")

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
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/pair", device_name="Hall",
         patient_ids="pat-1")
    assert [u["name"] for u in config_of(path)["users"]] == ["Grace"]


def test_a_patient_the_account_was_not_shown_is_refused(admin, monkeypatch):
    client, _server, path = admin
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    status, _headers, body = post(client, "/settings/glucocore/pair",
                                  device_name="Hall",
                                  patient_ids="somebody-elses-id")
    assert status == 400
    assert "at least one person" in body.decode()
    assert "glucocore" not in config_of(path)


def test_pairing_needs_a_name_for_the_display(admin, monkeypatch):
    client, _server, path = admin
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    status, _headers, body = post(client, "/settings/glucocore/pair",
                                  device_name="  ", patient_ids="pat-1")
    assert status == 400
    assert "name" in body.decode().lower()
    assert "glucocore" not in config_of(path)


def test_a_name_already_on_the_display_is_not_reused(admin, monkeypatch):
    """Everything in the database is keyed by name."""
    client, _server, path = admin
    signed_in(monkeypatch, [{"userId": "pat-1", "name": "Ada"}])
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/pair", device_name="Hall",
         patient_ids="pat-1")
    names = [user["name"] for user in config_of(path)["users"]]
    assert len(set(names)) == len(names)
    assert "Ada 2" in names


def test_a_failed_registration_changes_nothing(admin, monkeypatch, restarts):
    client, _server, path = admin
    signed_in(monkeypatch)
    monkeypatch.setattr(glucocore, "register_device",
                        lambda *a, **k: {"device": {"id": "x"}})
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    status, _headers, body = post(client, "/settings/glucocore/pair",
                                  device_name="Hall", patient_ids="pat-1")
    assert status == 502
    assert "Could not register" in body.decode()
    assert "glucocore" not in config_of(path)
    assert not restarts


def test_pairing_forgets_the_account_session(admin, monkeypatch, store):
    """The device keeps its own read-only token, and nothing else."""
    client, _server, _path = admin
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/pair", device_name="Hall",
         patient_ids="pat-1")
    assert store.get_params(webadmin.AdminHandler.PAIR_KEY) == {}


def test_a_stale_config_version_does_not_swallow_the_first_push(admin,
                                                                monkeypatch,
                                                                store):
    client, _server, _path = admin
    store.set_params(sync.LAST_VERSION_KEY, {"version": 12})
    signed_in(monkeypatch)
    register(monkeypatch)
    post(client, "/settings/glucocore/signin", email="a@b.c", password="pw")
    post(client, "/settings/glucocore/pair", device_name="Hall",
         patient_ids="pat-1")
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
    client, _server, path = paired
    signed_in(monkeypatch)
    status, headers, _body = post(client, "/settings/glucocore/signin",
                                  email="a@b.c", password="pw")
    assert status == 303
    assert headers["Location"] == "/settings/glucocore"
    assert config_of(path)["glucocore"]["device_token"] == "device-token"


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
        path, {"patientIds": ["pat-1", "pat-2"], "display": {"low": 75}}, 3,
        patient_names={"pat-1": "Grace", "pat-2": "Rex"},
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
        path, {"patientIds": ["pat-1"]}, 4, patient_names={"pat-1": "Grace"})
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
