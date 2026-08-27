"""nspull.py — pulling from somebody else's Nightscout site.

The interesting part is authentication: the configured key may be an API
secret or an access token, sites accept exactly one style, and getting it
wrong looks identical to a wrong password. The poller tries each style and
remembers the winner, so that is what these tests pin down.
"""

import hashlib
import urllib.error
import urllib.request

import pytest

from glucocube import nspull, synclog
from glucocube.nspull import (
    NightscoutPoller,
    auth_modes,
    params_from_profile,
    probe,
    request,
)

from helpers import FakeResponse, RecordingOpener

BASE = "https://ns.example.invalid"


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE, code, "denied", {}, None)


def profile_doc(**overrides) -> dict:
    doc = {
        "defaultProfile": "Default",
        "store": {"Default": {"dia": 6, "units": "mg/dl",
                              "sens": [{"time": "00:00", "value": 45}],
                              "carbratio": [{"time": "00:00", "value": 9}]}},
    }
    doc.update(overrides)
    return doc


# -------------------------------------------------- params_from_profile ----

def test_therapy_settings_are_read_from_the_default_profile():
    assert params_from_profile([profile_doc()]) == {
        "isf": 45.0, "cr": 9.0, "dia_hours": 6.0}


def test_the_named_default_profile_wins_over_the_others():
    doc = profile_doc(defaultProfile="Night")
    doc["store"]["Night"] = {"sens": [{"value": 70}], "carbratio": [{"value": 12}]}
    params = params_from_profile([doc])
    assert (params["isf"], params["cr"]) == (70.0, 12.0)


def test_an_unknown_default_profile_falls_back_to_the_first_one():
    doc = profile_doc(defaultProfile="Missing")
    assert params_from_profile([doc])["isf"] == 45.0


def test_mmol_sensitivity_is_converted_to_mgdl():
    """A profile in mmol/L would otherwise read as an ISF of 2.5."""
    doc = profile_doc()
    doc["store"]["Default"]["units"] = "mmol/L"
    doc["store"]["Default"]["sens"] = [{"value": 2.5}]
    assert params_from_profile([doc])["isf"] == pytest.approx(45.04, abs=0.1)


def test_a_small_sensitivity_is_treated_as_mmol_even_unlabelled():
    doc = profile_doc()
    doc["store"]["Default"]["units"] = ""
    doc["store"]["Default"]["sens"] = [{"value": 3.0}]
    assert params_from_profile([doc])["isf"] == pytest.approx(54.05, abs=0.1)


@pytest.mark.parametrize("docs", [
    [], [None], ["junk"], [{}], [{"store": {}}],
    [{"store": {"Default": {}}}],
])
def test_a_profile_with_nothing_usable_yields_nothing(docs):
    assert params_from_profile(docs) == {}


def test_partial_profiles_yield_what_they_have():
    doc = {"defaultProfile": "Default", "store": {"Default": {"dia": 5}}}
    assert params_from_profile([doc]) == {"dia_hours": 5.0}


# ----------------------------------------------------------- auth_modes ----

def test_all_three_auth_styles_are_tried_for_a_key():
    assert [mode for mode, _key in auth_modes("s3cret")] == \
        ["sha1", "token", "raw"]


@pytest.mark.parametrize("key", ["", None, "   "])
def test_a_site_with_no_key_is_asked_anonymously(key):
    assert auth_modes(key) == [("none", "")]


# -------------------------------------------------------------- request ----

