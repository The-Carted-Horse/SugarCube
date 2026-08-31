"""webadmin.py — the settings site, over real HTTP.

Saving is the risky half of this module: it rewrites config.json and then
exits so systemd restarts the app. ``restart_soon`` is stubbed in every
test here — unstubbed it would call ``os._exit`` and take the test runner
with it — and each save is checked by reloading the file the way the next
boot would.
"""

import json
import threading
import urllib.parse

import pytest

from glucocube import config as config_mod
from glucocube import updater, webadmin
from glucocube.config import load
from glucocube.webadmin import AdminServer, _check_ranges, _g, _number

from helpers import Client

PASSWORD = "letmein"
AUTH = {"Authorization": "Basic " + __import__("base64").b64encode(
    b"admin:letmein").decode()}


@pytest.fixture
def restarts(monkeypatch):
    """Catch the process exit that follows every save."""
    calls = []
    monkeypatch.setattr(webadmin, "restart_soon",
                        lambda delay=0.8: calls.append(delay))
    return calls


@pytest.fixture
def admin(config_path, store, restarts):
    config = load(config_path)
    config.admin_port = 0                  # let the OS choose
    server = AdminServer(config, str(config_path), store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    yield Client(server.server_address[1]), server, config_path
    server.shutdown()
    server.server_close()


def form(**fields) -> bytes:
    return urllib.parse.urlencode(fields).encode()


def post(client, path, **fields):
    return client.request("POST", path, form(**fields),
                          {**AUTH, "Content-Type":
                           "application/x-www-form-urlencoded"})


def users_of(config_path) -> list[dict]:
    return json.loads(config_path.read_text())["users"]


# --------------------------------------------------------------- helpers ----

@pytest.mark.parametrize("value, expected", [
    (70, "70"), (70.0, "70"), (70.5, "70.5"), ("70", "70"),
    ("", ""), (None, ""), ("nonsense", "nonsense"),
])
def test_thresholds_are_shown_the_way_people_write_them(value, expected):
    assert _g(value) == expected


def test_a_blank_threshold_can_fall_back_to_a_default():
    assert _g("", 180) == "180"


def test_a_number_is_read_from_a_form():
    assert _number({"low": "70"}, "low", "The low") == 70.0
    assert _number({"low": " 70.5 "}, "low", "The low") == 70.5


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_field_means_unchanged(value):
    assert _number({"low": value}, "low", "The low") is None


def test_a_field_that_is_not_a_number_names_itself_in_the_error():
    """"could not convert string to float" is not something to show anyone."""
    with pytest.raises(ValueError, match="The low needs to be a number"):
        _number({"low": "ninety"}, "low", "The low")


def test_sensible_ranges_are_accepted():
    _check_ranges({"low": 70, "high": 180, "urgent_low": 55, "urgent_high": 250})


@pytest.mark.parametrize("values, message", [
    ({"low": 180, "high": 70, "urgent_low": 55, "urgent_high": 250},
     "low has to be under the high"),
    ({"low": 70, "high": 180, "urgent_low": 90, "urgent_high": 250},
     "urgent low has to be at or under"),
    ({"low": 70, "high": 180, "urgent_low": 55, "urgent_high": 100},
     "urgent high has to be at or over"),
    ({"low": 70, "high": 180, "urgent_low": 0, "urgent_high": 250},
     "above zero"),
])
def test_ranges_the_display_could_not_colour_are_refused(values, message):
    with pytest.raises(ValueError, match=message):
        _check_ranges(values)


def test_the_timezone_picker_offers_the_system_zones():
    options = webadmin.timezone_options()
    assert options[0][0] == ""              # "leave it alone" comes first
    assert ("UTC", "UTC") in options
    assert any(label == "Europe/London" for _value, label in options)


# ------------------------------------------------------------------ auth ----

def test_a_page_needs_the_password(admin):
    client, _server, _path = admin
    status, headers, _body = client.get("/settings")
    assert status == 401
    assert "Basic" in headers["WWW-Authenticate"]


def test_basic_auth_opens_the_settings_page(admin):
    client, _server, _path = admin
    status, _headers, body = client.get("/settings", headers=AUTH)
    assert status == 200
    assert b"GlucoCube settings" in body


def test_a_key_in_the_url_opens_it_too_and_leaves_a_cookie(admin):
    """The QR code on the screen carries the key, so scanning signs you in."""
    client, _server, _path = admin
    status, headers, _body = client.get(f"/settings?key={PASSWORD}")
    assert status == 200
    assert f"glucocube_key={PASSWORD}" in headers["Set-Cookie"]


def test_a_wrong_key_does_not_open_it(admin):
    client, _server, _path = admin
    assert client.get("/settings?key=wrong")[0] == 401


def test_the_cookie_alone_is_enough_for_the_rest_of_the_session(admin):
    client, _server, _path = admin
    status, _headers, _body = client.get(
        "/settings", headers={"Cookie": f"glucocube_key={PASSWORD}"})
    assert status == 200


def test_a_write_without_the_password_is_refused(admin, config_path):
    client, _server, _path = admin
    status, _headers, _body = client.request(
        "POST", "/settings/ranges", form(low="80"),
        {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 401
    assert load(config_path).display.low == 70


def test_an_open_device_needs_no_password(config_path, store, restarts):
    """A device with no admin password set is reachable by anyone on the LAN."""
    config = load(config_path)
    config.admin_password = ""
    config.admin_port = 0
    server = AdminServer(config, str(config_path), store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    try:
        assert Client(server.server_address[1]).get("/settings")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------- pages ----

@pytest.mark.parametrize("path", [
    "/", "/settings", "/settings/screen", "/settings/people",
    "/settings/ranges", "/settings/network", "/settings/clock",
    "/settings/updates", "/settings/access", "/log",
])
def test_every_page_renders(admin, path):
    client, _server, _path = admin
    status, _headers, body = client.get(path, headers=AUTH)
    assert status == 200
    assert body.startswith(b"<!DOCTYPE html>")


def test_a_persons_page_renders_for_each_configured_person(admin):
    client, _server, _path = admin
    for index, name in enumerate(("Ada", "Bo")):
        _status, _headers, body = client.get(f"/settings/person?i={index}",
                                             headers=AUTH)
        assert name.encode() in body


def test_the_add_person_page_renders(admin):
    client, _server, _path = admin
    assert client.get("/settings/person?i=new", headers=AUTH)[0] == 200


def test_an_out_of_range_person_redirects_to_the_list(admin):
    client, _server, _path = admin
    status, headers, _body = client.get("/settings/person?i=99", headers=AUTH)
    assert status == 303
    assert headers["Location"] == "/settings/people"


def test_an_unknown_settings_page_redirects_to_the_hub(admin):
    client, _server, _path = admin
    status, headers, _body = client.get("/settings/nonsense", headers=AUTH)
    assert (status, headers["Location"]) == (303, "/settings")


def test_an_unknown_page_is_a_404_page(admin):
    client, _server, _path = admin
    status, _headers, body = client.get("/nope", headers=AUTH)
    assert status == 404
    assert b"Not found" in body


def test_a_stored_secret_is_never_rendered_back_into_the_page(admin):
    """The page shows a placeholder; blank on save means "keep it"."""
    client, _server, path = admin
    raw = json.loads(path.read_text())
    raw["users"][0]["source"] = {"type": "tidepool",
                                 "email": "cassidy@example.invalid",
                                 "password": "sup3rsecret"}
    path.write_text(json.dumps(raw))
    _status, _headers, body = client.get("/settings/person?i=0", headers=AUTH)
    assert b"sup3rsecret" not in body
    assert b"cassidy@example.invalid" in body


# -------------------------------------------------------------- the API ----

def test_the_health_endpoint_answers_with_the_running_version(admin):
    """The page shown during a restart polls this until the app is back."""
    client, _server, _path = admin
    status, body = client.json("/api/health.json", headers=AUTH)
    assert status == 200
    assert body == {"ok": True, "version": updater.current_version()}


def test_the_dashboard_json_carries_every_person(admin, store):
    client, _server, _path = admin
    store.add_entries("Ada", [{"sgv": 120, "date": _now_ms() - 2 * 60_000,
                               "direction": "Flat"}])
    _status, body = client.json("/api/dashboard.json", headers=AUTH)
    assert [u["name"] for u in body["users"]] == ["Ada", "Bo"]
    ada = body["users"][0]
    assert ada["sgv"] == 120
    assert ada["thresholds"]["low"] == 70
    assert body["update"]["current"] == updater.current_version()


@pytest.mark.parametrize("source, label", [
    (None, "TRIO"),
    ({"type": "tidepool", "email": "c@example.invalid", "password": "pw"},
     "TWIIST"),
    ({"type": "nightscout", "url": "https://ns.example.invalid"}, "NS"),
])
def test_the_dashboard_labels_where_each_persons_data_comes_from(admin, source,
                                                                 label):
    client, server, _path = admin
    server.config.users[0].source = source
    _status, body = client.json("/api/dashboard.json", headers=AUTH)
    assert body["users"][0]["source_label"] == label


def test_a_persons_forecast_is_in_the_dashboard_json(admin, store):
    client, _server, _path = admin
    now = _now_ms()
    store.add_entries("Ada", [{"sgv": 120 + i, "date": now - (10 - i) * 300_000}
                              for i in range(10)])
    _status, body = client.json("/api/dashboard.json", headers=AUTH)
    forecast = body["users"][0]["forecast"]
    assert forecast["source"] in ("device", "est")
    assert forecast["horizons"]


def test_the_sync_log_is_served_as_json(admin):
    from glucocube import synclog

    client, _server, _path = admin
    synclog.add("push", "Ada", "received 2 readings")
    _status, body = client.json("/api/log.json", headers=AUTH)
    assert body["entries"][0]["message"] == "received 2 readings"


def test_the_wifi_json_reports_the_cached_scan(admin):
    client, _server, _path = admin
    _status, body = client.json("/api/wifi.json", headers=AUTH)
    assert set(body) == {"scanning", "networks", "age"}


def test_fonts_are_served_from_the_device(admin):
    """The pages must render fully offline, on the setup hotspot."""
    client, _server, _path = admin
    status, headers, body = client.get("/fonts/JetBrainsMono-Bold.ttf",
                                       headers=AUTH)
    assert status == 200
    assert headers["Content-Type"] == "font/ttf"
    assert len(body) > 1000


def test_the_font_licence_travels_with_the_fonts(admin):
    """The OFL asks that every copy carry it, and this hands out copies."""
    client, _server, _path = admin
    status, _headers, body = client.get("/fonts/OFL-JetBrainsMono.txt",
                                        headers=AUTH)
    assert status == 200
    assert b"SIL OPEN FONT LICENSE" in body.upper()


@pytest.mark.parametrize("path", ["/fonts/../config.json", "/fonts/evil.sh",
                                  "/fonts/nope.ttf"])
def test_nothing_but_fonts_is_served_from_that_directory(admin, path):
    client, _server, _path = admin
    assert client.get(path, headers=AUTH)[0] == 404


# ---------------------------------------------------------- saving ranges ----

def test_saving_ranges_writes_them_and_restarts(admin, restarts):
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/ranges", low="80",
                                   high="160", urgent_low="60",
                                   urgent_high="240", stale_minutes="15")
    assert status == 200
    config = load(path)
    assert (config.display.low, config.display.high) == (80, 160)
    assert config.display.stale_minutes == 15
    assert restarts        # the new values only take effect on restart


def test_a_blank_range_field_leaves_that_threshold_alone(admin):
    client, _server, path = admin
    post(client, "/settings/ranges", low="80", high="", urgent_low="",
         urgent_high="", stale_minutes="")
    config = load(path)
    assert config.display.low == 80
    assert config.display.high == 180


def test_impossible_ranges_are_refused_and_nothing_is_written(admin, restarts):
    client, _server, path = admin
    status, _headers, body = post(client, "/settings/ranges", low="200",
                                  high="100")
    assert status == 400
    assert b"low has to be under the high" in body
    assert load(path).display.low == 70
    assert restarts == []


def test_a_zero_staleness_is_refused(admin):
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/ranges",
                                   stale_minutes="0")
    assert status == 400
    assert load(path).display.stale_minutes == 12


def test_a_non_numeric_range_says_which_field(admin):
    client, _server, _path = admin
    _status, _headers, body = post(client, "/settings/ranges", low="ninety")
    assert b"The low needs to be a number" in body


# ---------------------------------------------------------- saving people ----

def test_adding_a_person_gives_them_a_port_and_a_secret(admin):
    client, _server, path = admin
    post(client, "/settings/person?i=new", name="Cass", source="push")
    users = users_of(path)
    assert [u["name"] for u in users] == ["Ada", "Bo", "Cass"]
    assert users[2]["port"] not in (users[0]["port"], users[1]["port"])
    assert len(users[2]["api_secret"]) >= 16


def test_a_person_with_no_name_is_refused(admin):
    client, _server, path = admin
    status, _headers, body = post(client, "/settings/person?i=new", name="  ")
    assert status == 400
    assert b"A name is needed" in body
    assert len(users_of(path)) == 2


def test_renaming_a_person_carries_their_history_over(admin, store):
    """Everything is keyed by name; without this a rename looks like a wipe."""
    client, _server, path = admin
    store.add_entries("Ada", [{"sgv": 120, "date": _now_ms() - 60_000}])
    post(client, "/settings/person?i=0", name="Ada Lovelace", source="push",
         port="1337")
    assert users_of(path)[0]["name"] == "Ada Lovelace"
    assert store.snapshot("Ada Lovelace").sgv == 120


def test_configuring_a_tidepool_source_stores_the_credentials(admin):
    client, _server, path = admin
    post(client, "/settings/person?i=0", name="Ada", source="tidepool",
         tp_email="cassidy@example.invalid", tp_password="sup3rsecret",
         poll="60")
    source = users_of(path)[0]["source"]
    assert source["type"] == "tidepool"
    assert source["email"] == "cassidy@example.invalid"
    assert source["password"] == "sup3rsecret"


def test_a_blank_password_keeps_the_stored_one(admin, config_path):
    """The page never renders the secret, so blank has to mean unchanged."""
    raw = json.loads(config_path.read_text())
    raw["users"][0]["source"] = {"type": "tidepool", "email": "c@example.invalid",
                                 "password": "sup3rsecret", "poll_seconds": 60}
    config_path.write_text(json.dumps(raw))
    client, _server, path = admin

    post(client, "/settings/person?i=0", name="Ada", source="tidepool",
         tp_email="c@example.invalid", tp_password="", poll="60")

    assert users_of(path)[0]["source"]["password"] == "sup3rsecret"


def test_a_tidepool_source_without_credentials_is_refused(admin):
    client, _server, path = admin
    status, _headers, body = post(client, "/settings/person?i=0", name="Ada",
                                  source="tidepool", tp_email="", tp_password="")
    assert status == 400
    assert b"Tidepool email and password" in body
    assert "source" not in users_of(path)[0]


def test_a_nightscout_url_gets_a_scheme(admin):
    client, _server, path = admin
    post(client, "/settings/person?i=0", name="Ada", source="nightscout",
         ns_url="ns.example.invalid", ns_key="abc", poll="60")
    assert users_of(path)[0]["source"]["url"] == "https://ns.example.invalid"


def test_a_nightscout_source_without_a_url_is_refused(admin):
    client, _server, _path = admin
    status, _headers, body = post(client, "/settings/person?i=0", name="Ada",
                                  source="nightscout", ns_url="")
    assert status == 400
    assert b"Nightscout site address is needed" in body


def test_switching_a_person_back_to_push_drops_the_source(admin, config_path):
    raw = json.loads(config_path.read_text())
    raw["users"][0]["source"] = {"type": "nightscout",
                                 "url": "https://ns.example.invalid"}
    config_path.write_text(json.dumps(raw))
    client, _server, path = admin
    post(client, "/settings/person?i=0", name="Ada", source="push", port="1337")
    assert "source" not in users_of(path)[0]


def test_per_person_thresholds_are_saved(admin):
    client, _server, path = admin
    post(client, "/settings/person?i=0", name="Ada", source="push",
         th_low="80", th_high="160")
    assert users_of(path)[0]["thresholds"] == {"low": 80, "high": 160}


def test_impossible_per_person_thresholds_are_refused(admin):
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/person?i=0", name="Ada",
                                   source="push", th_low="200", th_high="100")
    assert status == 400
    assert "thresholds" not in users_of(path)[0]


def test_a_poll_interval_has_a_floor(admin):
    """A one-second poll would hammer somebody else's server."""
    client, _server, path = admin
    post(client, "/settings/person?i=0", name="Ada", source="nightscout",
         ns_url="https://ns.example.invalid", ns_key="k", poll="1")
    assert users_of(path)[0]["source"]["poll_seconds"] == 15


def test_removing_a_person_leaves_the_other(admin):
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/person/remove?i=1")
    assert status == 200
    assert [u["name"] for u in users_of(path)] == ["Ada"]


def test_the_last_person_cannot_be_removed(admin):
    """A config with no users is one the loader rejects at boot."""
    client, _server, path = admin
    post(client, "/settings/person/remove?i=1")
    status, _headers, body = post(client, "/settings/person/remove?i=0")
    assert status == 400
    assert b"at least one person" in body
    assert len(users_of(path)) == 1


def test_every_saved_config_is_one_the_next_boot_can_load(admin):
    """The single property that keeps a bad edit from bricking the device."""
    client, _server, path = admin
    post(client, "/settings/person?i=new", name="Cass", source="tidepool",
         tp_email="c@example.invalid", tp_password="pw", poll="60")
    post(client, "/settings/ranges", low="75", high="170")
    config = load(path)
    assert len(config.users) == 3
    assert config.display.low == 75


# ----------------------------------------------------------- clock, access ----

def test_saving_a_time_zone(admin):
    client, _server, path = admin
    post(client, "/settings/clock", timezone="Europe/London")
    assert load(path).display.timezone == "Europe/London"


def test_a_browser_alias_time_zone_is_stored_canonically(admin):
    client, _server, path = admin
    post(client, "/settings/clock", timezone="Asia/Calcutta")
    assert config_mod.valid_timezone(load(path).display.timezone)


def test_an_unknown_time_zone_is_refused(admin):
    client, _server, _path = admin
    status, _headers, body = post(client, "/settings/clock",
                                  timezone="Mars/Olympus")
    assert status == 400
    assert b"not a time zone this device knows" in body


def test_the_time_zone_can_be_cleared(admin):
    client, _server, path = admin
    post(client, "/settings/clock", timezone="Europe/London")
    post(client, "/settings/clock", timezone="")
    assert load(path).display.timezone == ""


def test_changing_the_password_writes_it_and_hands_the_browser_the_new_one(admin):
    """Otherwise the cookie stops matching the moment the app restarts."""
    client, _server, path = admin
    status, headers, _body = post(client, "/settings/access",
                                  admin_password="newpassword")
    assert status == 200
    assert load(path).admin_password == "newpassword"
    assert "glucocube_key=newpassword" in headers["Set-Cookie"]


@pytest.mark.parametrize("password", ["short", "  five "])
def test_a_password_under_six_characters_is_refused(admin, password):
    client, _server, path = admin
    status, _headers, body = post(client, "/settings/access",
                                  admin_password=password)
    assert status == 400
    assert b"at least six characters" in body
    assert load(path).admin_password == PASSWORD


@pytest.mark.parametrize("password", ["", "   "])
def test_a_blank_field_keeps_the_password_in_use(admin, password):
    """The field says "leave blank to keep the current one", so it must."""
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/access",
                                   admin_password=password)
    assert status == 200
    assert load(path).admin_password == PASSWORD


