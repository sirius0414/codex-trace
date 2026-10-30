"""Portable release checks use only synthetic sources and disposable directories."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import build_release as release
finally:
    sys.path.pop(0)


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-trace-release-中文 空格-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.assets = self.base / "assets"
        self.assets.mkdir()
        (self.assets / "app.js").write_text('document.getElementById("root").textContent="synthetic";', encoding="utf-8")
        (self.assets / "style.css").write_text("body{margin:0}", encoding="utf-8")
        (self.assets / "build-info.json").write_text('{"version":"1.2.3","builtAt":"1970-01-01T00:00:00.000Z"}', encoding="utf-8")
        sources = {
            "package.json": '{"version":"1.2.3"}',
            "package-lock.json": '{"version":"1.2.3","packages":{"":{"version":"1.2.3"}}}',
            "requirements.lock": "mcp==1.30.0\n",
            "backend/codex_trace/__init__.py": '__version__ = "1.2.3"\n',
            "backend/codex_trace/app.py": 'print("synthetic backend")\n',
            "plugin/.codex-plugin/plugin.json": '{"name":"codex-trace","version":"1.2.3"}',
            "plugin/assets/icon.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
            "contracts/trajectory.schema.json": '{"type":"object"}',
            "contracts/tools.json": '{}',
            "LICENSE": "Synthetic project license\n", "NOTICE": "Synthetic notice\n",
            "install.py": 'print("synthetic installer")\n', "install.sh": '#!/bin/sh\n',
            "install.ps1": '# synthetic installer\n', "INSTALL.md": '# Synthetic instructions\n',
            **{"scripts/" + name: '# synthetic helper\n' for name in release.SCRIPTS},
        }
        for name, value in sources.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        legal = {"LICENSES/python/mcp@1.30.0/LICENSE": b"Synthetic upstream license\r\n",
                 "THIRD_PARTY_NOTICES.txt": b"Synthetic dependency notices\n"}
        manifest = {"schemaVersion": 1,
            "lockfiles": {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest() for name in ("package-lock.json", "requirements.lock")},
            "components": [{"ecosystem": "python", "location": "mcp", "name": "mcp", "version": "1.30.0",
                            "files": ["LICENSES/python/mcp@1.30.0/LICENSE"]}],
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in legal.items()}}
        for name, value in legal.items():
            path = self.source / "third_party" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.source / "third_party/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def build(self, name="out"):
        return release.build_release(self.base / name, root=self.source, assets=self.assets)

    def test_repeat_build_has_identical_archive_and_verifiable_payload(self):
        first, second = self.build("first"), self.build("second")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        self.assertEqual(self.build("first"), first)
        checksum = Path(first["checksums"]).read_text(encoding="ascii")
        self.assertEqual(checksum, f"{first['sha256']}  codex-trace-1.2.3.zip\n")
        with zipfile.ZipFile(first["archive"]) as archive:
            prefix = "codex-trace-1.2.3/"
            metadata = json.loads(archive.read(prefix + "release.json"))
            self.assertEqual(set(archive.namelist()), {prefix + name for name in metadata["files"]} | {prefix + "release.json"})
            for name, expected in metadata["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(prefix + name)).hexdigest(), expected)
            self.assertNotIn(prefix + "plugin/.mcp.json", archive.namelist())
            self.assertEqual(archive.getinfo(prefix + "install.sh").external_attr >> 16, 0o100755)

    def test_allowlist_excludes_environment_tasks_cache_and_arbitrary_plugin_config(self):
        for name in (".env", "auth.json", "session.jsonl", "plugin/.mcp.json", "plugin/tasks.json", "backend/codex_trace/task.json"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("DO_NOT_SHIP_SYNTHETIC_DATA", encoding="utf-8")
        result = self.build()
        self.assertNotIn(b"DO_NOT_SHIP_SYNTHETIC_DATA", Path(result["archive"]).read_bytes())

    def test_version_mismatch_or_unsafe_version_is_rejected_without_output(self):
        for version in ("2.0.0", "../escape"):
            (self.source / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Version mismatch|safe semantic"):
                self.build()
            self.assertFalse((self.base / "out").exists())

    def test_different_content_cannot_replace_existing_version(self):
        first = self.build()
        before = Path(first["archive"]).read_bytes()
        (self.assets / "app.js").write_text('console.log("changed")', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different content"):
            self.build()
        self.assertEqual(Path(first["archive"]).read_bytes(), before)

    def test_missing_input_symlink_and_credentials_fail_before_archive(self):
        source = self.source / "backend/codex_trace/app.py"
        for content in (f'ROOT = {str(self.source)!r}\n', 'TOKEN = "sk-proj-' + 'x' * 24 + '"\n', 'PATH = "/Users/synthetic/secret"\n'):
            source.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "absolute path|credential"):
                self.build()
        source.unlink()
        try:
            source.symlink_to(self.source / "LICENSE")
        except OSError as error:
            self.skipTest(f"Symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build()
        self.assertFalse((self.base / "out").exists())

    def test_legal_document_tamper_is_rejected(self):
        (self.source / "third_party/LICENSES/python/mcp@1.30.0/LICENSE").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "license validation failed"):
            self.build()


if __name__ == "__main__":
    unittest.main()
