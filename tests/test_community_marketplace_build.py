"""社区 Git 市场分发约束：真实白名单组包、精确校验和受管理目录替换。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_build_release as release_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import build_community_marketplace as community
finally:
    sys.path.pop(0)


class CommunityMarketplaceBuildTests(unittest.TestCase):
    def setUp(self):
        fixture = release_fixture.ReleaseBuildTests("test_repeat_build_has_identical_archive_and_verifiable_payload")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.base, self.source, self.assets = fixture.base, fixture.source, fixture.assets
        self.output = self.source / "plugins/codex-trace"
        additions = {"plugin/plugin.json": '{"name":"codex-trace","version":"1.2.3"}',
                     "plugin/launch.py": '# synthetic relocatable launcher\n',
                     community.CATALOG_PATH: json.dumps(community.catalog())}
        for name, content in additions.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def build(self, *, check=False):
        return community.build_community(root=self.source, assets=self.assets, check=check)

    def test_real_payload_is_prebuilt_and_uv_configuration_does_not_bind_to_builder(self):
        result = self.build()
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["marketplace"], "codex-trace-community")
        files = community.check_regular_tree(self.output)
        self.assertIn("backend/codex_trace/app.py", files)
        self.assertIn("contracts/trajectory.schema.json", files)
        self.assertIn("ui/trajectory-v1.html", files)
        self.assertIn("LICENSES/python/mcp@1.30.0/LICENSE", files)
        self.assertTrue(set(community.RELEASE_DOCUMENTS) <= files.keys())
        self.assertNotIn("install.py", files)
        self.assertFalse(any(name.startswith(("runtime/", "runtime-deps/", "node_modules/")) for name in files))
        legacy = json.loads(files[".mcp.json"])["mcpServers"]["codex-trace"]
        modern = json.loads(files["mcp.json"])["mcpServers"]["codex-trace"]
        self.assertEqual(modern, {"type": "stdio", **legacy, "cwd": "${PLUGIN_ROOT}",
            "args": [item.replace("./", "${PLUGIN_ROOT}/", 1) if item.startswith("./") else item for item in legacy["args"]]})
        self.assertEqual(modern["args"][6], "${PLUGIN_ROOT}/requirements.lock")
        self.assertEqual(modern["args"][-1], "${PLUGIN_ROOT}/launch.py")
        self.assertEqual(legacy["command"], "uv")
        self.assertEqual(legacy["args"], ["run", "--no-project", "--no-config", "--python", "3.14.2",
            "--with-requirements", "./requirements.lock", "--", "python", "-I", "-B",
            "./launch.py"])
        self.assertEqual(legacy["cwd"], ".")
        self.assertNotIn("${PLUGIN_ROOT}", json.dumps(legacy))
        self.assertNotIn(str(self.base).encode(), b"".join(files.values()))
        metadata = json.loads(files["runtime.json"])
        self.assertFalse(metadata["runtimeBundled"])
        self.assertTrue(metadata["startupDependencyPreparation"])
        self.assertTrue(metadata["startupNetworkInstallation"])
        self.assertEqual(metadata["runtimeManagement"], "uv")
        self.assertEqual(metadata["platforms"], ["darwin", "win32"])
        manifest = json.loads(files[community.PACKAGE_MANIFEST])
        self.assertEqual(set(files), set(manifest["files"]) | {community.PACKAGE_MANIFEST})
        for name, digest in manifest["files"].items():
            self.assertEqual(community.digest(files[name]), digest, name)

    def test_repeat_generation_and_check_are_identical(self):
        first = self.build()
        original = community.check_regular_tree(self.output)
        second = self.build()
        checked = self.build(check=True)
        self.assertEqual(first["packageManifestSha256"], second["packageManifestSha256"])
        self.assertEqual(community.check_regular_tree(self.output), original)
        self.assertTrue(checked["checked"])

    def test_check_rejects_modified_missing_and_extra_files_without_changing_them(self):
        for kind in ("modified", "missing", "extra"):
            with self.subTest(kind=kind):
                self.build()
                path = self.output / "SUPPORT.md"
                if kind == "modified":
                    path.write_text("hand edited generated file")
                elif kind == "missing":
                    path.unlink()
                else:
                    (self.output / "old-version.py").write_text("stale artifact")
                before = community.check_regular_tree(self.output)
                with self.assertRaisesRegex(ValueError, "stale"):
                    self.build(check=True)
                self.assertEqual(community.check_regular_tree(self.output), before)

    def test_regeneration_removes_stale_files_and_includes_new_source(self):
        self.build()
        (self.output / "obsolete.py").write_text("previous version")
        (self.source / "backend/codex_trace/new_module.py").write_text("VALUE = 2\n")
        self.build()
        self.assertFalse((self.output / "obsolete.py").exists())
        self.assertEqual((self.output / "backend/codex_trace/new_module.py").read_text(), "VALUE = 2\n")
        self.assertTrue(self.build(check=True)["passed"])

    def test_output_ownership_and_links_are_checked_before_replacement(self):
        self.output.mkdir(parents=True)
        (self.output / "personal.txt").write_text("keep me")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.build()
        self.assertEqual((self.output / "personal.txt").read_text(), "keep me")
        (self.output / "personal.txt").unlink()
        self.output.rmdir()
        self.build()
        try:
            (self.output / "link.txt").symlink_to(self.source / "LICENSE")
        except OSError:
            self.skipTest("Symbolic link creation is unavailable on this host")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build()
        self.assertTrue((self.output / "link.txt").is_symlink())

    def test_source_directory_symlinks_are_not_silently_omitted(self):
        try:
            (self.source / "backend/codex_trace/linked").symlink_to(self.source / "contracts", target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation is unavailable on this host")
        with self.assertRaisesRegex(ValueError, "source contains a symlink"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_manifest_version_or_catalog_changes_fail_before_generating(self):
        (self.source / "plugin/plugin.json").write_text('{"name":"codex-trace","version":"2.0.0"}')
        with self.assertRaisesRegex(ValueError, "manifest must match"):
            self.build()
        (self.source / community.CATALOG_PATH).write_text('{"name":"other-market"}')
        with self.assertRaisesRegex(ValueError, "catalog differs"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_failed_directory_switch_restores_previous_generation(self):
        self.build()
        before = community.check_regular_tree(self.output)
        original_rename = Path.rename

        def fail_new(source, target):
            if source.name == "new":
                raise OSError("synthetic rename failure")
            return original_rename(source, target)

        with mock.patch.object(Path, "rename", fail_new):
            with self.assertRaisesRegex(OSError, "synthetic rename"):
                self.build()
        self.assertEqual(community.check_regular_tree(self.output), before)


if __name__ == "__main__":
    unittest.main()
