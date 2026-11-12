import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from browser import EXTRACT
from browser_setup import digest, prepare
from run_state import ContractError


class BrowserBundleTests(unittest.TestCase):
    def test_wrong_platform_has_structured_error(self):
        with patch("browser_setup.platform.system", return_value="Darwin"), self.assertRaises(ContractError) as caught:
            prepare("/tmp/not-used")
        self.assertEqual(caught.exception.code, "browser_requires_linux_x86_64")

    def test_digest_stream(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "file"
            path.write_bytes(b"abc")
            self.assertEqual(digest(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_dom_extraction_has_actual_properties_and_scope_guard(self):
        self.assertIn("node.currentSrc", EXTRACT)
        self.assertIn("getComputedStyle", EXTRACT)
        self.assertIn("['BODY','HTML']", EXTRACT)
        self.assertNotIn("document.body.innerHTML", EXTRACT)


if __name__ == "__main__":
    unittest.main()
