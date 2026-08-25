"""onboarding.py — the setup wizard, as a state machine and end to end.

Two things are worth more than the individual screens. First, the step
list is dynamic (Wi-Fi only when there is none, two steps per person), so
"what comes next" has to hold for every shape of draft. Second, nothing is
written to config.json until the final commit — a device abandoned halfway
through setup must still boot the way it did before.
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
    load_draft,
    mark_done,
    next_step,
    path_for,
    reconcile_wifi,
    save_draft,
    seed_draft,
    steps_for,
    users_from_draft,
    wifi_needed,
)
from glucocube.webadmin import AdminServer

from helpers import Client


def draft_with(people=1, done=(), wifi=None) -> dict:
    return {
        "version": onboarding.DRAFT_VERSION,
        "started_at": 0,
        "done": list(done),
        "wifi": wifi or {},
        "people": [{"name": f"P{i}", "port": None, "api_secret": "",
                    "source": None, "thresholds": {}} for i in range(people)],
        "display": {},
        "admin_password": "",
        "admin_password_off": False,
        "committed_at": None,
    }


# ----------------------------------------------------------- the step list ----

def test_the_steps_for_one_person_without_wifi():
    assert steps_for(draft_with(people=1)) == [
        "welcome", "timezone", "people", "source:0", "creds:0",
        "thresholds", "password", "review"]


def test_each_extra_person_adds_their_own_two_steps():
    steps = steps_for(draft_with(people=3))
    assert steps.count("source:2") == 1
    assert steps.index("source:1") < steps.index("creds:1") < steps.index("source:2")


def test_wifi_is_asked_about_only_when_the_device_has_none(monkeypatch):
    """The hotspot being up is the cheap proxy for "no network"."""
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert "wifi" in steps_for(draft_with())
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: False)
    assert "wifi" not in steps_for(draft_with())


def test_a_skipped_wifi_step_does_not_come_back(monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    assert wifi_needed(draft_with(wifi={"skipped": True})) is False


def test_the_time_zone_is_asked_before_anything_that_shows_a_time():
    steps = steps_for(draft_with())
    assert steps.index("timezone") < steps.index("people")


def test_the_current_step_is_the_first_one_not_done():
    draft = draft_with(done=["welcome", "timezone"])
    assert current_step(draft) == "people"


def test_a_finished_draft_rests_on_review():
    draft = draft_with(done=steps_for(draft_with()))
    assert current_step(draft) == "review"


@pytest.mark.parametrize("step, expected", [
    ("welcome", "timezone"),
    ("timezone", "people"),
    ("people", "source:0"),
    ("source:0", "creds:0"),
    ("creds:0", "thresholds"),
    ("thresholds", "password"),
    ("password", "review"),
    ("review", "review"),
])
def test_next_step_walks_the_list(step, expected):
    assert next_step(draft_with(), step) == expected


def test_next_step_from_an_unknown_step_recovers():
    """A stale bookmark must not become a dead end."""
    assert next_step(draft_with(), "nonsense") == "welcome"


@pytest.mark.parametrize("step, path", [
    ("welcome", "/setup/welcome"),
    ("source:1", "/setup/source?i=1"),
    ("creds:0", "/setup/creds?i=0"),
])
def test_a_step_maps_to_its_url(step, path):
    assert path_for(step) == path


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


def test_seeding_carries_over_people_already_configured(config_path):
    draft = seed_draft(str(config_path))
    assert [person["name"] for person in draft["people"]] == ["Ada", "Bo"]
    assert draft["people"][0]["api_secret"] == "ada-secret"


def test_seeding_a_fresh_image_asks_about_one_person(tmp_path):
    """Two blank placeholders is a worse first question than one name."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [
        {"name": "Person A", "port": 1337, "api_secret": "x"},
        {"name": "Person B", "port": 1338, "api_secret": "y"}]}))
    draft = seed_draft(str(path))
    assert len(draft["people"]) == 1
    assert draft["people"][0]["name"] == ""


