"""Tests for GlucoCore client URL helpers."""

import unittest

from glucocube.glucocore import GLUCOCORE_BASE


class GlucoCoreClientTest(unittest.TestCase):
    def test_base_url_has_no_trailing_slash(self):
        self.assertFalse(GLUCOCORE_BASE.endswith("/"))


if __name__ == "__main__":
    unittest.main()
