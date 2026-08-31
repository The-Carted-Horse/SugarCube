"""onboarding.py — the setup wizard, as a state machine and end to end.

Two things are worth more than the individual screens. First, the step
list is dynamic — Wi-Fi only when there is none, an email-verification
step only for an account being created here — so "what comes next" has to
hold for every shape of draft. Second, nothing is written to config.json
until the final commit: a device abandoned halfway through setup must
still boot the way it did before.

The wizard signs in to GlucoCore and registers the display, so every
GlucoCore call is stubbed at the module boundary the wizard uses.
Conftest blocks the network underneath, which is what turns a forgotten
stub into a failure rather than a real login attempt.
"""

import json
import threading
import urllib.parse

import pytest

from glucocube import network, onboarding, verify
from glucocube import webadmin
from glucocube.config import load
from glucocube.onboarding import (
    current_step,
    keep_local_users,
    load_draft,
    mark_done,
    next_step,
    path_for,
    reconcile_wifi,
    save_draft,
    seed_draft,
    steps_for,
    wifi_needed,
)
from glucocube.verify import Result
from glucocube.webadmin import AdminServer

from helpers import Client

PATIENTS = [
    {"userId": "pat-1", "name": "Ada"},
    {"userId": "pat-2", "name": "Bo"},
]

PAIRED_CONFIG = {
    "version": 2,
    "patientIds": ["pat-1", "pat-2"],
    "display": {"low": 75, "high": 165},
    "perPatient": {"pat-1": {"label": "Ada"}, "pat-2": {"label": "Bo"}},
}


def draft_with(done=(), wifi=None) -> dict:
    return {
        "version": onboarding.DRAFT_VERSION,
        "started_at": 0,
        "done": list(done),
        "wifi": wifi or {},
        "device_name": "",
        "pairing": {},
        "display": {},
        "admin_password": "",
        "admin_password_off": False,
        "committed_at": None,
    }


# ----------------------------------------------------------- the step list ----

def test_the_steps_for_a_device_already_online():
    assert steps_for(draft_with()) == [
        "welcome", "timezone", "pair", "thresholds", "password", "review"]


def test_wifi_is_asked_about_only_when_the_device_has_none(monkeypatch):
    """The hotspot being up is the cheap proxy for "no network"."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert "wifi" in steps_for(draft_with())
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: False)
    assert "wifi" not in steps_for(draft_with())


def test_a_skipped_wifi_step_does_not_come_back(monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert wifi_needed(draft_with(wifi={"skipped": True})) is False


def test_wifi_comes_before_anything_that_needs_the_internet(monkeypatch):
    """Redeeming a code over no network at all is not a question."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    steps = steps_for(draft_with())
    assert steps.index("wifi") < steps.index("pair")


def test_the_time_zone_is_asked_before_anything_that_shows_a_time():
    steps = steps_for(draft_with())
    assert steps.index("timezone") < steps.index("thresholds")


def test_the_current_step_is_the_first_one_not_done():
    draft = draft_with(done=["welcome", "timezone"])
    assert current_step(draft) == "pair"


def test_a_finished_draft_rests_on_review():
    draft = draft_with(done=steps_for(draft_with()))
    assert current_step(draft) == "review"


@pytest.mark.parametrize("step, expected", [
    ("welcome", "timezone"),
    ("timezone", "pair"),
    ("pair", "thresholds"),
    ("thresholds", "password"),
    ("password", "review"),
    ("review", "review"),
])
def test_next_step_walks_the_list(step, expected):
    assert next_step(draft_with(), step) == expected


def test_next_step_from_an_unknown_step_recovers():
    """A stale bookmark must not become a dead end."""
    assert next_step(draft_with(), "nonsense") == "welcome"


@pytest.mark.parametrize("step", sorted(onboarding.RENDERERS))
def test_a_step_maps_to_its_url(step):
    assert path_for(step) == f"/setup/{step}"


