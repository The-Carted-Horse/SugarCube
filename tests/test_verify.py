"""verify.py — "Test connection" on the settings page and setup wizard.

Two things matter beyond "does it say yes when it works": the check must
be bounded (it runs inside an HTTP handler on a Pi, talking to somebody
else's slow server) and it must never echo the secret back to the page.
"""

import socket
import ssl
import time
import urllib.error

import pytest

from glucocube import glucocore, nspull, tidepool, verify
from glucocube.verify import Result


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://ns.example.invalid", code,
                                  "denied", {}, None)


# --------------------------------------------------------------- result ----

def test_a_result_serializes_for_the_pages_javascript():
    assert Result(True, "Signed in.", "detail").as_dict() == {
        "ok": True, "message": "Signed in.", "detail": "detail"}


def test_scrub_masks_a_secret_wherever_it_appears():
    assert verify._scrub("failed for hunter2 (hunter2)", "hunter2") == \
        "failed for *** (***)"


def test_scrub_leaves_a_secret_too_short_to_match_safely_alone():
    """Blanking every "abc" in a message would mangle unrelated words."""
    assert verify._scrub("no abc here", "abc") == "no abc here"


# ------------------------------------------------------- input checking ----

@pytest.mark.parametrize("email, password", [
    ("", "pw"), ("  ", "pw"), ("cassidy@example.invalid", ""),
])
def test_tidepool_needs_both_halves_before_it_tries(email, password, monkeypatch):
    monkeypatch.setattr(tidepool, "login", _never_called)
    result = verify.tidepool_login(email, password)
    assert result.ok is False
    assert "email and password" in result.message


def test_nightscout_needs_an_address(monkeypatch):
    monkeypatch.setattr(nspull, "probe", _never_called)
    assert verify.nightscout_site("", "key").ok is False


def _never_called(*args, **kwargs):
    raise AssertionError("no network call should have been attempted")


# -------------------------------------------------------------- tidepool ----

def test_a_good_login_with_readings(monkeypatch):
    monkeypatch.setattr(tidepool, "login", lambda *a: ("tok", "u-1"))
    monkeypatch.setattr(tidepool, "latest_cbg", lambda *a: [{"value": 5.5}])
    result = verify.tidepool_login("cassidy@example.invalid", "pw")
    assert result.ok is True
    assert "readings are there" in result.message


def test_a_good_login_with_no_readings_yet_still_passes(monkeypatch):
    monkeypatch.setattr(tidepool, "login", lambda *a: ("tok", "u-1"))
    monkeypatch.setattr(tidepool, "latest_cbg", lambda *a: [])
    result = verify.tidepool_login("cassidy@example.invalid", "pw")
    assert result.ok is True
    assert "no glucose readings yet" in result.message


def test_login_working_is_the_answer_even_if_the_data_call_fails(monkeypatch):
    monkeypatch.setattr(tidepool, "login", lambda *a: ("tok", "u-1"))
    monkeypatch.setattr(tidepool, "latest_cbg", _raise(RuntimeError("boom")))
    assert verify.tidepool_login("cassidy@example.invalid", "pw").ok is True


def test_a_rejected_tidepool_password_says_so(monkeypatch):
    monkeypatch.setattr(tidepool, "login", _raise(http_error(401)))
    result = verify.tidepool_login("cassidy@example.invalid", "wrong")
    assert result.ok is False
    assert "rejected those credentials" in result.message


def test_the_password_is_never_shown_back(monkeypatch):
    secret = "sup3rsecret"
    monkeypatch.setattr(tidepool, "login",
                        _raise(RuntimeError(f"bad login for {secret}")))
    result = verify.tidepool_login("cassidy@example.invalid", secret)
    assert secret not in result.message + result.detail
    assert "***" in result.detail


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc
    return raiser


# ------------------------------------------------------------ nightscout ----

@pytest.mark.parametrize("mode, label", [
    ("sha1", "API secret"), ("token", "access token"),
    ("raw", "API secret"), ("none", "no key"),
])
def test_the_accepted_auth_style_is_named_in_the_answer(monkeypatch, mode, label):
    monkeypatch.setattr(nspull, "probe", lambda *a: (mode, [{"sgv": 120}]))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert result.ok is True
    assert label in result.message


def test_a_site_with_no_recent_readings_still_connects(monkeypatch):
    monkeypatch.setattr(nspull, "probe", lambda *a: ("sha1", []))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert result.ok is True
    assert "no recent readings" in result.message


def test_a_missing_scheme_is_assumed_to_be_https(monkeypatch):
    seen = []
    monkeypatch.setattr(nspull, "probe",
                        lambda url, key, timeout: (seen.append(url),
                                                   ("sha1", []))[1])
    verify.nightscout_site("ns.example.invalid", "key")
    assert seen == ["https://ns.example.invalid"]


