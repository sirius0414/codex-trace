"""授权原文收录、依赖变化和发行包损坏的合成检查，不读取真实任务。"""
from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import third_party_licenses as legal
finally:
    sys.path.pop(0)


class ThirdPartyLicenseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-trace-licenses-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "node_modules/@example/widget"
        self.package.mkdir(parents=True)
        (self.package / "package.json").write_text(json.dumps({"name": "@example/widget", "version": "2.0.0", "license": "MIT"}))
        (self.package / "LICENSE").write_bytes(b"Upstream combined Apache and MIT terms\r\nCopyright Example\r\n")
        (self.package / "NOTICE").write_bytes(b"Required widget attribution\n")
        (self.package / "LICENSES").mkdir()
        (self.package / "LICENSES/vendor.txt").write_text("Nested upstream vendor terms\n")
        (self.root / "NOTICE").write_text("Independent project and reference acknowledgments\n")
        (self.root / "package-lock.json").write_text(json.dumps({"packages": {
            "": {"name": "synthetic"}, "node_modules/@example/widget": {"version": "2.0.0", "license": "MIT"},
            "node_modules/dev-only": {"version": "9.0.0", "dev": True}}}))
        (self.root / "requirements.lock").write_text("example-py==1.2.3\n")
        metadata = Message()
        metadata["License-Expression"] = "BSD-3-Clause"
        metadata["Project-URL"] = "Source, https://example.invalid/source"
        self.python_files = [Path("example_py-1.2.3.dist-info/licenses/LICENSE"),
                             Path("example_py-1.2.3.dist-info/licenses/NOTICE")]
        for path, content in zip(self.python_files, (b"Python upstream license\n", b"Required Python attribution\n")):
            target = self.root / "installed" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.distribution = SimpleNamespace(version="1.2.3", metadata=metadata, files=self.python_files,
                                            locate_file=lambda name: self.root / "installed" / name)

    def snapshot(self):
        with mock.patch.object(legal.importlib.metadata, "distribution", return_value=self.distribution):
            manifest, files = legal.collect(self.root)
        directory = self.root / "third_party"
        for name, data in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return manifest, files

    def windows_wheel(self, *, bad_path=False):
        (self.root / "requirements.lock").write_text("example-py==1.2.3\npywin32==311 ; sys_platform == 'win32'\n")
        wheelhouse = self.root / "wheelhouse"
        wheelhouse.mkdir()
        with zipfile.ZipFile(wheelhouse / "pywin32-311-cp314-cp314-win_amd64.whl", "w") as wheel:
            wheel.writestr("pywin32-311.dist-info/METADATA", "Metadata-Version: 2.4\nName: pywin32\nVersion: 311\nLicense: PSF\n")
            wheel.writestr("win32/license.txt", b"Windows upstream license\r\n")
            wheel.writestr("win32com/License.txt", b"COM upstream license\r\n")
            if bad_path:
                wheel.writestr("../license.txt", b"Invalid path")
        return wheelhouse

    def distribution_for_platform(self, name):
        if name == "pywin32":
            raise legal.importlib.metadata.PackageNotFoundError(name)
        return self.distribution

    def write_snapshot(self, manifest, files):
        directory = self.root / "third_party"
        for name, data in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (directory / "manifest.json").write_text(json.dumps(manifest))

    def test_missing_platform_license_is_read_from_wheel_and_retained_in_inventory(self):
        wheelhouse = self.windows_wheel()
        with mock.patch.object(legal.importlib.metadata, "distribution", side_effect=self.distribution_for_platform):
            manifest, files = legal.collect(self.root, wheelhouse)
        component = next(item for item in manifest["components"] if item["name"] == "pywin32")
        self.assertEqual(component["marker"], "sys_platform == 'win32'")
        self.assertEqual(component["licenseArtifact"]["filename"], "pywin32-311-cp314-cp314-win_amd64.whl")
        self.assertEqual(files["LICENSES/python/pywin32@311/win32/license.txt"], b"Windows upstream license\r\n")
        self.assertEqual(files["LICENSES/python/pywin32@311/win32com/License.txt"], b"COM upstream license\r\n")
        self.write_snapshot(manifest, files)
        self.assertEqual(legal.check_snapshot(self.root), manifest)
        shutil.rmtree(wheelhouse)
        with mock.patch.object(legal.importlib.metadata, "distribution", side_effect=self.distribution_for_platform):
            reused_manifest, reused_files = legal.collect(self.root)
        self.assertEqual(reused_manifest, manifest)
        self.assertEqual(reused_files, files)

    def test_missing_platform_wheel_without_snapshot_explains_wheelhouse(self):
        self.windows_wheel()
        with mock.patch.object(legal.importlib.metadata, "distribution", side_effect=self.distribution_for_platform):
            with self.assertRaisesRegex(ValueError, "--wheelhouse"):
                legal.collect(self.root)

    def test_wheel_legal_paths_cannot_escape_snapshot(self):
        wheelhouse = self.windows_wheel(bad_path=True)
        with mock.patch.object(legal.importlib.metadata, "distribution", side_effect=self.distribution_for_platform):
            with self.assertRaisesRegex(ValueError, "Invalid third-party document path"):
                legal.collect(self.root, wheelhouse)

    def test_changed_platform_marker_or_corrupt_snapshot_cannot_be_reused(self):
        wheelhouse = self.windows_wheel()
        with mock.patch.object(legal.importlib.metadata, "distribution", side_effect=self.distribution_for_platform):
            manifest, files = legal.collect(self.root, wheelhouse)
            self.write_snapshot(manifest, files)
            (self.root / "requirements.lock").write_text("example-py==1.2.3\npywin32==311 ; sys_platform == 'darwin'\n")
            with self.assertRaisesRegex(ValueError, "--wheelhouse"):
                legal.collect(self.root)
            (self.root / "requirements.lock").write_text("example-py==1.2.3\npywin32==311 ; sys_platform == 'win32'\n")
            (self.root / "third_party/LICENSES/python/pywin32@311/win32/license.txt").write_text("changed")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                legal.collect(self.root)

    def test_collects_full_license_notice_and_nested_texts_without_trusting_spdx_hint(self):
        manifest, files = self.snapshot()
        self.assertEqual({item["name"] for item in manifest["components"]}, {"@example/widget", "example-py"})
        self.assertEqual(files["LICENSES/npm/@example/widget@2.0.0/LICENSE"], (self.package / "LICENSE").read_bytes())
        self.assertIn(b"Apache and MIT", files["LICENSES/npm/@example/widget@2.0.0/LICENSE"])
        self.assertEqual(files["LICENSES/npm/@example/widget@2.0.0/NOTICE"], b"Required widget attribution\n")
        self.assertIn("LICENSES/npm/@example/widget@2.0.0/LICENSES/vendor.txt", files)
        self.assertEqual(files["LICENSES/python/example-py@1.2.3/NOTICE"], b"Required Python attribution\n")
        self.assertEqual(legal.check_snapshot(self.root), manifest)

    def test_snapshot_checks_without_installed_dependencies_and_tolerates_git_newlines(self):
        manifest, _ = self.snapshot()
        shutil.rmtree(self.root / "node_modules")
        shutil.rmtree(self.root / "installed")
        path = self.root / "requirements.lock"
        # 明确从规范化文本生成 CRLF，避免 Windows 夹具已有 CRLF 被转成 CRCRLF。
        path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(legal.check_snapshot(self.root), manifest)

    def test_changed_dependency_requires_refresh(self):
        self.snapshot()
        (self.root / "requirements.lock").write_text("example-py==1.2.4\n")
        with self.assertRaisesRegex(ValueError, "snapshot is stale"):
            legal.check_snapshot(self.root)

    def test_matching_locks_cannot_hide_an_omitted_component(self):
        manifest, _ = self.snapshot()
        manifest["components"].pop()
        (self.root / "third_party/manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "does not cover"):
            legal.check_snapshot(self.root)

    def test_modified_or_removed_upstream_notice_is_rejected(self):
        self.snapshot()
        path = self.root / "third_party/LICENSES/npm/@example/widget@2.0.0/NOTICE"
        for content in (b"Truncated attribution", None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "missing or changed"):
                    legal.check_snapshot(self.root)

    def test_missing_license_or_different_installed_version_cannot_be_collected(self):
        with mock.patch.object(legal.importlib.metadata, "distribution", return_value=self.distribution):
            (self.package / "LICENSE").unlink()
            with self.assertRaisesRegex(ValueError, "Missing upstream license"):
                legal.collect(self.root)
            (self.package / "LICENSE").write_text("Upstream license restored")
            self.distribution.version = "1.2.4"
            with self.assertRaisesRegex(ValueError, "differs from lockfile"):
                legal.collect(self.root)

    def test_packaged_manifest_cannot_reference_paths_outside_license_directory(self):
        manifest, _ = self.snapshot()
        manifest["components"][0]["files"] = ["LICENSES/../../LICENSE"]
        manifest["files"]["LICENSES/../../LICENSE"] = "0" * 64
        # 删除原引用以确保验证抵达路径检查，而非先因孤立文件失败。
        referenced = {"THIRD_PARTY_NOTICES.txt"} | {name for item in manifest["components"] for name in item["files"]}
        manifest["files"] = {name: digest for name, digest in manifest["files"].items() if name in referenced}
        with self.assertRaisesRegex(ValueError, "Invalid third-party document path"):
            legal.verify_documents(self.root / "third_party", manifest)


if __name__ == "__main__":
    unittest.main()