def test_a_blank_field_is_refused_when_there_is_no_password_to_keep(admin):
    client, server, path = admin
    post(client, "/settings/access", mode="off")
    server.config = load(path)
    status, _headers, body = post(client, "/settings/access", mode="on",
                                  admin_password="")
    assert status == 400
    assert b"Type a password, or choose No password" in body


# ------------------------------------------------- access: no password ----
#
# An empty password has always disabled Basic auth; until the mode cards
# there was no way to ask for one from the UI. The flag beside it is what
# separates "open on purpose" from "never finished setting up".

def test_choosing_no_password_clears_it_and_records_the_choice(admin):
    client, _server, path = admin
    status, _headers, _body = post(client, "/settings/access", mode="off")
    assert status == 200
    config = load(path)
    assert config.admin_password == ""
    assert config.admin_password_off is True


def test_a_device_with_no_password_lets_anyone_in(admin):
    """The point of the whole thing: no login on a network you trust."""
    client, server, path = admin
    post(client, "/settings/access", mode="off")
    server.config = load(path)
    server.password = ""
    status, _headers, _body = client.get("/settings")      # no Authorization
    assert status == 200


def test_setting_a_password_again_drops_the_flag(admin):
    client, server, path = admin
    post(client, "/settings/access", mode="off")
    server.config = load(path)
    post(client, "/settings/access", mode="on", admin_password="newpassword")
    config = load(path)
    assert config.admin_password == "newpassword"
    assert config.admin_password_off is False
    assert "password_off" not in json.loads(path.read_text())["admin"]