def test_a_request_carries_a_browser_user_agent(monkeypatch):
    """Hosted sites sit behind Cloudflare, which rejects default urllib."""
    opener = RecordingOpener({BASE: []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    request(BASE, "/api/v1/entries/sgv.json", {"count": 1}, ("none", ""))
    assert "Mozilla" in opener.requests[0].get_header("User-agent")


def test_the_sha1_style_hashes_the_secret(monkeypatch):
    opener = RecordingOpener({BASE: []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    request(BASE, "/api/v1/entries.json", {}, ("sha1", "s3cret"))
    assert opener.requests[0].get_header("Api-secret") == \
        hashlib.sha1(b"s3cret").hexdigest()


def test_the_raw_style_sends_the_secret_unhashed(monkeypatch):
    opener = RecordingOpener({BASE: []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    request(BASE, "/api/v1/entries.json", {}, ("raw", "s3cret"))
    assert opener.requests[0].get_header("Api-secret") == "s3cret"


def test_the_token_style_goes_in_the_query_string(monkeypatch):
    opener = RecordingOpener({BASE: []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    request(BASE, "/api/v1/entries.json", {"count": 5}, ("token", "abc-123"))
    assert "token=abc-123" in opener.urls[0]
    assert "count=5" in opener.urls[0]
    assert opener.requests[0].get_header("Api-secret") is None


def test_a_trailing_slash_on_the_site_url_does_not_double_up(monkeypatch):
    opener = RecordingOpener({"ns.example.invalid": []})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    request(BASE + "/", "/api/v1/entries.json", {}, ("none", ""))
    assert "invalid//api" not in opener.urls[0]


def test_a_non_list_answer_is_treated_as_no_documents(monkeypatch):
    """Some sites answer an error page with 200 and a JSON object."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        RecordingOpener({BASE: {"status": "error"}}))
    assert request(BASE, "/api/v1/entries.json", {}, ("none", "")) == []


# ---------------------------------------------------------------- probe ----

def test_probe_reports_the_style_the_site_accepted(monkeypatch):
    def answer(req):
        if req.get_header("Api-secret") == hashlib.sha1(b"key").hexdigest():
            raise http_error(401)
        if "token=key" in req.full_url:
            return FakeResponse([{"sgv": 120}])
        raise http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen",
                        RecordingOpener({BASE: answer}))
    mode, entries = probe(BASE, "key")
    assert mode == "token"
    assert entries == [{"sgv": 120}]


def test_probe_raises_when_every_style_is_rejected(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        RecordingOpener({BASE: http_error(401)}))
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        probe(BASE, "key")
    assert excinfo.value.code == 401


def test_probe_does_not_retry_a_failure_that_is_not_about_auth(monkeypatch):
    """A 500 is the site being broken; trying two more styles just waits."""
    opener = RecordingOpener({BASE: http_error(500)})
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    with pytest.raises(urllib.error.HTTPError):
        probe(BASE, "key")
    assert len(opener.requests) == 1


def test_probe_of_a_site_with_no_readings_still_reports_success(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", RecordingOpener({BASE: []}))
    assert probe(BASE, "") == ("none", [])


# --------------------------------------------------------------- poller ----

@pytest.fixture
def poller(store):
    return NightscoutPoller(
        "Ada", {"url": BASE + "/", "api_secret": "key", "poll_seconds": 60},
        store)


def stub_poller(poller, monkeypatch, entries=(), treatments=(),
                devicestatus=(), profile=(), reject=()):
    """Answer the poller's four endpoints without any HTTP."""
    seen = []

    def fake_request(path, params, mode):
        seen.append((path, mode))
        if mode[0] in reject:
            raise http_error(401)
        if "entries" in path:
            return list(entries)
        if "treatments" in path:
            return list(treatments)
        if "devicestatus" in path:
            return list(devicestatus)
        return list(profile)

    monkeypatch.setattr(poller, "_request", fake_request)
    return seen


def test_a_poll_ingests_every_kind_of_document(poller, monkeypatch, store):
    stub_poller(
        poller, monkeypatch,
        entries=[{"sgv": 120, "date": 1_700_000_000_000}],
        treatments=[{"_id": "t1", "insulin": 2, "created_at": 1_700_000_000_000}],
        devicestatus=[{"created_at": 1_700_000_000_000,
                       "openaps": {"iob": {"iob": 1.4}}}],
        profile=[profile_doc()],
    )
    poller._poll_once()
    snap = store.snapshot("Ada")
    assert snap.sgv == 120
    assert snap.last_bolus == 2
    assert snap.iob == 1.4
    assert snap.params["isf"] == 45.0


def test_a_poll_is_recorded_in_the_sync_log(poller, monkeypatch):
    stub_poller(poller, monkeypatch, entries=[{"sgv": 120, "date": 1}])
    poller._poll_once()
    entry = synclog.recent()[0]
    assert entry["source"] == "nightscout"
    assert entry["ok"] is True
    assert "1 readings" in entry["message"]


def test_the_working_auth_style_is_remembered(poller, monkeypatch):
    """Otherwise every poll pays for two rejected requests first."""
    seen = stub_poller(poller, monkeypatch, entries=[{"sgv": 120, "date": 1}],
                       reject=("sha1",))
    poller._poll_once()
    first_round = len([mode for _path, mode in seen if mode[0] == "sha1"])
    seen.clear()
    poller._poll_once()
    assert first_round == 1
    assert not [mode for _path, mode in seen if mode[0] == "sha1"]


def test_a_site_that_rejects_every_style_raises(poller, monkeypatch):
    stub_poller(poller, monkeypatch, reject=("sha1", "token", "raw"))
    with pytest.raises(urllib.error.HTTPError):
        poller._poll_once()


def test_the_profile_is_not_fetched_on_every_poll(poller, monkeypatch):
    """Therapy settings change rarely; the site does not need the traffic."""
    seen = stub_poller(poller, monkeypatch, entries=[{"sgv": 120, "date": 1}],
                       profile=[profile_doc()])
    for _ in range(3):
        poller._poll_once()
    assert len([path for path, _mode in seen if "profile" in path]) == 1


def test_a_broken_profile_endpoint_does_not_fail_the_poll(poller, monkeypatch,
                                                          store):
    def fake_request(path, params, mode):
        if "profile" in path:
            raise http_error(500)
        return [{"sgv": 120, "date": 1_700_000_000_000}] if "entries" in path else []

    monkeypatch.setattr(poller, "_request", fake_request)
    poller._poll_once()
    assert store.snapshot("Ada").sgv == 120


def test_the_configured_url_is_normalized(store):
    poller = NightscoutPoller("Ada", {"url": BASE + "///"}, store)
    assert poller.base == BASE


def test_a_token_only_source_is_configured_from_the_token_field(store):
    poller = NightscoutPoller("Ada", {"url": BASE, "token": "abc"}, store)
    assert poller._modes[0] == ("sha1", "abc")


def test_the_poll_interval_has_a_floor(store):
    """A misconfigured 1-second poll would hammer somebody else's site."""
    poller = NightscoutPoller("Ada", {"url": BASE, "poll_seconds": 1}, store)
    assert poller.poll_seconds == 30


def test_the_documented_page_sizes_are_sane():
    assert nspull.ENTRY_COUNT >= 36        # at least three hours of chart
    assert nspull.PROFILE_EVERY_N_POLLS > 1