def test_every_step_has_a_screen_and_a_title():
    """The three lists that describe the wizard cannot drift apart."""
    for step in steps_for(draft_with()):
        assert step in onboarding.RENDERERS
        assert step in onboarding.TITLES


def test_marking_a_step_done_is_idempotent():
    draft = draft_with()
    mark_done(draft, "welcome")
    mark_done(draft, "welcome")
    assert draft["done"] == ["welcome"]


# ---------------------------------------------------------------- drafts ----

def test_a_draft_survives_a_restart(store):
    save_draft(store, draft_with(done=["welcome"]))
    assert load_draft(store)["done"] == ["welcome"]


def test_a_committed_draft_reads_as_nothing_in_progress(store):
    """A stale QR code must not drop a working device back at step one."""
    save_draft(store, {"version": onboarding.DRAFT_VERSION,
                       "committed_at": 123})
    assert load_draft(store) == {}


def test_a_draft_from_an_older_version_is_discarded(store):
    save_draft(store, {**draft_with(), "version": "ancient"})
    assert load_draft(store) == {}


def test_saving_a_draft_replaces_rather_than_merges(store):
    """set_params drops falsy values, so a cleared flag would not stick."""
    save_draft(store, draft_with(done=["welcome"], wifi={"pending": True}))
    save_draft(store, draft_with(done=[], wifi={}))
    assert load_draft(store)["done"] == []
    assert load_draft(store)["wifi"] == {}


def test_seeding_carries_over_the_ranges_already_set(config_path):
    """Re-running setup should not re-ask what the device already knows."""
    draft = seed_draft(str(config_path))
    assert draft["display"]["low"] == 70
    assert draft["display"]["high"] == 180


def test_seeding_starts_with_nothing_paired(config_path):
    """Who appears comes from GlucoCore, not from what was on the device."""
    draft = seed_draft(str(config_path))
    assert draft["pairing"] == {}
    assert draft["committed_at"] is None


def test_seeding_without_a_config_still_produces_a_usable_draft(tmp_path):
    draft = seed_draft(str(tmp_path / "missing.json"))
    assert draft["display"] == {}
    assert draft["done"] == []


# --------------------------------------------------- who a pairing replaces ----

def test_people_fed_by_an_uploader_survive_setup():
    """Setup pairs a display with GlucoCore; it does not clear it."""
    users = [
        {"name": "Person A"},
        {"name": "Person B", "source": {"type": "push"}},
        {"name": "Grace", "source": {"type": "glucocore"}},
        {"name": "Bo", "source": {"type": "tidepool"}},
    ]
    assert [u["name"] for u in keep_local_users(users)] == ["Person B", "Bo"]


# ------------------------------------------------------- wifi reconciling ----

def test_a_join_that_succeeded_over_the_reboot_is_settled(store, monkeypatch):
    monkeypatch.setattr(network, "state",
                        lambda: {"state": "ok", "ssid": "Home"})
    draft = reconcile_wifi(draft_with(wifi={"pending": True}))
    assert draft["wifi"]["pending"] is False
    assert draft["wifi"]["joined_ssid"] == "Home"
    assert "wifi" in draft["done"]


def test_a_join_that_failed_reports_why(store, monkeypatch):
    monkeypatch.setattr(network, "state",
                        lambda: {"state": "failed",
                                 "error": "wrong Wi-Fi password"})
    draft = reconcile_wifi(draft_with(wifi={"pending": True}))
    assert draft["wifi"]["pending"] is False
    assert draft["wifi"]["error"] == "wrong Wi-Fi password"
    assert "wifi" not in draft["done"]


def test_a_join_still_in_flight_is_left_pending(monkeypatch):
    monkeypatch.setattr(network, "state", lambda: {"state": "joining"})
    draft = reconcile_wifi(draft_with(wifi={"pending": True}))
    assert draft["wifi"]["pending"] is True


def test_nothing_to_reconcile_leaves_the_draft_alone():
    draft = draft_with()
    assert reconcile_wifi(draft) is draft


# -------------------------------------------------------------- routing ----