def test_the_hub_stops_asking_once_no_password_is_a_choice(admin):
    client, server, path = admin
    _status, _headers, before = client.get("/settings", headers=AUTH)
    assert b"Set a password</a>" not in before          # it has one

    post(client, "/settings/access", mode="off")
    server.config = load(path)
    _status, _headers, after = client.get("/settings", headers=AUTH)
    assert b"Set a password</a>" not in after
    assert b"on purpose" in after
    assert b">open<" not in after


def test_the_hub_still_asks_when_the_password_just_went_missing(admin):
    """A hand-edited config with no password and no flag is not a choice."""
    client, server, path = admin
    raw = json.loads(path.read_text())
    raw["admin"] = {"port": 8080, "password": ""}
    path.write_text(json.dumps(raw))
    server.config = load(path)
    _status, _headers, body = client.get("/settings", headers=AUTH)
    # The hub says it twice: once as the notice that goes straight to the
    # fix, and once as the value on the Access row itself.
    assert b"set a password" in body
    assert b'href="/settings/access"' in body
    assert b"No password set" in body


def test_the_access_page_offers_both_ways_in(admin):
    client, _server, _path = admin
    _status, _headers, body = client.get("/settings/access", headers=AUTH)
    assert b'value="on" checked' in body               # it has a password
    assert b"No password" in body