def test_seeding_without_a_config_still_produces_a_usable_draft(tmp_path):
    draft = seed_draft(str(tmp_path / "missing.json"))
    assert len(draft["people"]) == 1
    assert draft["committed_at"] is None


def test_seeding_keeps_an_existing_source(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [
        {"name": "Ada", "port": 1337,
         "source": {"type": "tidepool", "email": "c@example.invalid"}}]}))
    assert seed_draft(str(path))["people"][0]["source"]["type"] == "tidepool"


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


# ------------------------------------------------------- source from form ----

def test_a_push_person_has_no_source_block():
    assert onboarding._source_from_form({}, {}) is None
    assert onboarding._source_from_form({"type": "push"}, {}) is None


def test_tidepool_credentials_are_trimmed():
    source = onboarding._source_from_form(
        {"type": "tidepool"}, {"email": "  c@example.invalid  ", "password": "pw"})
    assert source == {"type": "tidepool", "email": "c@example.invalid",
                      "password": "pw", "poll_seconds": 60}


def test_a_nightscout_url_gets_a_scheme():
    source = onboarding._source_from_form({"type": "nightscout"},
                                          {"url": "ns.example.invalid"})
    assert source["url"] == "https://ns.example.invalid"


def test_an_http_nightscout_url_is_left_alone():
    source = onboarding._source_from_form(
        {"type": "nightscout"}, {"url": "http://192.168.1.9:1337"})
    assert source["url"] == "http://192.168.1.9:1337"


@pytest.mark.parametrize("source, message", [
    (None, ""),
    ({"type": "tidepool", "email": "c@example.invalid", "password": "pw"}, ""),
    ({"type": "tidepool", "email": "", "password": "pw"}, "email and password"),
    ({"type": "tidepool", "email": "c@example.invalid", "password": ""},
     "email and password"),
    ({"type": "nightscout", "url": "https://x.invalid"}, ""),
    ({"type": "nightscout", "url": ""}, "site address is needed"),
])
def test_missing_credentials_are_named(source, message):
    assert message in onboarding._missing_credentials(source)


# ------------------------------------------------------- users from draft ----

def test_a_draft_becomes_users_the_loader_accepts():
    draft = draft_with(people=2)
    draft["people"][0]["name"] = "Ada"
    draft["people"][1]["name"] = "Bo"
    users = users_from_draft(draft, reserved={80})
    assert [u["name"] for u in users] == ["Ada", "Bo"]
    assert len({u["port"] for u in users}) == 2
    assert all(u["api_secret"] for u in users)


def test_an_existing_secret_is_not_regenerated():
    draft = draft_with()
    draft["people"][0].update(name="Ada", api_secret="keep-me", port=1337)
    assert users_from_draft(draft, reserved=set())[0]["api_secret"] == "keep-me"


def test_the_admin_port_is_not_handed_to_a_person():
    draft = draft_with()
    draft["people"][0]["name"] = "Ada"
    assert users_from_draft(draft, reserved={1337})[0]["port"] != 1337


def test_a_source_and_thresholds_travel_into_the_config():
    draft = draft_with()
    draft["people"][0].update(
        name="Ada", thresholds={"low": 80},
        source={"type": "nightscout", "url": "https://ns.example.invalid"})
    user = users_from_draft(draft, reserved=set())[0]
    assert user["thresholds"] == {"low": 80}
    assert user["source"]["type"] == "nightscout"


# -------------------------------------------------------------- routing ----