def test_a_rejected_key_explains_which_key_is_wanted(monkeypatch):
    """The site's own login password is the common mistake."""
    monkeypatch.setattr(nspull, "probe", _raise(http_error(403)))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert result.ok is False
    assert "API secret or an access token" in result.message


def test_a_404_means_the_address_is_not_a_nightscout_site(monkeypatch):
    monkeypatch.setattr(nspull, "probe", _raise(http_error(404)))
    result = verify.nightscout_site("https://example.invalid", "key")
    assert "not a Nightscout site" in result.message


def test_a_server_error_is_reported_with_its_code(monkeypatch):
    monkeypatch.setattr(nspull, "probe", _raise(http_error(502)))
    assert "(502)" in verify.nightscout_site("https://ns.example.invalid",
                                             "key").message


def test_a_bad_certificate_is_named_as_such(monkeypatch):
    monkeypatch.setattr(nspull, "probe", _raise(ssl.SSLError("bad cert")))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert "certificate" in result.message


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("no route"),
    socket.gaierror("name resolution"),
    OSError("network unreachable"),
])
def test_an_unreachable_site_suggests_checking_the_address(monkeypatch, exc):
    monkeypatch.setattr(nspull, "probe", _raise(exc))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert result.ok is False
    assert "Could not reach that address" in result.message


def test_an_unexpected_error_still_produces_a_result(monkeypatch):
    monkeypatch.setattr(nspull, "probe", _raise(ValueError("surprise")))
    result = verify.nightscout_site("https://ns.example.invalid", "key")
    assert result.ok is False
    assert "surprise" in result.detail


# -------------------------------------------------------------- bounding ----

def test_a_slow_service_does_not_hold_the_page_open(monkeypatch):
    """urlopen's timeout bounds each socket read, not the whole exchange."""
    monkeypatch.setattr(tidepool, "login", lambda *a: time.sleep(30))
    started = time.monotonic()
    result = verify.tidepool_login("cassidy@example.invalid", "pw", timeout=0.3)
    assert result.ok is False
    assert "No answer within" in result.message
    assert time.monotonic() - started < 5


def test_a_repeated_test_is_throttled(monkeypatch):
    """Tidepool locks accounts out; a jabbed button must not spend attempts."""
    attempts = []
    monkeypatch.setattr(tidepool, "login",
                        lambda *a: (attempts.append(1), ("tok", "u"))[1])
    monkeypatch.setattr(tidepool, "latest_cbg", lambda *a: [])

    first = verify.tidepool_login("cassidy@example.invalid", "pw")
    second = verify.tidepool_login("cassidy@example.invalid", "pw")

    assert first.ok is True
    assert second.ok is False
    assert "wait" in second.message
    assert len(attempts) == 1


def test_the_throttle_is_per_identity(monkeypatch):
    monkeypatch.setattr(tidepool, "login", lambda *a: ("tok", "u"))
    monkeypatch.setattr(tidepool, "latest_cbg", lambda *a: [])
    assert verify.tidepool_login("one@example.invalid", "pw").ok is True
    assert verify.tidepool_login("two@example.invalid", "pw").ok is True


def test_only_a_couple_of_checks_run_at_once(monkeypatch):
    """A burst of tests must not become a burst of failed logins."""
    acquired = [verify._slots.acquire(blocking=False)
                for _ in range(verify.MAX_CONCURRENT)]
    try:
        assert all(acquired)
        monkeypatch.setattr(tidepool, "login", _never_called)
        result = verify.tidepool_login("cassidy@example.invalid", "pw")
        assert result.ok is False
        assert "still running" in result.message
    finally:
        for _ in range(verify.MAX_CONCURRENT):
            verify._slots.release()


# -------------------------------------------------------------- dispatch ----

def test_source_dispatches_to_tidepool(monkeypatch):
    monkeypatch.setattr(verify, "tidepool_login",
                        lambda email, password, timeout: Result(True, email))
    result = verify.source({"type": "tidepool",
                            "email": "cassidy@example.invalid",
                            "password": "pw"})
    assert result.message == "cassidy@example.invalid"


def test_source_dispatches_to_nightscout_with_either_key_field(monkeypatch):
    seen = []
    monkeypatch.setattr(verify, "nightscout_site",
                        lambda url, key, timeout: (seen.append(key),
                                                   Result(True, "ok"))[1])
    verify.source({"type": "nightscout", "url": "https://ns.example.invalid",
                   "api_secret": "secret"})
    verify.source({"type": "nightscout", "url": "https://ns.example.invalid",
                   "token": "token"})
    assert seen == ["secret", "token"]


