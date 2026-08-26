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

from glucocube import glucocore, network, onboarding, verify
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


def draft_with(done=(), wifi=None, pending_verification=False) -> dict:
    return {
        "version": onboarding.DRAFT_VERSION,
        "started_at": 0,
        "done": list(done),
        "wifi": wifi or {},
        "account": {"email": "c@example.invalid",
                    "pending_verification": pending_verification},
        "device_name": "",
        "patient_ids": [],
        "patient_names": {},
        "display": {},
        "admin_password": "",
        "admin_password_off": False,
        "committed_at": None,
    }


# ----------------------------------------------------------- the step list ----

def test_the_steps_for_a_device_already_online():
    assert steps_for(draft_with()) == [
        "welcome", "timezone", "account", "device_name", "patients",
        "thresholds", "password", "review"]


def test_an_account_being_created_here_has_an_email_to_check():
    """Signing in to an existing account skips it; signing up cannot."""
    assert "verify_email" not in steps_for(draft_with())
    steps = steps_for(draft_with(pending_verification=True))
    assert steps.index("account") < steps.index("verify_email")
    assert steps.index("verify_email") < steps.index("device_name")


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
    """Signing in to GlucoCore over no network at all is not a question."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    steps = steps_for(draft_with())
    assert steps.index("wifi") < steps.index("account")


def test_the_time_zone_is_asked_before_anything_that_shows_a_time():
    steps = steps_for(draft_with())
    assert steps.index("timezone") < steps.index("thresholds")


def test_the_current_step_is_the_first_one_not_done():
    draft = draft_with(done=["welcome", "timezone"])
    assert current_step(draft) == "account"


def test_a_finished_draft_rests_on_review():
    draft = draft_with(done=steps_for(draft_with()))
    assert current_step(draft) == "review"


@pytest.mark.parametrize("step, expected", [
    ("welcome", "timezone"),
    ("timezone", "account"),
    ("account", "device_name"),
    ("device_name", "patients"),
    ("patients", "thresholds"),
    ("thresholds", "password"),
    ("password", "review"),
    ("review", "review"),
])
def test_next_step_walks_the_list(step, expected):
    assert next_step(draft_with(), step) == expected


def test_verifying_an_email_leads_back_into_the_wizard():
    draft = draft_with(pending_verification=True)
    assert next_step(draft, "account") == "verify_email"
    assert next_step(draft, "verify_email") == "device_name"


def test_next_step_from_an_unknown_step_recovers():
    """A stale bookmark must not become a dead end."""
    assert next_step(draft_with(), "nonsense") == "welcome"


@pytest.mark.parametrize("step", sorted(onboarding.RENDERERS))
def test_a_step_maps_to_its_url(step):
    assert path_for(step) == f"/setup/{step}"


def test_every_step_has_a_screen_and_a_title():
    """The three lists that describe the wizard cannot drift apart."""
    for step in steps_for(draft_with(pending_verification=True)):
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


def test_seeding_starts_with_nobody_chosen(config_path):
    """Who appears comes from GlucoCore, not from what was on the device."""
    draft = seed_draft(str(config_path))
    assert draft["patient_ids"] == []
    assert draft["account"] == {}
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
def account(monkeypatch):
    """A GlucoCore account that signs in and can see two patients."""
    monkeypatch.setattr(verify, "glucocore_login",
                        lambda email, password, timeout=10.0: Result(
                            True, "Signed in."))
    monkeypatch.setattr(glucocore, "login",
                        lambda email, password, timeout=30: ("session-token",
                                                             "u-1"))
    monkeypatch.setattr(glucocore, "list_patients",
                        lambda token, userid, timeout=30: PATIENTS)
    return PATIENTS


@pytest.fixture
def registers(monkeypatch):
    """GlucoCore accepting the display, and recording what it was told."""
    calls = []

    def register(token, name, hardware_id, patient_ids, config=None,
                 timeout=60):
        calls.append({"token": token, "name": name,
                      "hardware_id": hardware_id,
                      "patient_ids": list(patient_ids), "config": config})
        return {"deviceToken": "device-token", "device": {"id": "dev-42"}}

    monkeypatch.setattr(glucocore, "register_device", register)
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
    step(client, "/setup/account", mode="login", email="c@example.invalid",
         password="pw")
    step(client, "/setup/device_name", device_name="Kitchen display")
    step(client, "/setup/patients", patient_ids="pat-1")
    step(client, "/setup/thresholds")


def test_setup_starts_at_the_first_step(wizard):
    client, _server, _path = wizard
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert status == 303
    assert headers["Location"] == "/setup/welcome"


@pytest.mark.parametrize("path", ["/setup/welcome", "/setup/timezone",
                                  "/setup/account", "/setup/verify_email",
                                  "/setup/device_name", "/setup/patients",
                                  "/setup/thresholds", "/setup/password",
                                  "/setup/review"])
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
        wizard, store, account, registers):
    client, _server, path = wizard
    before = path.read_text()

    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="Europe/London")
    step(client, "/setup/account", mode="login", email="c@example.invalid",
         password="pw")
    step(client, "/setup/device_name", device_name="Kitchen display")
    step(client, "/setup/patients", patient_ids="pat-1")

    # Nothing is written until the last step.
    assert path.read_text() == before

    step(client, "/setup/thresholds", low="80", high="160")
    step(client, "/setup/password", admin_password="newpassword")
    status, _headers, body = step(client, "/setup/review")

    assert status == 200
    assert b"GlucoCube" in body
    config = load(path)
    assert [u.name for u in config.users] == ["Ada"]
    assert config.users[0].source["patient_id"] == "pat-1"
    assert config.glucocore.device_token == "device-token"
    assert config.glucocore.name == "Kitchen display"
    assert config.display.timezone == "Europe/London"
    assert (config.display.low, config.display.high) == (80, 160)
    assert config.admin_password == "newpassword"
    # The draft is left as a tombstone, without the credentials in it.
    assert load_draft(store) == {}


def test_the_display_is_registered_with_what_was_answered(
        wizard, account, registers):
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    assert len(registers) == 1
    sent = registers[0]
    assert sent["token"] == "session-token"
    assert sent["name"] == "Kitchen display"
    assert sent["patient_ids"] == ["pat-1"]
    assert sent["hardware_id"]
    assert sent["config"]["patientIds"] == ["pat-1"]


def test_two_people_each_get_their_own_panel_and_port(wizard, account,
                                                      registers):
    client, _server, path = wizard
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/account", mode="login", email="c@example.invalid",
         password="pw")
    step(client, "/setup/device_name", device_name="Hall")
    step_many(client, "/setup/patients", [("patient_ids", "pat-1"),
                                          ("patient_ids", "pat-2")])
    step(client, "/setup/thresholds")
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    config = load(path)
    assert [u.name for u in config.users] == ["Ada", "Bo"]
    assert config.users[0].port != config.users[1].port


def test_the_placeholders_an_image_ships_with_do_not_survive(
        wizard, account, registers):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert [u.name for u in load(path).users] == ["Ada"]


def test_a_person_fed_by_an_uploader_is_left_alone(wizard, account,
                                                   registers):
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


# --------------------------------------------------------- the account ----

def test_a_refused_sign_in_is_shown_on_the_same_step(wizard, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "glucocore_login",
                        lambda *a, **k: Result(
                            False, "That email or password did not work."))
    status, _headers, body = step(client, "/setup/account", mode="login",
                                  email="c@example.invalid", password="wrong")
    assert status == 400
    assert b"did not work" in body


def test_signing_in_lists_the_patients_to_choose_from(wizard, store, account):
    client, _server, _path = wizard
    status, headers, _body = step(client, "/setup/account", mode="login",
                                  email="c@example.invalid", password="pw")
    assert (status, headers["Location"]) == (303, "/setup/device_name")
    assert load_draft(store)["available_patients"] == PATIENTS
    _status, _headers, body = client.get("/setup/patients", headers=AUTH)
    assert b"Ada" in body and b"Bo" in body


def test_creating_an_account_waits_for_the_email_to_be_verified(
        wizard, store, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(glucocore, "signup",
                        lambda email, password, name="", timeout=30: {})
    status, headers, _body = step(client, "/setup/account", mode="signup",
                                  email="c@example.invalid",
                                  password="sup3rsecret", name="Cass")
    assert (status, headers["Location"]) == (303, "/setup/verify_email")
    assert load_draft(store)["account"]["pending_verification"] is True


def test_a_signup_that_the_service_refuses_says_so(wizard, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(glucocore, "signup",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("that email is already registered")))
    status, _headers, body = step(client, "/setup/account", mode="signup",
                                  email="c@example.invalid", password="pw")
    assert status == 400
    assert b"already registered" in body


def test_a_verified_account_carries_on_into_the_wizard(wizard, store,
                                                       monkeypatch, account):
    client, _server, _path = wizard
    monkeypatch.setattr(glucocore, "signup",
                        lambda email, password, name="", timeout=30: {})
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/account", mode="signup", email="c@example.invalid",
         password="sup3rsecret")
    # Verifying drops the step from the list it was in, so "what is next"
    # is the first thing still unanswered rather than the step after it.
    status, headers, _body = step(client, "/setup/verify_email")
    assert (status, headers["Location"]) == (303, "/setup/device_name")
    draft = load_draft(store)
    assert draft["account"]["pending_verification"] is False
    # The password was only held to sign in with once verification landed.
    assert "password" not in draft["account"]


def test_an_email_not_verified_yet_comes_back_to_the_same_step(
        wizard, monkeypatch):
    client, _server, _path = wizard
    monkeypatch.setattr(glucocore, "signup",
                        lambda email, password, name="", timeout=30: {})
    step(client, "/setup/account", mode="signup", email="c@example.invalid",
         password="sup3rsecret")
    monkeypatch.setattr(verify, "glucocore_login",
                        lambda *a, **k: Result(False, "Not verified yet."))
    status, _headers, body = step(client, "/setup/verify_email")
    assert status == 400
    assert b"Not verified yet." in body


# ------------------------------------------------- the display, and who ----

def test_the_display_needs_a_name(wizard):
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/device_name",
                                  device_name="   ")
    assert status == 400
    assert b"Enter a name for this display" in body


def test_choosing_nobody_is_refused(wizard):
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/patients")
    assert status == 400
    assert b"Choose at least one person" in body


def test_the_chosen_names_are_remembered_for_the_review(wizard, store,
                                                        account):
    client, _server, _path = wizard
    step(client, "/setup/account", mode="login", email="c@example.invalid",
         password="pw")
    step(client, "/setup/patients", patient_ids="pat-2")
    assert load_draft(store)["patient_names"] == {"pat-2": "Bo"}
    _status, _headers, body = client.get("/setup/review", headers=AUTH)
    assert b"Bo" in body


def test_a_review_missing_an_answer_does_not_write_anything(wizard, account,
                                                            registers):
    """Straight to the last step with nothing filled in."""
    client, _server, path = wizard
    before = path.read_text()
    status, _headers, body = step(client, "/setup/review")
    assert status == 400
    assert b"go back and complete each step" in body
    assert path.read_text() == before
    assert not registers


def test_a_registration_that_fails_leaves_the_device_as_it_was(
        wizard, account, monkeypatch):
    client, _server, path = wizard
    before = path.read_text()
    monkeypatch.setattr(glucocore, "register_device",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("GlucoCore is having a moment")))
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    status, _headers, body = step(client, "/setup/review")
    assert status == 500
    assert b"having a moment" in body
    assert path.read_text() == before


def test_a_registration_with_no_token_is_not_a_pairing(wizard, account,
                                                       monkeypatch):
    client, _server, path = wizard
    before = path.read_text()
    monkeypatch.setattr(glucocore, "register_device",
                        lambda *a, **k: {"device": {"id": "dev-42"}})
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    status, _headers, _body = step(client, "/setup/review")
    assert status == 500
    assert path.read_text() == before


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


def test_a_blank_password_keeps_the_one_in_use(wizard, account, registers):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert load(path).admin_password == "letmein"


# ------------------------------------------------ finishing with no password ----
#
# Someone whose device is only reachable from a network they trust can say
# so here rather than being handed a password they then have to look up.

def test_choosing_no_password_finishes_without_one(wizard, account,
                                                   registers):
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


def test_choosing_a_password_after_no_password_drops_the_flag(
        wizard, account, registers):
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


def test_the_review_says_which_way_the_page_will_be_reachable(wizard, account):
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


def test_the_committed_draft_keeps_no_credentials(wizard, store, account,
                                                  registers, monkeypatch):
    """The draft holds a GlucoCore session; only a tombstone stays."""
    client, _server, _path = wizard
    monkeypatch.setattr(glucocore, "signup",
                        lambda email, password, name="", timeout=30: {})
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/account", mode="signup", email="c@example.invalid",
         password="sup3rsecret")
    step(client, "/setup/verify_email")
    step(client, "/setup/device_name", device_name="Kitchen display")
    step(client, "/setup/patients", patient_ids="pat-1")
    step(client, "/setup/thresholds")
    step(client, "/setup/password")
    step(client, "/setup/review")

    tombstone = store.get_params(onboarding.SETUP_KEY)
    assert tombstone["committed_at"]
    dumped = json.dumps(tombstone)
    assert "sup3rsecret" not in dumped
    assert "session-token" not in dumped
    assert load_draft(store) == {}


def test_the_done_page_never_seeds_a_new_draft(wizard, store):
    client, _server, _path = wizard
    status, _headers, _body = client.get("/setup/done", headers=AUTH)
    assert status == 200
    assert load_draft(store) == {}