@pytest.mark.parametrize("path, handled", [
    ("/setup", True), ("/setup/welcome", True), ("/setup/creds", True),
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


def step(client, path, **fields):
    return client.request(
        "POST", path, urllib.parse.urlencode(fields).encode(),
        {**AUTH, "Content-Type": "application/x-www-form-urlencoded"})


def test_setup_starts_at_the_first_step(wizard):
    client, _server, _path = wizard
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert status == 303
    assert headers["Location"] == "/setup/welcome"


@pytest.mark.parametrize("path", ["/setup/welcome", "/setup/timezone",
                                  "/setup/people", "/setup/source?i=0",
                                  "/setup/creds?i=0", "/setup/thresholds",
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


def test_walking_the_whole_wizard_writes_one_config_at_the_end(wizard, store):
    client, _server, path = wizard
    before = path.read_text()

    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="Europe/London")
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="push")

    # Nothing is written until the last step.
    assert path.read_text() == before

    step(client, "/setup/creds?i=0", api_secret="ada-secret-value")
    step(client, "/setup/thresholds", low="80", high="160")
    step(client, "/setup/password", admin_password="newpassword")
    status, _headers, body = step(client, "/setup/review")

    assert status == 200
    assert b"GlucoCube" in body
    config = load(path)
    assert [u.name for u in config.users] == ["Ada"]
    assert config.users[0].api_secret == "ada-secret-value"
    assert config.display.timezone == "Europe/London"
    assert (config.display.low, config.display.high) == (80, 160)
    assert config.admin_password == "newpassword"
    # The draft is left as a tombstone, without the credentials in it.
    assert load_draft(store) == {}


def test_a_second_person_gets_their_own_steps_and_port(wizard):
    client, _server, path = wizard
    step(client, "/setup/welcome")
    step(client, "/setup/timezone", tzmode="list", timezone="")
    step(client, "/setup/people", name0="Ada", name1="Bo")
    for index in (0, 1):
        step(client, f"/setup/source?i={index}", source="push")
        status, headers, _body = step(client, f"/setup/creds?i={index}")
        assert status == 303
    step(client, "/setup/thresholds")
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")

    config = load(path)
    assert [u.name for u in config.users] == ["Ada", "Bo"]
    assert config.users[0].port != config.users[1].port


def test_a_people_step_with_no_names_is_refused(wizard):
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/people", name0="", name1="")
    assert status == 400
    assert b"At least one person needs a name" in body


def test_a_blank_first_slot_does_not_shift_the_second_person(wizard, store):
    """Their port and secret belong to their position, not their order."""
    client, _server, _path = wizard
    step(client, "/setup/people", name0="", name1="Bo")
    draft = load_draft(store)
    assert [person["name"] for person in draft["people"]] == ["Bo"]


def test_credentials_are_required_before_moving_on(wizard):
    client, _server, _path = wizard
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="tidepool")
    status, _headers, body = step(client, "/setup/creds?i=0", email="",
                                  password="")
    assert status == 400
    assert b"Tidepool email and password" in body


def test_switching_source_discards_the_other_kinds_credentials(wizard, store):
    client, _server, _path = wizard
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="tidepool")
    step(client, "/setup/creds?i=0", email="c@example.invalid", password="pw")
    step(client, "/setup/source?i=0", source="nightscout")
    source = load_draft(store)["people"][0]["source"]
    assert "password" not in source
    assert source["type"] == "nightscout"


def test_choosing_push_mints_a_secret_to_show(wizard, store):
    client, _server, _path = wizard
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="push")
    assert len(load_draft(store)["people"][0]["api_secret"]) == 16


def test_a_time_zone_the_device_does_not_know_asks_again(wizard):
    """The phone reported it; that is the browser's doing, not the user's."""
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/timezone", tzmode="phone",
                                  tz_detected="Mars/Olympus")
    assert status == 200
    assert b"does not know a zone called" in body


def test_a_short_password_is_refused(wizard):
    client, _server, _path = wizard
    status, _headers, body = step(client, "/setup/password",
                                  admin_password="short")
    assert status == 400
    assert b"at least 6 characters" in body


def test_a_blank_password_keeps_the_one_in_use(wizard):
    client, _server, path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", admin_password="")
    step(client, "/setup/review")
    assert load(path).admin_password == "letmein"


