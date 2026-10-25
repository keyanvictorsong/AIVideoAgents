"""A fresh installation must fail before browser startup without selectors."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xhs_publisher.browser import BrowserStepError, run_prepare, validate_cover_config


class PortableSetupTests(unittest.TestCase):
    def test_missing_selector_does_not_start_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "profile"
            with patch("sys.stdin.isatty", return_value=True), patch("playwright.sync_api.sync_playwright") as browser:
                with self.assertRaisesRegex(BrowserStepError, "首次使用"):
                    run_prepare({}, profile, selector_config={})
            browser.assert_not_called()
            self.assertFalse(profile.exists())

    def test_all_six_cover_selectors_are_required(self):
        names = ("cover_trigger", "cover_dialog", "cover_input", "cover_imported", "cover_confirm", "cover_ready")
        config = {name: "#synthetic-fixture" for name in names}
        validate_cover_config(config)
        for missing in names:
            with self.subTest(missing=missing), self.assertRaises(BrowserStepError):
                validate_cover_config({name: value for name, value in config.items() if name != missing})


if __name__ == "__main__":
    unittest.main()
