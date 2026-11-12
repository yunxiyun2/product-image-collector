import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from browser import EXTRACT
from browser_setup import assemble_archive, digest, prepare
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

    def test_split_archive_reassembly(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "runtime"
            output.mkdir()
            data = b"browser archive test"
            files = []
            for index, chunk in enumerate((data[:7], data[7:])):
                relative = f"part{index}"
                (root / relative).write_bytes(chunk)
                files.append({"path": relative, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
            catalog = {"archive_parts": [item["path"] for item in files], "files": files, "archive_bytes": len(data), "archive_sha256": hashlib.sha256(data).hexdigest()}
            merged = assemble_archive(root, catalog, output)
            self.assertEqual(merged.read_bytes(), data)
            (root / "part0").write_bytes(b"invalid")
            with self.assertRaises(ContractError):
                assemble_archive(root, catalog, output)
            self.assertFalse((output / "chrome-linux64.zip.partial").exists())

    def test_archive_wrong_order_rejected(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_bytes(b"a")
            (root / "b").write_bytes(b"b")
            files = [{"path": key, "bytes": 1, "sha256": hashlib.sha256(key.encode()).hexdigest()} for key in ("a", "b")]
            catalog = {"archive_parts": ["b", "a"], "files": files, "archive_bytes": 2, "archive_sha256": hashlib.sha256(b"ab").hexdigest()}
            with self.assertRaises(ContractError):
                assemble_archive(root, catalog, root)

    def test_dom_extraction_has_actual_properties_and_scope_guard(self):
        self.assertIn("node.currentSrc", EXTRACT)
        self.assertIn("getComputedStyle", EXTRACT)
        self.assertIn("['BODY','HTML']", EXTRACT)
        self.assertNotIn("document.body.innerHTML", EXTRACT)


if __name__ == "__main__":
    unittest.main()