# ---------------------------------------------------------------- updates ----

def test_checking_for_updates_records_the_answer(admin, store, monkeypatch):
    client, _server, _path = admin
    monkeypatch.setattr(updater, "_get_json",
                        lambda url: [{"tag_name": "v99.0.0", "prerelease": False,
                                      "draft": False, "body": "", "name": ""}])
    status, headers, _body = post(client, "/update/check")
    assert (status, headers["Location"]) == (303, "/settings/updates?msg=checked")
    assert store.get_params(updater.PARAMS_KEY)["latest_tag"] == "v99.0.0"


def test_only_the_release_on_offer_can_be_installed(admin, store, monkeypatch):
    """Nothing arbitrary: the tag has to match the last check's answer."""
    client, _server, _path = admin
    applied = []
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])
    store.replace_params(updater.PARAMS_KEY,
                        {"available": True, "latest_tag": "v99.0.0"})

    refused, _headers, _body = post(client, "/update/apply", tag="v0.0.1")
    accepted, _headers, _body = post(client, "/update/apply", tag="v99.0.0")

    assert refused == 400
    assert accepted == 200
    assert applied == ["v99.0.0"]


def test_an_install_with_nothing_on_offer_is_refused(admin, store, monkeypatch):
    client, _server, _path = admin
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: pytest.fail("must not install"))
    store.replace_params(updater.PARAMS_KEY, {"available": False,
                                              "latest_tag": "v99.0.0"})
    assert post(client, "/update/apply", tag="v99.0.0")[0] == 400