@pytest.mark.parametrize("path, handled", [
    ("/setup", True), ("/setup/welcome", True), ("/setup/account", True),
    ("/settings", False), ("/", False), ("/setupx", False),
])
def test_which_paths_the_wizard_owns(path, handled):
    assert onboarding.handles(path) is handled


def test_setup_is_open_without_a_login_only_on_the_hotspot(monkeypatch):
    """WPA2 is the boundary there; a captive browser has no cookie."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert onboarding.open_without_login("/setup/wifi") is True
    assert onboarding.open_without_login("/settings") is False
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: False)
    assert onboarding.open_without_login("/setup/wifi") is False


# ---------------------------------------------------------- end to end ----

AUTH = {"Authorization": "Basic " + __import__("base64").b64encode(
    b"admin:letmein").decode()}


@pytest.fixture
def wizard(tmp_path, store, monkeypatch):
    """An admin server on a device that has not been set up yet."""
    monkeypatch.setattr(webadmin, "restart_soon", lambda delay=0.8: None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "users": [{"name": "Person A", "port": 1337, "api_secret": ""},
                  {"name": "Person B", "port": 1338, "api_secret": ""}],
        "admin": {"port": 80, "password": "letmein"},
    }))
    config = load(path)
    config.admin_port = 0
    server = AdminServer(config, str(path), store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    yield Client(server.server_address[1]), server, path
    server.shutdown()
    server.server_close()


@pytest.fixture
def pairs(monkeypatch):
    """A pairing code GlucoCore accepts, recording what the display sent."""
    calls = []

    def claim(code, hardware_id, name="", timeout=10.0):
        calls.append({"code": code, "hardware_id": hardware_id, "name": name})
        return (Result(True, "Paired."),
                {"deviceToken": "device-token",
                 "device": {"id": "dev-42", "name": name or "Kitchen display",
                            "config": PAIRED_CONFIG}})

    monkeypatch.setattr(verify, "glucocore_claim", claim)
    return calls


def step(client, path, **fields):
    return client.request(
        "POST", path, urllib.parse.urlencode(fields).encode(),
        {**AUTH, "Content-Type": "application/x-www-form-urlencoded"})


def step_many(client, path, pairs):
    """A POST with a repeated field — checkboxes, as a browser sends them."""
    return client.request(
        "POST", path, urllib.parse.urlencode(pairs).encode(),
        {**AUTH, "Content-Type": "application/x-www-form-urlencoded"})


def _through_to_the_password_step(client):
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/pair", code="123456", device_name="Kitchen display")
    step(client, "/setup/thresholds")


def test_setup_starts_at_the_first_step(wizard):
    client, _server, _path = wizard
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert status == 303
    assert headers["Location"] == "/setup/welcome"


@pytest.mark.parametrize("path", ["/setup/welcome", "/setup/timezone",
                                  "/setup/pair", "/setup/thresholds",
                                  "/setup/password", "/setup/review"])
def test_every_screen_renders(wizard, path):
    client, _server, _path = wizard
    status, _headers, body = client.get(path, headers=AUTH)
    assert status == 200
    assert body.startswith(b"<!DOCTYPE html>")


def test_an_unknown_step_returns_to_the_start(wizard):
    client, _server, _path = wizard
    status, headers, _body = client.get("/setup/nonsense", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/setup")


def test_walking_the_whole_wizard_writes_one_config_at_the_end(
        wizard, store, pairs):
    client, _server, path = wizard
    before = path.read_text()

    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="Europe/London")
    step(client, "/setup/pair", code="123456", device_name="Kitchen display")

    # Nothing is written until the last step — the pairing included. A code
    # is spent by then, but the device it belongs to is still only a draft.
    assert path.read_text() == before

    step(client, "/setup/thresholds", low="80", high="160")
    step(client, "/setup/password", admin_password="newpassword")
    status, _headers, body = step(client, "/setup/review")

    assert status == 200
    assert b"GlucoCube" in body
    config = load(path)
    assert [u.name for u in config.users] == ["Ada", "Bo"]
    assert config.users[0].source["patient_id"] == "pat-1"
    assert config.glucocore.device_token == "device-token"
    assert config.glucocore.name == "Kitchen display"
    assert config.display.timezone == "Europe/London"
    assert (config.display.low, config.display.high) == (80, 160)
    assert config.admin_password == "newpassword"
    # The draft is left as a tombstone, without the token in it.
    assert load_draft(store) == {}


def test_the_code_and_this_device_are_the_whole_request(wizard, pairs):
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    assert len(pairs) == 1
    sent = pairs[0]
    assert sent["code"] == "123456"
    assert sent["hardware_id"]
    assert sent["name"] == "Kitchen display"


def test_the_wizard_offers_all_three_ways_in(wizard):
    client, _server, _path = wizard
    _status, _headers, body = client.get("/setup/pair", headers=AUTH)
    assert b'value="qr"' in body
    assert b'name="code"' in body
    assert b'name="email"' in body


def test_scanning_is_what_the_wizard_opens_on(wizard):
    """The one that needs no typing at all leads."""
    client, _server, _path = wizard
    _status, _headers, body = client.get("/setup/pair", headers=AUTH)
    assert b'value="qr" checked' in body


def test_the_wizard_shows_the_request_the_display_is_waiting_on(wizard, store):
    from glucocube import pairing
    client, _server, _path = wizard
    store.replace_params(pairing.STATE_KEY, {
        "request_id": "req-1", "secret": "never-rendered",
        "approve_url": "https://www.glucocore.app/devices/add?request=req-1",
        "expires_at": 0, "error": ""})
    _status, _headers, body = client.get("/setup/pair", headers=AUTH)
    assert b"<svg" in body
    assert b"never-rendered" not in body


def test_a_display_scanned_mid_wizard_moves_the_wizard_on(wizard, store):
    """Somebody walked up to the wall while this page was open."""
    client, server, path = wizard
    raw = json.loads(path.read_text())
    raw["glucocore"] = {"device_id": "dev-9", "device_token": "device-token"}
    path.write_text(json.dumps(raw))
    server.config = load(path)

    status, headers, _body = client.get("/setup/pair", headers=AUTH)
    assert status == 303
    assert headers["Location"] == "/setup/thresholds"


def test_a_display_scanned_mid_wizard_still_finishes(wizard, store):
    """The remaining answers are saved; the pairing is already written."""
    client, server, path = wizard
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="Europe/London")

    raw = json.loads(path.read_text())
    raw["glucocore"] = {"device_id": "dev-9", "device_token": "device-token"}
    raw["users"] = [{"name": "Grace", "port": 1337, "api_secret": "s",
                     "source": {"type": "glucocore", "patient_id": "pat-1"}}]
    path.write_text(json.dumps(raw))
    server.config = load(path)

    client.get("/setup/pair", headers=AUTH)      # notices, marks it done
    step(client, "/setup/thresholds", low="80", high="160")
    step(client, "/setup/password", admin_password="newpassword")
    status, _headers, _body = step(client, "/setup/review")

    assert status == 200
    config = load(path)
    assert config.glucocore.device_token == "device-token"
    assert [u.name for u in config.users] == ["Grace"]
    assert (config.display.low, config.display.high) == (80, 160)
    assert config.admin_password == "newpassword"


# ------------------------------------------------------ signing in here ----

def test_signing_in_asks_who_to_show_next(wizard, store, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda email, password, timeout=10.0: (
                            Result(True, "Signed in."),
                            {"token": "session-token", "userid": "u-1",
                             "patients": PATIENTS}))
    status, headers, _body = step(client, "/setup/pair", how="signin",
                                  email="c@example.invalid", password="pw")
    assert (status, headers["Location"]) == (303, "/setup/people")
    assert "people" in steps_for(load_draft(store))
    _status, _headers, body = client.get("/setup/people", headers=AUTH)
    assert b"Ada" in body and b"Bo" in body


def test_registering_from_the_wizard_pairs_the_display(wizard, store,
                                                       monkeypatch):
    client, _server, path = wizard
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda *a, **k: (
                            Result(True, "Signed in."),
                            {"token": "session-token", "userid": "u-1",
                             "patients": PATIENTS}))
    calls = []

    def register(token, name, hardware_id, patient_ids, display=None,
                 timeout=10.0):
        calls.append({"token": token, "patient_ids": list(patient_ids)})
        return (Result(True, "Paired."),
                {"deviceToken": "device-token",
                 "device": {"id": "dev-42", "name": name,
                            "config": PAIRED_CONFIG}})

    monkeypatch.setattr(verify, "glucocore_register", register)
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/pair", how="signin", email="c@example.invalid",
         password="pw")
    step(client, "/setup/people", patient_ids="pat-1",
         device_name="Kitchen display")
    step(client, "/setup/thresholds")
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    assert calls[0]["token"] == "session-token"
    config = load(path)
    assert config.glucocore.device_token == "device-token"
    assert [u.name for u in config.users] == ["Ada", "Bo"]


def test_the_account_session_does_not_outlive_the_step_that_used_it(
        wizard, store, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda *a, **k: (
                            Result(True, "Signed in."),
                            {"token": "session-token", "userid": "u-1",
                             "patients": PATIENTS}))
    monkeypatch.setattr(verify, "glucocore_register",
                        lambda *a, **k: (
                            Result(True, "Paired."),
                            {"deviceToken": "device-token",
                             "device": {"id": "dev-42", "name": "Hall",
                                        "config": PAIRED_CONFIG}}))
    step(client, "/setup/pair", how="signin", email="c@example.invalid",
         password="pw")
    assert "session-token" in json.dumps(load_draft(store))
    step(client, "/setup/people", patient_ids="pat-1")
    assert "session-token" not in json.dumps(load_draft(store))


def test_choosing_nobody_from_the_signed_in_list_is_refused(wizard,
                                                            monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_session",
                        lambda *a, **k: (
                            Result(True, "Signed in."),
                            {"token": "session-token", "userid": "u-1",
                             "patients": PATIENTS}))
    step(client, "/setup/pair", how="signin", email="c@example.invalid",
         password="pw")
    status, _headers, body = step(client, "/setup/people",
                                  patient_ids="somebody-else")
    assert status == 400
    assert b"at least one person" in body


def test_two_people_each_get_their_own_panel_and_port(wizard, pairs):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    config = load(path)
    assert [u.name for u in config.users] == ["Ada", "Bo"]
    assert config.users[0].port != config.users[1].port


def test_the_placeholders_an_image_ships_with_do_not_survive(wizard, pairs):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert [u.name for u in load(path).users] == ["Ada", "Bo"]


def test_a_person_fed_by_an_uploader_is_left_alone(wizard, pairs):
    client, server, path = wizard
    raw = json.loads(path.read_text())
    raw["users"] = [{"name": "Cass", "port": 1337, "api_secret": "cass-secret",
                     "source": {"type": "nightscout",
                                "url": "https://ns.example"}}]
    path.write_text(json.dumps(raw))
    server.config = load(path)

    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    users = {u.name: u for u in load(path).users}
    assert users["Cass"].source["url"] == "https://ns.example"
    assert users["Cass"].api_secret == "cass-secret"
    assert users["Ada"].source["patient_id"] == "pat-1"


# ------------------------------------------------------------- pairing ----

def test_the_bands_the_pairing_carries_fill_in_the_next_question(wizard,
                                                                 pairs):
    """The ranges step opens on what GlucoCore already holds for it."""
    client, _server, _path = wizard
    step(client, "/setup/pair", code="123456")
    _status, _headers, body = client.get("/setup/thresholds", headers=AUTH)
    assert b'value="75"' in body
    assert b'value="165"' in body


def test_a_refused_code_stays_on_the_same_step(wizard, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_claim",
                        lambda *a, **k: (
                            Result(False, "That code was not accepted.",
                                   "HTTPError: 400"), {}))
    status, _headers, body = step(client, "/setup/pair", code="000000")
    assert status == 400
    assert b"was not accepted" in body
    # The detail says why, the way the settings page does.
    assert b"Technical detail" in body


def test_a_pairing_with_nobody_on_it_is_refused(wizard, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_claim",
                        lambda *a, **k: (
                            Result(True, "Paired."),
                            {"deviceToken": "device-token",
                             "device": {"id": "dev-42",
                                        "config": {"patientIds": []}}}))
    status, _headers, body = step(client, "/setup/pair", code="123456")
    assert status == 400
    assert b"nobody on it yet" in body


def test_a_code_is_not_spent_twice(wizard, pairs):
    """It is redeemed once, at its own step; review reads what came back."""
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert len(pairs) == 1


def test_a_review_with_nothing_paired_writes_nothing(wizard, pairs):
    """Straight to the last step without ever entering a code."""
    client, _server, path = wizard
    before = path.read_text()
    status, _headers, body = step(client, "/setup/review")
    assert status == 400
    assert b"not paired yet" in body
    assert path.read_text() == before
    assert not pairs


# ------------------------------------------------------------ the clock ----

def test_a_time_zone_the_device_does_not_know_asks_again(wizard):
    """The phone reported it; that is the browser's doing, not the user's."""
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/timezone", tzmode="phone",
                                  tz_detected="Mars/Olympus")
    assert status == 200
    assert b"does not know a zone called" in body


# --------------------------------------------------------- the password ----

def test_a_short_password_is_refused(wizard):
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/password",
                                  admin_password="short")
    assert status == 400
    assert b"at least 6 characters" in body


def test_a_blank_password_keeps_the_one_in_use(wizard, pairs):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert load(path).admin_password == "letmein"


# ------------------------------------------------ finishing with no password ----
#
# Someone whose device is only reachable from a network they trust can say
# so here rather than being handed a password they then have to look up.

def test_choosing_no_password_finishes_without_one(wizard, pairs):
    client, server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", mode="off")
    status, _headers, body = step(client, "/setup/review")
    assert status == 200
    config = load(path)
    assert config.admin_password == ""
    assert config.admin_password_off is True
    assert b"There is no password" in body
    # The live config follows the file, so the page served next — and the
    # device's own screen — stop offering a login that no longer exists.
    assert server.config.admin_password == ""


def test_the_password_step_remembers_which_way_it_was_answered(wizard):
    client, _server, _path = wizard
    step(client, "/setup/password", mode="off")
    _status, _headers, body = client.get("/setup/password", headers=AUTH)
    assert b'value="off" checked' in body


def test_choosing_a_password_after_no_password_drops_the_flag(wizard,
                                                              pairs):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", mode="off")
    step(client, "/setup/password", mode="on", admin_password="sup3rsecret")
    step(client, "/setup/review")
    config = load(path)
    assert config.admin_password == "sup3rsecret"
    assert config.admin_password_off is False
    assert "password_off" not in json.loads(path.read_text())["admin"]


def test_a_short_password_comes_back_on_the_card_it_was_sent_from(wizard):
    """Otherwise the error page silently flips the answer back to "on"."""
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/password", mode="on",
                                  admin_password="short")
    assert status == 400
    assert b'value="on" checked' in body


def test_the_review_says_which_way_the_page_will_be_reachable(wizard, pairs):
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", mode="off")
    _status, _headers, body = client.get("/setup/review", headers=AUTH)
    assert b"no password" in body


# ----------------------------------------------------------- wifi, and out ----

def test_skipping_wifi_moves_on(wizard, store, monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    client, _server, _path = wizard
    status, headers, _body = step(client, "/setup/wifi/skip")
    assert status == 303
    assert headers["Location"] == "/setup/welcome"
    assert load_draft(store)["wifi"]["skipped"] is True


def test_a_join_with_no_network_chosen_is_refused(wizard, monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/wifi", wifi_ssid="__other__",
                                  wifi_other_ssid="  ")
    assert status == 400
    assert b"Tap a network" in body


def test_a_join_is_recorded_before_it_can_reboot_the_device(
        wizard, store, monkeypatch):
    """The join tears the phone's connection down; the draft has to survive."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    monkeypatch.setattr(network, "connect_wifi",
                        lambda ssid, password, hidden=False: (False, "nope"))
    client, _server, _path = wizard
    status, _headers, _body = step(client, "/setup/wifi", wifi_ssid="Home",
                                   wifi_password="hunter2")
    assert status == 200
    wifi = load_draft(store)["wifi"]
    assert wifi == {"attempted_ssid": "Home", "pending": True, "error": ""}


