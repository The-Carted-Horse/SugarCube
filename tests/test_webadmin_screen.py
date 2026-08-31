"""The settings pages for the ambient screen, the weather and the art.

Driven over a real socket, like the rest of the settings site, because
these are the pages somebody uses standing in front of the device with a
phone — and because the upload is the only thing on the site that posts
bytes, which is a code path a unit test would not exercise at all.
"""

import base64
import json
import struct
import threading
import zlib
from urllib.parse import urlencode

import pytest

from glucocube import wallpaper, weather, webadmin
from glucocube.config import load
from glucocube.webadmin import AdminServer
from helpers import Client, FakeResponse, RecordingOpener

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:letmein").decode()}
BOUNDARY = "----GlucoCubeUpload"


def png_bytes(width=64, height=40):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    rows = b"".join(b"\x00" + bytes([(x * 4) % 256 for x in range(width)
                                     for _ in range(3)])
                    for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def restarts(monkeypatch):
    calls = []
    monkeypatch.setattr(webadmin, "restart_soon",
                        lambda delay=0.8: calls.append(delay))
    return calls


@pytest.fixture
def admin(config_path, store, restarts):
    config = load(config_path)
    config.admin_port = 0
    server = AdminServer(config, str(config_path), store)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,),
                              daemon=True)
    thread.start()
    yield Client(server.server_address[1]), config_path, store
    server.shutdown()
    server.server_close()


def form(**fields) -> bytes:
    return urlencode({k: v for k, v in fields.items()
                      if v is not None}).encode()


def post(client, path, **fields):
    return client.post(path, form(**fields), headers={
        "Content-Type": "application/x-www-form-urlencoded", **AUTH})