def test_a_failed_install_is_reported_on_a_real_page(admin, store, monkeypatch):
    client, _server, _path = admin
    monkeypatch.setattr(updater, "apply_update", lambda tag: (False, "disk full"))
    store.replace_params(updater.PARAMS_KEY, {"available": True,
                                              "latest_tag": "v99.0.0"})
    status, _headers, body = post(client, "/update/apply", tag="v99.0.0")
    assert status == 500
    assert body.startswith(b"<!DOCTYPE html>")
    assert b"disk full" in body


def test_changing_the_channel_writes_it_and_moves_the_device(admin, monkeypatch):
    client, server, path = admin
    applied = []
    monkeypatch.setattr(updater, "_get_json",
                        lambda url: [{"tag_name": "v99.0.0", "prerelease": True,
                                      "draft": False, "body": "", "name": ""}])
    monkeypatch.setattr(updater, "apply_update",
                        lambda tag: (applied.append(tag), (True, tag))[1])

    status, _headers, _body = post(client, "/settings/updates/channel",
                                   channel="beta")

    assert status == 200
    assert json.loads(path.read_text())["updates"]["channel"] == "beta"
    # The live config too, so the checker thread follows it without a reboot.
    assert server.config.update_channel == "beta"
    assert applied == ["v99.0.0"]