def test_setup_on_an_already_configured_device_goes_to_settings(wizard, store):
    """A stale QR code or bookmark must not restart setup."""
    client, _server, _path = wizard
    store.add_entries("Person A", [{"sgv": 120, "date": 1_700_000_000_000}])
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/settings")


def test_a_paired_device_is_past_first_boot(wizard):
    """The device token is the surest sign setup already happened."""
    client, server, path = wizard
    raw = json.loads(path.read_text())
    raw["glucocore"] = {"device_id": "dev-9", "device_token": "device-token"}
    path.write_text(json.dumps(raw))
    server.config = load(path)
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/settings")


def test_setup_can_still_be_re_run_deliberately(wizard, store):
    client, _server, _path = wizard
    store.add_entries("Person A", [{"sgv": 120, "date": 1_700_000_000_000}])
    status, headers, _body = client.get("/setup?again=1", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/setup/welcome")


def test_the_committed_draft_keeps_no_credentials(wizard, store, pairs):
    """The draft holds this display's token until it is written down."""
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    # It is in the draft while the wizard is in flight — that is what lets
    # the last step write it without spending the code again.
    assert "device-token" in json.dumps(load_draft(store))

    step(client, "/setup/password")
    step(client, "/setup/review")

    tombstone = store.get_params(onboarding.SETUP_KEY)
    assert tombstone["committed_at"]
    assert "device-token" not in json.dumps(tombstone)
    assert load_draft(store) == {}


def test_the_done_page_never_seeds_a_new_draft(wizard, store):
    client, _server, _path = wizard
    status, _headers, _body = client.get("/setup/done", headers=AUTH)
    assert status == 200
    assert load_draft(store) == {}


# --------------------------------------------------------------- mmol/L ----

def test_the_ranges_step_opens_in_mgdl(wizard):
    client, _server, _path = wizard
    _status, _headers, body = client.get("/setup/thresholds", headers=AUTH)
    assert b'value="mg/dL" checked' in body
    assert b'value="70"' in body


def test_choosing_mmol_mid_wizard_converts_the_boxes(wizard, store):
    client, _server, _path = wizard
    step(client, "/setup/thresholds", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250")
    display = load_draft(store)["display"]
    assert display["units"] == "mmol/L"
    assert (display["low"], display["high"]) == (70, 180)

    _status, _headers, body = client.get("/setup/thresholds", headers=AUTH)
    assert b'value="mmol/L" checked' in body
    assert b'value="3.9"' in body


def test_a_range_typed_in_mmol_reaches_the_config_as_mgdl(wizard, pairs):
    client, _server, path = wizard
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/pair", how="code", code="123456",
         device_name="Kitchen display")
    step(client, "/setup/thresholds", units="mmol/L", typed_units="mmol/L",
         low="4.0", high="9.0", urgent_low="3.0", urgent_high="14.0")
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    config = load(path)
    assert config.display.units == "mmol/L"
    assert (config.display.low, config.display.high) == (72, 162)


def test_the_review_says_the_ranges_in_the_unit_that_was_chosen(wizard):
    client, _server, _path = wizard
    step(client, "/setup/thresholds", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250")
    _status, _headers, body = client.get("/setup/review", headers=AUTH)
    assert b"3.9" in body and b"10.0" in body
    assert b"mmol/L" in body