def upload(client, path, data, filename="art.png"):
    body = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"image\";"
            f" filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode() + data + f"\r\n--{BOUNDARY}--\r\n".encode()
    return client.post(path, body, headers={
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}", **AUTH})


def saved(config_path):
    return json.loads(config_path.read_text())


# ------------------------------------------------------------ the screen ---

def test_the_screen_page_offers_both_arrangements(admin):
    client, _, _ = admin
    status, _, body = client.get("/settings/screen", headers=AUTH)
    assert status == 200
    assert b"Everyone at once" in body and b"One at a time" in body


def test_choosing_one_at_a_time_lands_in_the_config(admin, restarts):
    client, config_path, _ = admin
    status, headers, _ = post(client, "/settings/screen", layout="rotate",
                              rotate_seconds="20", wallpaper_dim="45",
                              night_dim_boost="30", split_direction="auto",
                              split_max="", wallpaper="")
    assert status == 200
    display = saved(config_path)["display"]
    assert display["layout"] == "rotate"
    assert display["rotate_seconds"] == 20
    assert (display["wallpaper_dim"], display["night_dim_boost"]) == (45, 30)
    assert restarts, "a display setting takes effect by starting over"


def test_everyone_on_screen_removes_the_cap_rather_than_keeping_it(admin):
    """Blank is a value here — "everyone" — not a field left alone."""
    client, config_path, _ = admin
    post(client, "/settings/screen", layout="split", split_max="2",
         split_direction="auto", wallpaper="")
    assert saved(config_path)["display"]["split_max"] == 2
    post(client, "/settings/screen", layout="split", split_max="",
         split_direction="auto", wallpaper="")
    assert "split_max" not in saved(config_path)["display"]


def test_a_rotation_nobody_could_read_is_refused(admin):
    client, config_path, _ = admin
    status, _, body = post(client, "/settings/screen", layout="rotate",
                           rotate_seconds="1", split_direction="auto",
                           wallpaper="")
    assert status == 400
    assert b"between 3" in body
    assert "layout" not in saved(config_path).get("display", {})


def test_a_dim_outside_the_scale_is_refused(admin):
    client, _, _ = admin
    status, _, _ = post(client, "/settings/screen", layout="split",
                        wallpaper_dim="150", split_direction="auto",
                        wallpaper="")
    assert status == 400


def test_a_background_this_device_does_not_have_is_refused(admin):
    """A form field is not a fact, and an unknown value is a black screen."""
    client, _, _ = admin
    status, _, body = post(client, "/settings/screen", layout="split",
                           split_direction="auto", wallpaper="d" * 32)
    assert status == 400
    assert b"not on this device" in body


def test_the_art_this_device_draws_itself_is_offered(admin):
    client, _, _ = admin
    _status, _headers, body = client.get("/settings/screen", headers=AUTH)
    for name in wallpaper.BUNDLED:
        assert name.title().encode() in body


def test_bundled_art_can_be_chosen(admin):
    client, config_path, _ = admin
    post(client, "/settings/screen", layout="split", split_direction="auto",
         wallpaper="bundled:tide")
    assert saved(config_path)["display"]["wallpaper"] == "bundled:tide"


# ------------------------------------------------------------- uploading ---

def test_a_picture_uploads_and_lands_on_that_person(admin, restarts):
    client, config_path, _ = admin
    status, _, _ = upload(client, "/settings/person/wallpaper?i=0", png_bytes())
    assert status == 200
    users = saved(config_path)["users"]
    assert wallpaper.is_id(users[0]["wallpaper"])
    config = load(config_path)
    assert wallpaper.cached_path(config.database,
                                 users[0]["wallpaper"]).exists()
    assert restarts


def test_the_same_picture_twice_is_one_file(admin):
    """Named by the digest of its own bytes, like an id from GlucoCore."""
    client, config_path, _ = admin
    upload(client, "/settings/person/wallpaper?i=0", png_bytes())
    first = saved(config_path)["users"][0]["wallpaper"]
    upload(client, "/settings/person/wallpaper?i=1", png_bytes())
    assert saved(config_path)["users"][1]["wallpaper"] == first


def test_a_file_that_is_not_an_image_is_refused(admin, config_path):
    client, config_path, _ = admin
    status, _, body = upload(client, "/settings/person/wallpaper?i=0",
                             b"#!/bin/sh\nrm -rf /\n")
    assert status == 400
    assert b"not a JPEG or a PNG" in body
    assert "wallpaper" not in saved(config_path)["users"][0]


def test_a_png_renamed_jpg_is_still_read_as_what_it_is(admin):
    client, config_path, _ = admin
    status, _, _ = upload(client, "/settings/person/wallpaper?i=0",
                          png_bytes(), filename="holiday.jpg")
    assert status == 200
    assert wallpaper.is_id(saved(config_path)["users"][0]["wallpaper"])


def test_a_file_too_big_for_the_device_is_refused_with_413(admin):
    client, config_path, _ = admin
    huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (
        webadmin.AdminHandler.MAX_WALLPAPER_BYTES + 1024)
    status, _, _ = upload(client, "/settings/person/wallpaper?i=0", huge)
    assert status == 413
    assert "wallpaper" not in saved(config_path)["users"][0]


def test_the_connection_survives_a_refused_upload(admin):
    """HTTP/1.1 keep-alive: a half-read body desyncs every request after it."""
    client, _, _ = admin
    huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (
        webadmin.AdminHandler.MAX_WALLPAPER_BYTES + 1024)
    upload(client, "/settings/person/wallpaper?i=0", huge)
    status, _, body = client.get("/settings/screen", headers=AUTH)
    assert status == 200 and b"The screen" in body


def test_a_form_that_sends_no_file_says_so(admin):
    client, _, _ = admin
    status, _, body = client.post(
        "/settings/person/wallpaper?i=0", b"nothing=here",
        headers={"Content-Type": "application/x-www-form-urlencoded", **AUTH})
    assert status == 400
    assert b"did not send a file" in body


# --------------------------------------------------------------- weather ---

def test_the_weather_page_says_it_is_off_until_it_knows_where(admin):
    client, _, _ = admin
    status, _, body = client.get("/settings/weather", headers=AUTH)
    assert status == 200
    assert b"Weather" in body


def test_a_town_is_looked_up_once_and_stored_as_numbers(admin, monkeypatch):
    client, config_path, _ = admin
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({
        "geocoding-api": FakeResponse({"results": [{
            "name": "Sheffield", "admin1": "England",
            "country": "United Kingdom",
            "latitude": 53.38, "longitude": -1.47}]})}))
    status, _, _ = post(client, "/settings/weather", enabled="1",
                        place="Sheffield", units="celsius")
    assert status == 200
    block = saved(config_path)["weather"]
    assert (block["latitude"], block["longitude"]) == (53.38, -1.47)
    assert block["place"].startswith("Sheffield")
    assert block["units"] == "celsius"