def test_a_junk_channel_falls_back_to_standard(admin, monkeypatch):
    client, _server, path = admin
    monkeypatch.setattr(updater, "_get_json", lambda url: [])
    post(client, "/settings/updates/channel", channel="nightly")
    assert json.loads(path.read_text())["updates"]["channel"] == "stable"


# ------------------------------------------------------------------ misc ----

def test_the_theme_toggle_is_stored_for_the_display(admin, store):
    client, _server, _path = admin
    status, headers, _body = post(client, "/display/theme", theme="light",
                                  back="/settings/screen")
    assert status == 303
    assert headers["Location"] == "/settings/screen"
    assert store.get_params("__display")["theme"] == "light"


def test_the_theme_toggle_only_redirects_within_settings(admin):
    """A redirect target from a form field is not somewhere to trust."""
    client, _server, _path = admin
    _status, headers, _body = post(client, "/display/theme", theme="dark",
                                   back="https://evil.example.com/")
    assert headers["Location"] == "/settings"


def test_a_junk_theme_is_ignored(admin, store):
    client, _server, _path = admin
    post(client, "/display/theme", theme="rainbow")
    assert store.get_params("__display") == {}


def test_testing_a_source_returns_the_verdict_as_json(admin, monkeypatch):
    from glucocube import verify

    client, _server, _path = admin
    monkeypatch.setattr(verify, "source",
                        lambda config, timeout=10: verify.Result(
                            True, "Signed in to Tidepool."))
    status, _headers, body = client.request(
        "POST", "/api/source/test",
        json.dumps({"type": "tidepool", "email": "c@example.invalid",
                    "password": "pw"}).encode(),
        {**AUTH, "Content-Type": "application/json"})
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_a_head_request_is_answered(admin):
    """Windows' connectivity probe uses HEAD."""
    client, _server, _path = admin
    assert client.request("HEAD", "/settings", headers=AUTH)[0] == 200
    assert client.request("HEAD", "/nope", headers=AUTH)[0] == 404