@pytest.mark.parametrize("config", [None, {}, {"type": "push"},
                                    {"type": "unknown"}])
def test_a_push_person_has_nothing_to_test(config):
    result = verify.source(config)
    assert result.ok is True
    assert "Nothing to test" in result.message


# ------------------------------------------------------------ glucocore ----
#
# A display pairs with a six-digit code rather than an account password,
# so what is checked here is the code: its shape before anything is sent,
# and that GlucoCore's deliberate refusal to say *why* a code failed is
# repeated rather than guessed at.

@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "    "])
def test_a_code_the_wrong_shape_is_not_sent(monkeypatch, code):
    monkeypatch.setattr(glucocore, "claim", _never_called)
    result, claimed = verify.glucocore_claim(code, "mac-abc")
    assert result.ok is False
    assert "6 digits" in result.message
    assert claimed == {}


def test_a_code_typed_with_spaces_is_still_a_code(monkeypatch):
    """Nobody reads six digits out in one breath."""
    seen = {}

    def claim(code, hardware_id, name="", timeout=30):
        seen["code"] = code
        return {"deviceToken": "device-token", "device": {"id": "dev-42"}}

    monkeypatch.setattr(glucocore, "claim", claim)
    result, claimed = verify.glucocore_claim("123 456", "mac-abc")
    assert result.ok is True
    assert seen["code"] == "123456"
    assert claimed["deviceToken"] == "device-token"


def test_a_device_with_no_hardware_id_cannot_pair(monkeypatch):
    monkeypatch.setattr(glucocore, "claim", _never_called)
    result, _claimed = verify.glucocore_claim("123456", "")
    assert result.ok is False


def test_a_refused_code_says_the_one_thing_that_helps(monkeypatch):
    """Wrong, expired, spent and too-fast all answer alike, on purpose."""
    monkeypatch.setattr(glucocore, "claim", _raise(http_error(400)))
    result, claimed = verify.glucocore_claim("123456", "mac-abc")
    assert result.ok is False
    assert "make a new one in GlucoCore" in result.message
    assert claimed == {}


def test_a_claim_that_answers_without_a_token_is_not_a_pairing(monkeypatch):
    monkeypatch.setattr(glucocore, "claim",
                        lambda *a, **k: {"device": {"id": "dev-42"}})
    result, claimed = verify.glucocore_claim("123456", "mac-abc")
    assert result.ok is False
    assert claimed == {}


def test_an_unreachable_glucocore_does_not_blame_a_typo(monkeypatch):
    """Nobody typed this address, so there is no spelling to check."""
    monkeypatch.setattr(glucocore, "claim",
                        _raise(urllib.error.URLError(
                            socket.gaierror(-2, "Name or service not known"))))
    result, _claimed = verify.glucocore_claim("123456", "mac-abc")
    assert result.ok is False
    assert "spelling" not in result.message
    assert glucocore.GLUCOCORE_BASE in result.message
    assert "Name or service not known" in result.detail


def test_a_mistyped_nightscout_address_still_says_to_check_it(monkeypatch):
    """The other half of the same branch: there, the address *was* typed."""
    monkeypatch.setattr(nspull, "probe",
                        _raise(urllib.error.URLError("no route")))
    result = verify.nightscout_site("ns.exampl.invalid", "key")
    assert "check the spelling" in result.message


def test_the_code_is_never_shown_back(monkeypatch):
    """It is single-use, but it is still somebody's key to an account."""
    monkeypatch.setattr(glucocore, "claim",
                        _raise(RuntimeError("claim failed for 123456")))
    result, _claimed = verify.glucocore_claim("123456", "mac-abc")
    assert "123456" not in result.message + result.detail
    assert "***" in result.detail


def test_a_slow_service_hands_back_no_pairing(monkeypatch):
    """A worker that overran must not leak a token nobody is waiting on."""
    monkeypatch.setattr(glucocore, "claim", lambda *a, **k: time.sleep(30))
    result, claimed = verify.glucocore_claim("123456", "mac-abc", timeout=0.3)
    assert result.ok is False
    assert claimed == {}


def test_the_same_code_twice_in_a_row_is_throttled(monkeypatch):
    """The service answers a rate-limit exactly as it answers a wrong code."""
    monkeypatch.setattr(glucocore, "claim",
                        lambda *a, **k: {"deviceToken": "t", "device": {}})
    first, _ = verify.glucocore_claim("123456", "mac-abc")
    second, _ = verify.glucocore_claim("123456", "mac-abc")
    assert first.ok is True
    assert second.ok is False
    assert "wait" in second.message