def test_a_place_nobody_has_heard_of_is_refused(admin, monkeypatch):
    client, config_path, _ = admin
    monkeypatch.setattr("urllib.request.urlopen",
                        RecordingOpener({"geocoding-api": {"results": []}}))
    status, _, body = post(client, "/settings/weather", enabled="1",
                           place="Nowhere-at-all", units="fahrenheit")
    assert status == 400
    assert b"was found" in body
    assert "weather" not in saved(config_path)


def test_clearing_the_town_forgets_where_the_device_is(admin, monkeypatch):
    client, config_path, _ = admin
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({
        "geocoding-api": {"results": [{"name": "Sheffield", "latitude": 53.4,
                                       "longitude": -1.5}]}}))
    post(client, "/settings/weather", enabled="1", place="Sheffield",
         units="fahrenheit")
    post(client, "/settings/weather", enabled="1", place="",
         units="fahrenheit")
    block = saved(config_path)["weather"]
    assert "latitude" not in block and block["place"] == ""


def test_saving_an_unchanged_town_asks_nothing(admin, monkeypatch):
    """Resolved once, on the save that names it — not on every save."""
    client, config_path, _ = admin
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({
        "geocoding-api": {"results": [{"name": "Sheffield", "latitude": 53.4,
                                       "longitude": -1.5}]}}))
    post(client, "/settings/weather", enabled="1", place="Sheffield",
         units="fahrenheit")
    stored = saved(config_path)["weather"]["place"]
    # The autouse no-network fixture is the assertion: a lookup would raise.
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({}))
    status, _, _ = post(client, "/settings/weather", enabled="1",
                        place=stored, units="celsius")
    assert status == 200
    assert saved(config_path)["weather"]["units"] == "celsius"


def test_the_weather_row_is_on_the_settings_hub(admin):
    client, _, _ = admin
    _status, _headers, body = client.get("/settings", headers=AUTH)
    assert b"/settings/weather" in body


def test_a_stored_reading_is_shown_back_on_the_page(admin, monkeypatch):
    client, config_path, store = admin
    monkeypatch.setattr("urllib.request.urlopen", RecordingOpener({
        "geocoding-api": {"results": [{"name": "Sheffield", "latitude": 53.4,
                                       "longitude": -1.5}]}}))
    post(client, "/settings/weather", enabled="1", place="Sheffield",
         units="fahrenheit")
    store.replace_params(weather.PARAMS_KEY,
                         {"temp": 12, "code": 0, "fetched_at": 1})
    _status, _headers, body = client.get("/settings/weather", headers=AUTH)
    assert b"Sheffield" in body


# ---------------------------------------------------------- the person page --

def test_a_person_can_be_given_their_own_background(admin):
    client, config_path, _ = admin
    status, _, body = client.get("/settings/person?i=0", headers=AUTH)
    assert status == 200
    assert b"Behind them" in body
    assert b"Nothing, even if the display has art" in body
    assert b'enctype="multipart/form-data"' in body


def test_choosing_nothing_for_a_person_is_recorded(admin):
    client, config_path, _ = admin
    status, _, _ = post(client, "/settings/person?i=0", name="Ada",
                        source="push", port="1337", api_secret="s",
                        wallpaper="none")
    assert status == 200
    assert saved(config_path)["users"][0]["wallpaper"] == "none"


def test_a_person_can_hold_the_screen_longer(admin):
    client, config_path, _ = admin
    post(client, "/settings/person?i=0", name="Ada", source="push",
         port="1337", api_secret="s", wallpaper="", rotate_seconds="25")
    assert saved(config_path)["users"][0]["rotate_seconds"] == 25


def test_a_duration_nobody_could_read_is_refused(admin):
    client, _, _ = admin
    status, _, body = post(client, "/settings/person?i=0", name="Ada",
                           source="push", port="1337", api_secret="s",
                           wallpaper="", rotate_seconds="1")
    assert status == 400
    assert b"between 3" in body