def test_pages_are_not_cached(admin):
    """A stale settings page would show values that are no longer set."""
    client, _server, _path = admin
    _status, headers, _body = client.get("/settings", headers=AUTH)
    assert headers["Cache-Control"] == "no-store"


def test_an_unknown_post_is_a_404(admin):
    client, _server, _path = admin
    assert post(client, "/nope")[0] == 404


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# --------------------------------------------------------------- mmol/L ----
#
# The device stores mg/dL and always will. What a page shows, and what a
# form means by the number in it, is the other question.

def units_of(config_path) -> str:
    return json.loads(config_path.read_text())["display"].get("units", "")


def display_of(config_path) -> dict:
    return json.loads(config_path.read_text())["display"]


def test_the_ranges_page_opens_in_mgdl_by_default(admin):
    client, _server, _path = admin
    _status, _headers, body = client.get("/settings/ranges", headers=AUTH)
    page = body.decode()
    assert 'value="mg/dL" checked' in page
    assert 'value="70"' in page


def test_choosing_mmol_converts_rather_than_reinterprets(admin, restarts,
                                                          admin_path=None):
    """70 mg/dL is 3.9 mmol/L — not 70 mmol/L, which is not a reading."""
    client, _server, path = admin
    post(client, "/settings/ranges", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250",
         stale_minutes="12")
    saved = display_of(path)
    assert saved["units"] == "mmol/L"
    assert (saved["low"], saved["high"]) == (70, 180)

    # And it comes back in the unit that was chosen.
    _status, _headers, body = client.get("/settings/ranges", headers=AUTH)
    page = body.decode()
    assert 'value="mmol/L" checked' in page
    assert 'value="3.9"' in page and 'value="10.0"' in page


