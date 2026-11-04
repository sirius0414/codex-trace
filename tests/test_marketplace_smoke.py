"""发布包验收先验证完整清单，损坏或恶意归档不得写入临时安装目录。"""
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from smoke_marketplace import extract_verified
finally:
    sys.path.pop(0)


class MarketplaceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "test.zip"
        self.destination = self.root / "output"

    def write_archive(self, *, path="runtime/bin/python3", tamper=False, symlink=False, extra=False):
        body = b"synthetic executable"
        manifest = {"files": {path: hashlib.sha256(body).hexdigest()}}
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("package-manifest.json", json.dumps(manifest))
            entry = zipfile.ZipInfo(path)
            entry.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o755) << 16
            archive.writestr(entry, b"modified" if tamper else body)
            if extra:
                archive.writestr("extra.py", b"unexpected code")

    def test_valid_archive_preserves_executable_and_bytes(self):
        self.write_archive()
        extract_verified(self.archive, self.destination)
        file = self.destination / "runtime/bin/python3"
        self.assertEqual(file.read_bytes(), b"synthetic executable")
        if sys.platform != "win32":
            self.assertEqual(file.stat().st_mode & 0o777, 0o755)

    def test_rejects_corrupt_file_before_extraction(self):
        self.write_archive(tamper=True)
        with self.assertRaisesRegex(ValueError, "checksum"):
            extract_verified(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_undeclared_file(self):
        self.write_archive(extra=True)
        with self.assertRaisesRegex(ValueError, "inventory"):
            extract_verified(self.archive, self.destination)
        self.assertFalse(self.destination.exists())

    def test_rejects_case_and_unicode_collisions_before_extraction(self):
        for first, second in (("A.py", "a.py"), ("café.py", "cafe\u0301.py")):
            with self.subTest(first=first):
                with zipfile.ZipFile(self.archive, "w") as archive:
                    archive.writestr(first, b"first")
                    archive.writestr(second, b"second")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    extract_verified(self.archive, self.destination)
                self.assertFalse(self.destination.exists())

    def test_rejects_traversal_absolute_windows_paths_and_links(self):
        for path, symlink in (("../escape", False), ("/absolute", False), ("C:/absolute", False),
                              ("a\\b", False), ("a/./b", False), ("runtime/link", True),
                              ("/".join(["deep"] * 21), False)):
            with self.subTest(path=path):
                self.write_archive(path=path, symlink=symlink)
                with self.assertRaisesRegex(ValueError, "path-or-file"):
                    extract_verified(self.archive, self.destination)
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
