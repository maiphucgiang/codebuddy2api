"""版本文件、应用版本及发布标签校验。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import converter
from scripts import check_version


class VersionTests(unittest.TestCase):
    def test_application_uses_version_file(self):
        version = check_version.check_version()
        self.assertEqual(converter.APP_VERSION, version)
        self.assertEqual(converter.app.version, version)
        self.assertEqual(converter.app.openapi()["info"]["version"], version)

    def test_matching_tag(self):
        version = check_version.check_version()
        self.assertEqual(check_version.check_version(f"refs/tags/v{version}"), version)
        self.assertEqual(check_version.check_version("refs/heads/main"), version)
        self.assertEqual(check_version.check_version("refs/pull/1/merge"), version)

    def test_mismatched_tag_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            path.write_text("1.0.0\n", encoding="utf-8")
            with patch.object(check_version, "VERSION_FILE", path):
                for ref in ["refs/tags/v1.0.1", "refs/tags/1.0.0", "refs/tags/v1.0.0-rc.1"]:
                    with self.subTest(ref=ref), self.assertRaises(ValueError):
                        check_version.check_version(ref)

    def test_invalid_version_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "VERSION"
            with patch.object(check_version, "VERSION_FILE", path):
                for value in ["", "v1.0.0", "1.0", "01.0.0", "1.0.0-rc.1", "1.0.0\nextra=true"]:
                    path.write_text(value, encoding="utf-8")
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        check_version.check_version()


if __name__ == "__main__":
    unittest.main(verbosity=2)