def test_a_threshold_typed_in_mmol_is_stored_in_mgdl(admin, restarts):
    client, _server, path = admin
    post(client, "/settings/ranges", units="mmol/L", typed_units="mmol/L",
         low="4.0", high="9.0", urgent_low="3.0", urgent_high="14.0",
         stale_minutes="12")
    saved = display_of(path)
    assert (saved["low"], saved["high"]) == (72, 162)
    assert (saved["urgent_low"], saved["urgent_high"]) == (54, 252)


def test_the_ranges_a_form_shows_are_the_ranges_it_saves(admin, restarts):
    """Open, save without touching anything, and nothing moves."""
    client, _server, path = admin
    post(client, "/settings/ranges", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250",
         stale_minutes="12")
    before = display_of(path)
    post(client, "/settings/ranges", units="mmol/L", typed_units="mmol/L",
         low="3.9", high="10.0", urgent_low="3.1", urgent_high="13.9",
         stale_minutes="12")
    after = display_of(path)
    for key in ("low", "high", "urgent_low", "urgent_high"):
        assert abs(after[key] - before[key]) <= 1, key


def test_switching_back_to_mgdl_keeps_the_thresholds_put(admin, restarts):
    client, _server, path = admin
    post(client, "/settings/ranges", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250",
         stale_minutes="12")
    post(client, "/settings/ranges", units="mg/dL", typed_units="mmol/L",
         low="3.9", high="10.0", urgent_low="3.1", urgent_high="13.9",
         stale_minutes="12")
    saved = display_of(path)
    assert saved["units"] == "mg/dL"
    assert 68 <= saved["low"] <= 72
    assert 178 <= saved["high"] <= 182


def test_a_range_that_is_not_a_range_is_still_refused_in_mmol(admin):
    """The check runs on mg/dL, after the conversion, as it must."""
    status, _headers, body = post(client_of(admin), "/settings/ranges",
                                  units="mmol/L", typed_units="mmol/L",
                                  low="10.0", high="4.0", urgent_low="3.0",
                                  urgent_high="14.0", stale_minutes="12")
    assert status == 400
    assert b"low" in body.lower()


def client_of(admin):
    return admin[0]


def test_the_api_still_answers_in_mgdl_whatever_the_page_shows(admin,
                                                               restarts,
                                                               store):
    """Anything reading this endpoint has always been given mg/dL."""
    client, server, path = admin
    store.add_entries("Ada", [{"sgv": 121, "date": 1_700_000_000_000}])
    post(client, "/settings/ranges", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250",
         stale_minutes="12")
    # Saving restarts the display in real life, which is how the running
    # config catches up with the file; `restarts` stubs that out.
    server.config = config_mod.load(path)
    _status, data = client.json("/api/dashboard.json", headers=AUTH)
    assert data["units"] == "mmol/L"
    assert data["thresholds"]["low"] == 70
    assert data["users"][0]["sgv"] == 121


def test_the_person_page_asks_for_overrides_in_the_display_unit(admin,
                                                                restarts):
    client, _server, path = admin
    post(client, "/settings/ranges", units="mmol/L", typed_units="mg/dL",
         low="70", high="180", urgent_low="55", urgent_high="250",
         stale_minutes="12")
    post(client, "/settings/person?i=0", name="Ada", th_low="4.5",
         th_high="9.5")
    person = users_of(path)[0]
    assert person["thresholds"]["low"] == 81
    assert person["thresholds"]["high"] == 171

    _status, _headers, body = client.get("/settings/person?i=0", headers=AUTH)
    page = body.decode()
    assert "mmol/L" in page
    assert 'value="4.5"' in page and 'value="9.5"' in page
