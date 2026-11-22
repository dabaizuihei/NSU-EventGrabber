import unittest

from updater import extract_version, is_newer, parse_version


class VersionCompareTest(unittest.TestCase):
    def test_version_forms_normalize(self) -> None:
        self.assertEqual(parse_version("v1.0"), (1, 0, 0))
        self.assertEqual(parse_version("NSU-EventGrabber 1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("latest"), ())

    def test_extract_version_prefers_release_title(self) -> None:
        self.assertEqual(extract_version("NSU-EventGrabber 1.0.0"), "1.0.0")
        self.assertEqual(extract_version("v1.2.3"), "1.2.3")
        self.assertEqual(extract_version("latest"), "")

    def test_newer_versions(self) -> None:
        self.assertTrue(is_newer("1.0.1", "1.0.0"))
        self.assertTrue(is_newer("NSU-EventGrabber 1.1.0", "1.0.0"))
        self.assertFalse(is_newer("1.0.0", "1.0.0"))
        self.assertFalse(is_newer("0.9.9", "1.0.0"))
        self.assertFalse(is_newer("latest", "1.0.0"))
