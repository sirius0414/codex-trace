"""公共候选包的隔离构建约束；合成运行时不冒充真实平台运行证据。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import build_marketplace as build
    import prepare_marketplace_runtime as prepare
finally:
    sys.path.pop(0)


class PreparedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="marketplace-runtime-中文 空格-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.python = self.source / "bin/python3"
        self.python.parent.mkdir()
        self.python.write_bytes(b"synthetic interpreter, never executed")
        self.python.chmod(0o755)
        self.license = self.base / "LICENSE.cpython.txt"
        self.license.write_text("Synthetic runtime distribution license\n")
        self.distribution = self.base / "PYTHON.json"
        self.distribution.write_text(json.dumps({"python_implementation_name": "cpython", "python_version": "3.14.2",
            "license_path": "licenses/LICENSE.cpython.txt"}))
        self.runtime = {"pythonVersion": "3.14.2", "platform": "darwin", "architecture": "arm64", "zstdAvailable": True}

    def prepared(self, name="prepared"):
        with mock.patch.object(prepare, "probe_runtime", return_value=self.runtime):
            result = prepare.prepare_runtime(self.source, self.base / name,
                source_url="https://example.invalid/cpython-standalone.tar.gz", license_files=[self.license, self.distribution])
        return Path(result["runtime"])

    def test_venv_and_external_or_absolute_symlinks_are_rejected(self):
        (self.source / "pyvenv.cfg").write_text("home = /external/python\n")
        with self.assertRaisesRegex(ValueError, "virtualenv"):
            self.prepared()
        (self.source / "pyvenv.cfg").unlink()
        for target in (str(self.license), "../LICENSE.cpython.txt"):
            link = self.source / "outside"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("Symbolic link creation is unavailable on this host")
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.prepared()
            link.unlink()

    def test_internal_links_materialize_and_tampering_fails(self):
        try:
            (self.source / "bin/python").symlink_to("python3")
        except OSError:
            self.skipTest("Symbolic link creation is unavailable on this host")
        result = self.prepared()
        self.assertFalse((result / "bin/python").is_symlink())
        self.assertEqual((result / "bin/python").read_bytes(), self.python.read_bytes())
        record = json.loads((result / prepare.PROVENANCE).read_bytes())
        self.assertNotIn(str(self.base), json.dumps(record))
        self.assertTrue(record["licenses"])
        with mock.patch.object(prepare, "probe_runtime", return_value=self.runtime):
            self.assertEqual(prepare.validated_runtime(result), record)
            (result / "unlisted.py").write_text("unexpected code")
            with self.assertRaisesRegex(ValueError, "provenance"):
                prepare.validated_runtime(result)

    def test_sysconfig_prefix_is_dynamic_and_output_relocated_before_probe(self):
        data = "build_time_vars = " + repr({"prefix": str(self.source), "BINDIR": str(self.source / "bin"), "OTHER": 42})
        metadata = self.source / "lib/python3.14/_sysconfigdata_test.py"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(data)
        paths = []
        with mock.patch.object(prepare, "probe_runtime", side_effect=lambda path: paths.append(path) or self.runtime):
            output = prepare.prepare_runtime(self.source, self.base / "prepared",
                source_url="https://example.invalid/python.tar.gz", license_files=[self.license, self.distribution])
        result = Path(output["runtime"])
        content = (result / "lib/python3.14/_sysconfigdata_test.py").read_text()
        self.assertNotIn(str(self.source), content)
        namespace = {}
        exec(content, namespace)
        self.assertEqual(namespace["build_time_vars"]["prefix"], sys.base_prefix)
        self.assertEqual(namespace["build_time_vars"]["OTHER"], 42)
        self.assertEqual(paths[0].name, "relocated 运行时")

    def test_missing_license_or_credential_url_never_creates_output(self):
        for licenses, url in (([], "https://example.invalid/python.tgz"),
                              ([self.license], "https://example.invalid/python.tgz?secret=foo")):
            with self.assertRaises(ValueError):
                prepare.prepare_runtime(self.source, self.base / "prepared", source_url=url, license_files=licenses)
            self.assertFalse((self.base / "prepared").exists())

    def test_recursive_distribution_license_reference_must_be_complete(self):
        data = json.loads(self.distribution.read_text())
        data["build_info"] = {"extensions": [{"license_paths": ["licenses/LICENSE.zstd.txt"]}]}
        self.distribution.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "LICENSE.zstd.txt"):
            self.prepared()

    def test_runtime_output_may_not_be_inside_input_through_directory_alias(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation is unavailable on this host")
        with self.assertRaisesRegex(ValueError, "outside the source"):
            prepare.prepare_runtime(self.source, alias / "child", source_url="https://example.invalid/python.tgz",
                                    license_files=[self.license, self.distribution])

    def test_clean_environment_does_not_retain_real_profile_or_python_injection(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/outside", "CODEX_HOME": "/real",
                                        "CODEX_TRACE_EXPORT_ROOT": "/real/downloads"}):
            result = prepare.clean_environment(self.base)
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("CODEX_TRACE_EXPORT_ROOT", result)
        self.assertEqual(result["CODEX_HOME"], str(self.base / ".codex"))


class MarketplaceBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="marketplace-build-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.assets = self.base / "assets"
        self.assets.mkdir()
        self.runtime = self.base / "runtime"
        (self.runtime / "bin").mkdir(parents=True)
        (self.runtime / "bin/python3").write_bytes(b"synthetic native bytes")
        (self.runtime / "bin/python3").chmod(0o755)
        (self.runtime / prepare.PROVENANCE).write_text('{"synthetic":true}\n')
        self.wheels = self.base / "wheels"
        self.wheels.mkdir()
        self.provenance = {"platform": "darwin", "architecture": "arm64", "pythonVersion": "3.14.2"}
        manifest = {"name": "codex-trace", "version": "1.2.3", "interface": {"displayName": "Synthetic"}}
        for name, content in {"plugin/plugin.json": json.dumps(manifest), "plugin/launch.py": "# synthetic launcher\n",
                              "requirements.lock": "mcp==1.30.0\n",
                              **{name: '# Synthetic release documentation\n'
                                 for name in build.RELEASE_DOCUMENTS}}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        self.installer = {"install.py": b"PRIVATE_INSTALLER_ONLY",
                          "plugin/.codex-plugin/plugin.json": json.dumps(manifest).encode(),
                          "plugin/ui/trajectory-v1.html": b"<html>synthetic UI</html>",
                          "plugin/requirements.lock": b"mcp==1.30.0\n",
                          "plugin/third-party-manifest.json": b"{}\n"}

    def fake_dependencies(self, python, wheelhouse, requirements, target, home):
        target.mkdir()
        (target / "mcp.py").write_text("# synthetic dependency\n")

    def build(self, name="out", *, platform="darwin", architecture="arm64"):
        runtime = {**self.provenance, "platform": platform, "architecture": architecture}
        if platform == "win32":
            (self.runtime / "python.exe").write_bytes(b"synthetic Windows native bytes")
        with mock.patch.object(build, "validated_runtime", return_value=runtime), \
             mock.patch.object(build, "collect_payload", return_value=("1.2.3", self.installer)), \
             mock.patch.object(build, "install_dependencies", side_effect=self.fake_dependencies), \
             mock.patch.object(build, "probe_runtime", return_value=runtime), \
             mock.patch.object(build, "verify_dependencies", return_value={"lockedDependencyCount": 1}), \
             mock.patch.object(build, "verify_mcp_start") as mcp:
            result = build.build_marketplace(self.base / name, runtime=self.runtime, wheelhouse=self.wheels,
                                            root=self.root, assets=self.assets)
            self.assertEqual(mcp.call_args.args[0].name, "moved 插件 candidate")
        return result

    def test_archive_has_real_plugin_root_compatible_launch_and_no_installer(self):
        result = self.build()
        self.assertTrue(result["archive"].endswith("codex-trace-1.2.3-darwin-arm64.zip"))
        with zipfile.ZipFile(result["archive"]) as archive:
            names = set(archive.namelist())
            self.assertTrue({"plugin.json", "mcp.json", ".mcp.json", "launch.py", "runtime/bin/python3",
                             "runtime-deps/mcp.py", "PRIVACY.md", "SUPPORT.md", "TERMS.md"} <= names)
            self.assertNotIn("install.py", names)
            for name in build.RELEASE_DOCUMENTS:
                self.assertEqual(archive.read(name), b'# Synthetic release documentation\n')
            modern = json.loads(archive.read("mcp.json"))["mcpServers"]["codex-trace"]
            legacy = json.loads(archive.read(".mcp.json"))["mcpServers"]["codex-trace"]
            self.assertEqual(modern, {"type": "stdio", **legacy})
            self.assertEqual(legacy["command"], "./runtime/bin/python3")
            self.assertEqual(legacy["args"], ["-I", "-B", "${PLUGIN_ROOT}/launch.py"])
            if os.name != "nt":
                self.assertEqual(archive.getinfo("runtime/bin/python3").external_attr >> 16, 0o100755)
            metadata = json.loads(archive.read(build.PACKAGE_MANIFEST))
            self.assertEqual(names, set(metadata["files"]) | {build.PACKAGE_MANIFEST})
            for name, digest in metadata["files"].items():
                self.assertEqual(build.digest(archive.read(name)), digest)
            self.assertNotIn(str(self.base).encode(), archive.read("runtime.json"))

    def test_repeated_build_identical_but_different_versioned_content_cannot_overwrite(self):
        first, second = self.build("first"), self.build("second")
        self.assertEqual(first["sha256"], second["sha256"])
        before = Path(first["archive"]).read_bytes()
        self.installer["plugin/ui/trajectory-v1.html"] = b"<html>new UI</html>"
        with self.assertRaisesRegex(ValueError, "different content"):
            self.build("first")
        self.assertEqual(Path(first["archive"]).read_bytes(), before)

    def test_portable_manifest_version_mismatch_rejected(self):
        (self.root / "plugin/plugin.json").write_text('{"name":"codex-trace","version":"9.9.9"}')
        with self.assertRaisesRegex(ValueError, "Portable plugin manifest version"):
            self.build()

    def test_output_inside_runtime_is_rejected_before_copy(self):
        with self.assertRaisesRegex(ValueError, "outside the input runtime"):
            build.build_marketplace(self.runtime / "candidate", runtime=self.runtime, wheelhouse=self.wheels,
                                    root=self.root, assets=self.assets)

    def test_windows_configuration_uses_bundled_exe_and_declares_separate_target(self):
        result = self.build(platform="win32", architecture="x64")
        self.assertEqual(result["target"], "win32-x64")
        with zipfile.ZipFile(result["archive"]) as archive:
            command = json.loads(archive.read("mcp.json"))["mcpServers"]["codex-trace"]["command"]
            self.assertEqual(command, "./runtime/python.exe")
            self.assertIn("runtime/python.exe", archive.namelist())

    def test_portal_limits_fail_closed_before_publishing_archive(self):
        for field in ("MAX_ENTRIES", "MAX_ENTRY_BYTES", "MAX_EXPANDED_BYTES", "MAX_ARCHIVE_BYTES", "MAX_PATH_SEGMENTS"):
            with self.subTest(field=field), mock.patch.object(build, field, 1):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    self.build(field)
                self.assertEqual(list((self.base / field).glob("*.zip")), [])

    def test_case_or_unicode_path_collisions_are_rejected(self):
        path = self.runtime / "bin/python3"
        for first, second in (("Asset.svg", "asset.svg"), ("caf\u00e9.txt", "cafe\u0301.txt")):
            with self.subTest(first=first), mock.patch.object(build, "runtime_files", return_value=[
                    (first, path, 0o644), (second, path, 0o644)]):
                with self.assertRaisesRegex(ValueError, "collision"):
                    build.archive_entries(self.runtime)

    def test_runtime_pruning_retains_dynamic_code_and_licenses_with_verifiable_history(self):
        for name in ("lib/python3.14/test/test_sample.py", "lib/libpython3.14.a",
                     "lib/libpython3.14.dylib", "lib/python3.14/unittest/case.py", "LICENSES/cpython-runtime/LICENSE"):
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic runtime payload")
        original = (self.runtime / prepare.PROVENANCE).read_bytes()
        build.trim_build_only_runtime_files(self.runtime)
        record = json.loads((self.runtime / prepare.PROVENANCE).read_bytes())
        self.assertEqual(record["packaging"]["preparedProvenanceSha256"], build.digest(original))
        self.assertEqual(set(record["packaging"]["excludedBuildAndTestFiles"]),
                         {"lib/python3.14/test/test_sample.py", "lib/libpython3.14.a"})
        self.assertTrue((self.runtime / "lib/libpython3.14.dylib").exists())
        self.assertTrue((self.runtime / "lib/python3.14/unittest/case.py").exists())
        self.assertTrue((self.runtime / "LICENSES/cpython-runtime/LICENSE").exists())
        actual = {name: build.digest(path.read_bytes()) for name, path, _ in prepare.runtime_files(self.runtime)
                  if name != prepare.PROVENANCE}
        self.assertEqual(actual, record["files"])

    def test_console_script_record_hashes_are_removed_but_module_hashes_preserved(self):
        deps = self.base / "runtime-deps"
        record = deps / "sample-1.dist-info/RECORD"
        record.parent.mkdir(parents=True)
        (deps / "sample.py").write_text("# module\n")
        record.write_text("../../bin/sample,sha256=temporaryShebangHash,305\n"
                          "sample.py,sha256=originalModuleHash,9\n"
                          "sample-1.dist-info/RECORD,,\n")
        build.normalize_dependency_records(deps)
        self.assertEqual(record.read_text(), "sample.py,sha256=originalModuleHash,9\nsample-1.dist-info/RECORD,,\n")
        record.write_text("../../outside/data,sha256=mustNotDisappear,1\n")
        with self.assertRaisesRegex(ValueError, "external path"):
            build.normalize_dependency_records(deps)


if __name__ == "__main__":
    unittest.main()
