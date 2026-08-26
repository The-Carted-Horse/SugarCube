"""The GlucoCore client's address, and the requests it builds from it.

The address is the one thing here nobody can correct from the device: it
is not a setting, it is not on any page, and a device that cannot resolve
it cannot pair at all. It shipped once pointing at a hostname that does
not exist, and every sign-in failed with "could not reach that address",
so it is pinned here rather than left to a constant nobody reads.
"""

import importlib
import json
import os
import unittest
from unittest import mock

from glucocube import glucocore


class GlucoCoreAddressTest(unittest.TestCase):
    def test_the_base_url_is_the_service(self):
        self.assertEqual(glucocore.GLUCOCORE_BASE, "https://glucocore.app")

    def test_base_url_has_no_trailing_slash(self):
        """Every path below is joined onto it directly."""
        self.assertFalse(glucocore.GLUCOCORE_BASE.endswith("/"))

    def test_the_address_can_be_overridden_for_a_staging_service(self):
        # Every other module holds this same module object, so the reload
        # has to be undone with the environment already back to normal —
        # otherwise the override leaks into whatever runs next.
        self.addCleanup(importlib.reload, glucocore)
        with mock.patch.dict(os.environ,
                             {"GLUCOCORE_BASE": "https://staging.example/"}):
            reloaded = importlib.reload(glucocore)
            self.assertEqual(reloaded.GLUCOCORE_BASE,
                             "https://staging.example")


class GlucoCoreRequestTest(unittest.TestCase):
    """What actually goes out, checked without going anywhere."""

    def _capture(self, fn, payload=b"{}"):
        sent = {}

        class Response:
            headers = {glucocore.SESSION_HEADER: "session-token"}

            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None, **kwargs):
            sent["url"] = request.full_url
            sent["method"] = request.get_method()
            sent["headers"] = dict(request.header_items())
            sent["body"] = request.data
            return Response()

        with mock.patch("urllib.request.urlopen", urlopen):
            sent["returned"] = fn()
        return sent

    def test_a_claim_goes_to_the_services_own_host(self):
        sent = self._capture(
            lambda: glucocore.claim("123456", "mac-abc", "Kitchen display"),
            json.dumps({"data": {"deviceToken": "device-token"}}).encode())
        self.assertEqual(sent["url"], "https://glucocore.app/v1/sugar_cubes/claim")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(json.loads(sent["body"]),
                         {"code": "123456", "hardwareId": "mac-abc",
                          "name": "Kitchen display"})
        self.assertEqual(sent["returned"], {"deviceToken": "device-token"})

    def test_a_claim_carries_nothing_that_could_authenticate_it(self):
        """The whole point of the code: a display has no credential yet."""
        sent = self._capture(
            lambda: glucocore.claim("123456", "mac-abc"),
            json.dumps({"data": {}}).encode())
        headers = {name.lower() for name in sent["headers"]}
        self.assertNotIn(glucocore.SESSION_HEADER, headers)
        self.assertNotIn("authorization", headers)

    def test_a_claim_without_a_name_does_not_send_an_empty_one(self):
        """Blank means "keep the name the account gave it"."""
        sent = self._capture(
            lambda: glucocore.claim("123456", "mac-abc"),
            json.dumps({"data": {}}).encode())
        self.assertNotIn("name", json.loads(sent["body"]))

    def test_the_device_token_travels_as_the_session_header(self):
        sent = self._capture(
            lambda: glucocore.get_config("device-token"))
        self.assertEqual(sent["url"],
                         "https://glucocore.app/v1/sugar_cubes/me/config")
        # urllib rewrites the case of header names, and HTTP does not care
        # about it either, so neither does this.
        headers = {name.lower(): value
                   for name, value in sent["headers"].items()}
        self.assertEqual(headers[glucocore.SESSION_HEADER], "device-token")

    def test_the_data_read_asks_for_the_types_the_display_draws(self):
        sent = self._capture(lambda: glucocore.fetch_patient_data(
            "device-token", "pat-1", "2026-01-01T00:00:00.000Z"), b"[]")
        self.assertIn("/data/pat-1", sent["url"])
        self.assertIn("type=cbg,bolus,food,dosingDecision", sent["url"])
        self.assertIn("startDate=2026-01-01T00:00:00.000Z", sent["url"])

    def test_an_envelope_is_unwrapped_and_a_bare_answer_is_not(self):
        """The service wraps most answers in `data` and a few not at all."""
        wrapped = self._capture(lambda: glucocore.get_config("t"),
                                json.dumps({"data": {"version": 3}}).encode())
        self.assertEqual(wrapped["returned"], {"version": 3})
        bare = self._capture(lambda: glucocore.get_config("t"),
                             json.dumps({"version": 3}).encode())
        self.assertEqual(bare["returned"], {"version": 3})


if __name__ == "__main__":
    unittest.main()