def _through_to_the_password_step(client):
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="push")
    step(client, "/setup/creds?i=0")
    step(client, "/setup/thresholds")


# ------------------------------------------------ finishing with no password ----
#
# Someone whose device is only reachable from a network they trust can say
# so here rather than being handed a password they then have to look up.

def test_choosing_no_password_finishes_without_one(wizard):
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
    _through_to_the_password_step(client)
    step(client, "/setup/password", mode="off")
    _status, _headers, body = client.get("/setup/password", headers=AUTH)
    assert b'value="off" checked' in body


def test_choosing_a_password_after_no_password_drops_the_flag(wizard):
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


def test_the_review_says_which_way_the_page_will_be_reachable(wizard):
    client, _server, _path = wizard
    _through_to_the_password_step(client)
    step(client, "/setup/password", mode="off")
    _status, _headers, body = client.get("/setup/review", headers=AUTH)
    assert b"no password" in body


def test_testing_a_source_mid_wizard_reports_the_verdict(wizard, monkeypatch,
                                                         store):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "source",
                        lambda config, timeout=10: verify.Result(
                            True, "Signed in to Tidepool."))
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="tidepool")
    status, _headers, body = client.request(
        "POST", "/setup/verify?i=0",
        urllib.parse.urlencode({"email": "c@example.invalid",
                                "password": "pw"}).encode(),
        {**AUTH, "Accept": "application/json",
         "Content-Type": "application/x-www-form-urlencoded"})
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert load_draft(store)["people"][0]["verified"] is True


def test_a_failed_test_is_shown_but_does_not_block(wizard, monkeypatch, store):
    client, _server, _path = wizard
    monkeypatch.setattr(verify, "source",
                        lambda config, timeout=10: verify.Result(
                            False, "Tidepool rejected those credentials."))
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="tidepool")
    status, _headers, body = step(client, "/setup/verify?i=0",
                                  email="c@example.invalid", password="wrong")
    assert status == 200
    assert b"rejected those credentials" in body
    assert load_draft(store)["people"][0]["verified"] is False


def test_skipping_wifi_moves_on(wizard, store, monkeypatch):
    monkeypatch.setattr(network, "hotspot_active_cached", lambda ttl=5.0: True)
    client, _server, _path = wizard
    status, headers, _body = step(client, "/setup/wifi/skip")
    assert status == 303
    assert headers["Location"] == "/setup/welcome"
    assert load_draft(store)["wifi"]["skipped"] is True


def test_setup_on_an_already_configured_device_goes_to_settings(wizard, store):
    """A stale QR code or bookmark must not restart setup."""
    client, server, _path = wizard
    store.add_entries("Person A", [{"sgv": 120, "date": 1_700_000_000_000}])
    status, headers, _body = client.get("/setup", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/settings")


def test_setup_can_still_be_re_run_deliberately(wizard, store):
    client, _server, _path = wizard
    store.add_entries("Person A", [{"sgv": 120, "date": 1_700_000_000_000}])
    status, headers, _body = client.get("/setup?again=1", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/setup/welcome")


def test_the_committed_draft_keeps_no_credentials(wizard, store):
    """The draft holds Tidepool and Nightscout logins; only a tombstone stays."""
    client, _server, _path = wizard
    step(client, "/setup/people", name0="Ada")
    step(client, "/setup/source?i=0", source="tidepool")
    step(client, "/setup/creds?i=0", email="c@example.invalid",
         password="sup3rsecret")
    step(client, "/setup/thresholds")
    step(client, "/setup/password")
    step(client, "/setup/review")

    tombstone = store.get_params(onboarding.SETUP_KEY)
    assert tombstone["committed_at"]
    assert "sup3rsecret" not in json.dumps(tombstone)
    assert load_draft(store) == {}


def test_the_done_page_never_seeds_a_new_draft(wizard, store):
    client, _server, _path = wizard
    status, _headers, _body = client.get("/setup/done", headers=AUTH)
    assert status == 200
    assert load_draft(store) == {}
