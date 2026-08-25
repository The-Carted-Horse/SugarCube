"""Onboarding step sequence for GlucoCore pairing."""

import unittest
from unittest.mock import MagicMock

from glucocube import onboarding


class OnboardingStepsTest(unittest.TestCase):
    def test_steps_after_login_skip_verify_email(self):
        draft = {
            "wifi": {"skipped": True},
            "account": {"pending_verification": False},
        }
        steps = onboarding.steps_for(draft)
        self.assertIn("account", steps)
        self.assertNotIn("verify_email", steps)
        self.assertIn("patients", steps)
        self.assertNotIn("people", steps)

    def test_steps_for_signup_include_verify(self):
        draft = {
            "wifi": {"skipped": True},
            "account": {"pending_verification": True},
        }
        steps = onboarding.steps_for(draft)
        self.assertIn("verify_email", steps)


if __name__ == "__main__":
    unittest.main()
