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

    def test_a_login_goes_to_the_services_own_host(self):
        sent = self._capture(
            lambda: glucocore.login("cassidy@example.invalid", "pw"),
            json.dumps({"userid": "u-1"}).encode())
        self.assertEqual(sent["url"], "https://glucocore.app/auth/login")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["returned"], ("session-token", "u-1"))

    def test_a_login_that_answers_without_a_token_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self._capture(
                lambda: glucocore.login("cassidy@example.invalid", "pw"),
                json.dumps({}).encode())

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

    def test_registering_sends_what_the_display_was_told_to_show(self):
        sent = self._capture(lambda: glucocore.register_device(
            "session-token", "Kitchen display", "mac-abc", ["pat-1"],
            config={"version": 1}))
        self.assertEqual(sent["url"], "https://glucocore.app/v1/sugar_cubes")
        body = json.loads(sent["body"])
        self.assertEqual(body["name"], "Kitchen display")
        self.assertEqual(body["hardwareId"], "mac-abc")
        self.assertEqual(body["patientIds"], ["pat-1"])
        self.assertEqual(body["config"], {"version": 1})


if __name__ == "__main__":
    unittest.main()
