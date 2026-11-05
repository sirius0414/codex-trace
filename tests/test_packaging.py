"""Package/browser boundaries using synthetic assets and disposable directories."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


packaging = load_script("package_plugin")
verification = load_script("verify_install")


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-trace-package-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "app.js").write_text('document.getElementById("root").textContent="synthetic";')
        (self.assets / "custom-name.css").write_text('body{margin:0}')

    def test_rejects_browser_bundle_with_unreplaced_node_environment(self):
        for reference in ("process.env.NODE_ENV", "process . env . NODE_ENV"):
            with self.subTest(reference=reference):
                (self.assets / "app.js").write_text(f'if ({reference} !== "production") console.log("development");')
                with self.assertRaisesRegex(ValueError, r"process\.env\.NODE_ENV"):
                    packaging.offline_html(self.assets)

    def test_inlines_assets_with_no_external_resources_and_strips_source_maps(self):
        (self.assets / "app.js").write_text('console.log("<link href=example> and </script><script src=https://invalid.example>");\n//# sourceMappingURL=app.js.map')
        (self.assets / "custom-name.css").write_text('body{margin:0}/*# sourceMappingURL=style.css.map */')
        html = packaging.offline_html(self.assets)
        assets = verification.HtmlAssetCheck()
        assets.feed(html)
        self.assertTrue(assets.passed)
        self.assertEqual(assets.scripts, 1)
        self.assertEqual(assets.styles, 1)
        self.assertNotIn("sourceMappingURL", html)
        self.assertIn(r"<\/script>", html)
        self.assertNotIn(str(self.assets), html)

    def test_rejects_remote_css_dependencies(self):
        for css in ('@import "local-but-not-inlined.css";', 'body{background:url(https://invalid.example/x.png)}', "body{background:url('//invalid.example/x.png')}"):
            with self.subTest(css=css):
                (self.assets / "custom-name.css").write_text(css)
                with self.assertRaisesRegex(ValueError, "remote dependency"):
                    packaging.offline_html(self.assets)

    def test_requires_exactly_one_css_asset(self):
        (self.assets / "custom-name.css").unlink()
        with self.assertRaisesRegex(ValueError, "found 0"):
            packaging.offline_html(self.assets)
        (self.assets / "first.css").write_text("")
        (self.assets / "second.css").write_text("")
        with self.assertRaisesRegex(ValueError, "found 2"):
            packaging.offline_html(self.assets)

    def test_html_verifier_rejects_real_remote_tags_but_not_script_strings(self):
        good = '<style>body{}</style><div id="root"></div><script>console.log("<link href=example>");</script>'
        check = verification.HtmlAssetCheck(); check.feed(good)
        self.assertTrue(check.passed)
        for resource in ('<script src="https://invalid.example/x.js"></script>', '<link rel="stylesheet" href="x.css">', '<img src="https://invalid.example/x.png">'):
            with self.subTest(resource=resource):
                check = verification.HtmlAssetCheck(); check.feed(good + resource)
                self.assertFalse(check.passed)

    def test_lock_requires_exact_pins_and_mcp(self):
        lock = self.root / "requirements.lock"
        for contents in ("mcp>=1.0\n", "mcp\n", "jsonschema==4.25.1\n"):
            with self.subTest(contents=contents):
                lock.write_text(contents)
                with self.assertRaises(ValueError):
                    packaging.locked_versions(lock)
        lock.write_text("# synthetic exact pins\nmcp==1.30.0\njsonschema==4.25.1\n")
        self.assertEqual(packaging.locked_versions(lock), {"mcp": "1.30.0", "jsonschema": "4.25.1"})

    def test_platform_lock_pins_are_filtered_for_runtime_without_losing_exact_versions(self):
        lock = self.root / "requirements.lock"
        lock.write_text("mcp==1.30.0\npywin32==311 ; sys_platform == 'win32'\n")
        self.assertEqual(packaging.locked_versions(lock, platform="darwin"), {"mcp": "1.30.0"})
        self.assertEqual(packaging.locked_versions(lock, platform="win32"), {"mcp": "1.30.0", "pywin32": "311"})
        for unsupported in ('pywin32>=311', 'pywin32==311 ; python_version >= "3.14"',
                            'pywin32==311 ; sys_platform != "darwin"',
                            'pywin32==311 ; sys_platform == "win32" or os_name == "nt"'):
            with self.subTest(pin=unsupported):
                lock.write_text("mcp==1.30.0\n" + unsupported + "\n")
                with self.assertRaises(ValueError):
                    packaging.locked_versions(lock, platform="win32")

    def test_runtime_mismatch_is_rejected_before_packaging(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"python": "3.14.2", "versionInfo": [3, 14, 2], "zstdAvailable": True, "packages": {"mcp": "1.29.0"}}), "")
        with mock.patch.object(packaging.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Runtime does not match"):
                packaging.verify_runtime(Path(sys.executable), {"mcp": "1.30.0"})

    def test_runtime_requires_python_314_and_usable_zstd(self):
        for version, available in (([3, 13, 9], True), ([3, 14, 2], False)):
            with self.subTest(version=version, zstd=available):
                result = subprocess.CompletedProcess([], 0, json.dumps({"versionInfo": version, "zstdAvailable": available, "packages": {"mcp": "1.30.0"}}), "")
                with mock.patch.object(packaging.subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(ValueError, "compression.zstd"):
                        packaging.verify_runtime(Path(sys.executable), {"mcp": "1.30.0"})

    def test_venv_interpreter_symlink_is_preserved(self):
        interpreter = self.root / "venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        try:
            interpreter.symlink_to(sys.executable)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        self.assertEqual(packaging.executable(interpreter), interpreter)
        self.assertNotEqual(packaging.executable(interpreter), interpreter.resolve())
        missing = self.root / "missing-python"
        with self.assertRaises(ValueError):
            packaging.executable(missing)

    def prepare_source(self):
        source = self.root / "source"
        backend = source / "backend/codex_trace"
        backend.mkdir(parents=True)
        (backend / "__init__.py").write_text("")
        (backend / "app.py").write_text('from pathlib import Path\nprint(Path(__file__).resolve())\n')
        (backend / "__pycache__").mkdir()
        (backend / "__pycache__/do-not-ship.pyc").write_bytes(b"synthetic cache")
        (source / "contracts").mkdir()
        (source / "contracts/trajectory.schema.json").write_text('{"type":"object"}')
        manifest = source / "plugin/.codex-plugin"
        manifest.mkdir(parents=True)
        (manifest / "plugin.json").write_text('{"name":"codex-trace","version":"0.0.0-test"}')
        (source / "requirements.lock").write_text("mcp==1.30.0\n")
        (source / "package-lock.json").write_text('{"packages": {"": {"name": "synthetic"}}}')
        (source / "LICENSE").write_text("synthetic license")
        (source / "NOTICE").write_text("synthetic project notice")
        legal_files = {"LICENSES/python/mcp@1.30.0/LICENSE": b"Synthetic upstream permission and copyright\r\n",
                       "LICENSES/python/mcp@1.30.0/NOTICE": b"Synthetic upstream attribution\n",
                       "THIRD_PARTY_NOTICES.txt": b"Synthetic third-party inventory\n"}
        inventory = {"schemaVersion": 1,
                     "lockfiles": {name: hashlib.sha256((source / name).read_text(encoding="utf-8").encode("utf-8")).hexdigest() for name in ("package-lock.json", "requirements.lock")},
                     "components": [{"ecosystem": "python", "location": "mcp", "name": "mcp", "version": "1.30.0",
                                     "files": [name for name in legal_files if name.startswith("LICENSES/")]}],
                     "files": {name: hashlib.sha256(data).hexdigest() for name, data in legal_files.items()}}
        for name, data in legal_files.items():
            path = source / "third_party" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (source / "third_party/manifest.json").write_text(json.dumps(inventory))
        (source / ".env").write_text("SYNTHETIC_SECRET_MUST_NOT_SHIP=yes\n")
        (source / "unrelated-session.jsonl").write_text("SYNTHETIC_UNRELATED_CONTENT\n")
        return source

    def test_existing_output_is_never_overwritten(self):
        source = self.prepare_source()
        output = self.root / "existing-package"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep existing contents")
        with mock.patch.object(packaging, "ROOT", source), mock.patch.object(packaging, "verify_runtime", return_value={"python": "synthetic", "packages": {}}):
            with self.assertRaisesRegex(ValueError, "Output already exists"):
                packaging.build(output, Path(sys.executable), Path(sys.executable), self.assets)
        self.assertEqual(sentinel.read_text(), "keep existing contents")
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_local_package_keeps_legacy_manifest_without_portable_root_entrypoints(self):
        source = self.prepare_source()
        (source / "plugin/plugin.json").write_text('{"name":"portable-root-manifest"}', encoding="utf-8")
        (source / "plugin/launch.py").write_text("raise RuntimeError('portable-only')\n", encoding="utf-8")
        output = self.root / "local-package"
        with mock.patch.object(packaging, "ROOT", source), mock.patch.object(packaging, "verify_runtime", return_value={}):
            packaging.build(output, Path(sys.executable), Path(sys.executable), self.assets)
        self.assertEqual(json.loads((output / ".codex-plugin/plugin.json").read_text())["name"], "codex-trace")
        self.assertFalse((output / "plugin.json").exists())
        self.assertFalse((output / "launch.py").exists())
        self.assertTrue((output / ".mcp.json").is_file())
        self.assertFalse((output / "mcp.json").exists())

    def test_package_imports_after_source_removed_and_excludes_unrelated_files(self):
        source = self.prepare_source()
        output = self.root / "package"
        with mock.patch.object(packaging, "ROOT", source), mock.patch.object(packaging, "verify_runtime", return_value={"python": "synthetic", "packages": {}}):
            packaging.build(output, Path(sys.executable), Path(sys.executable), self.assets)
        cache = self.root / "installed-cache"
        shutil.copytree(output, cache)
        shutil.rmtree(source)
        shutil.rmtree(output)
        shutil.rmtree(self.assets)
        launch = json.loads((cache / ".mcp.json").read_text())["mcpServers"]["codex-trace"]
        self.assertEqual(launch["cwd"], "./backend")
        self.assertEqual(launch["args"], ["-m", "codex_trace.app"])
        self.assertTrue(Path(launch["command"]).is_absolute())
        self.assertNotIn(str(source), json.dumps(launch))
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run([launch["command"], *launch["args"]], cwd=cache / launch["cwd"], env=environment, text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(Path(result.stdout.strip()), cache / "backend/codex_trace/app.py")
        shipped = [path.relative_to(cache).as_posix() for path in cache.rglob("*") if path.is_file()]
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in shipped))
        self.assertNotIn(".env", shipped)
        self.assertNotIn("unrelated-session.jsonl", shipped)
        self.assertTrue((cache / "ui/trajectory-v1.html").is_file())
        # 移走源码和 node_modules 后仍能读到完整上游授权，不能只保留 SPDX 标识。
        self.assertEqual((cache / "NOTICE").read_text(), "synthetic project notice")
        self.assertEqual((cache / "LICENSES/python/mcp@1.30.0/LICENSE").read_bytes(), b"Synthetic upstream permission and copyright\r\n")
        self.assertEqual((cache / "LICENSES/python/mcp@1.30.0/NOTICE").read_text(), "Synthetic upstream attribution\n")
        inventory = json.loads((cache / "third-party-manifest.json").read_text())
        for name, digest in inventory["files"].items():
            self.assertEqual(hashlib.sha256((cache / name).read_bytes()).hexdigest(), digest)
        for path in cache.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SYNTHETIC_SECRET_MUST_NOT_SHIP", path.read_bytes())
                self.assertNotIn(b"SYNTHETIC_UNRELATED_CONTENT", path.read_bytes())

    def test_missing_notices_and_stale_inventory_never_publish_a_package(self):
        source = self.prepare_source()
        for relative in ("NOTICE", "third_party/LICENSES/python/mcp@1.30.0/NOTICE", "requirements.lock"):
            with self.subTest(relative=relative):
                path = source / relative
                original = path.read_bytes()
                if relative.endswith(".lock"):
                    path.write_bytes(b"mcp==1.30.1\n")
                else:
                    path.unlink()
                output = self.root / "must-not-publish"
                with mock.patch.object(packaging, "ROOT", source), mock.patch.object(packaging, "verify_runtime", return_value={}):
                    with self.assertRaisesRegex(ValueError, "Third-party license validation failed"):
                        packaging.build(output, Path(sys.executable), Path(sys.executable), self.assets)
                self.assertFalse(output.exists())
                path.write_bytes(original)

    def test_production_cache_smoke_is_fully_synthetic_and_isolated(self):
        output = self.root / "package"
        packaging.build(output, Path(sys.executable), Path(sys.executable), self.assets)
        cache = self.root / "cache 中文 空格"
        shutil.copytree(output, cache)
        shutil.rmtree(output)
        shutil.rmtree(self.assets)
        checks = []
        forbidden = self.root / "must-not-be-created"
        original_run = subprocess.run
        encoded_path_probes = []
        def windows_ansi_path_probe(command, *args, **kwargs):
            command = list(command)
            if "-c" in command:
                index = command.index("-c") + 1
                if "codex_trace.app.__file__" in command[index]:
                    # 模拟隔离 Windows Python 的管道编码，不能依赖 CI 主机恰好启用 UTF-8。
                    command[index] = "import sys;sys.stdout.reconfigure(encoding='cp1252',errors='strict');" + command[index]
                    encoded_path_probes.append(True)
            return original_run(command, *args, **kwargs)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(forbidden / "home"),
                "CODEX_TRACE_STATE_ROOT": str(forbidden / "index"), "CODEX_TRACE_EXPORT_ROOT": str(forbidden / "exports"),
                "CODEX_TRACE_UI_PATH": str(forbidden / "ui.html"), "PYTHONPATH": str(forbidden / "modules")}), \
                mock.patch.object(verification.subprocess, "run", side_effect=windows_ansi_path_probe):
            report = verification.verify_cache(cache, self.root, checks)
        self.assertEqual(encoded_path_probes, [True])
        self.assertTrue(all(c["passed"] for c in checks), checks)
        self.assertGreater(len(checks), 25)
        self.assertEqual(report["pluginVersion"], json.loads((ROOT / "package.json").read_text())["version"])
        self.assertEqual(set(report["historyMethods"]), verification.READ_METHODS)
        self.assertFalse(forbidden.exists())
        self.assertNotIn(verification.SYNTHETIC_BODY, json.dumps(report) + json.dumps(checks))
        self.assertTrue((self.root / "index").is_dir())
        self.assertEqual(len(list((self.root / "Downloads/Codex Trace").glob("*.zip"))), 1)

    def test_release_versions_and_lock_metadata_agree(self):
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        manifest = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
        from codex_trace import __version__
        self.assertEqual({package["version"], lock["version"], lock["packages"][""]["version"], manifest["version"], __version__}, {package["version"]})

    def test_stderr_privacy_check_scans_whole_stream_without_retaining_text(self):
        client = object.__new__(verification.RpcClient)
        client.process = mock.Mock(stderr=iter(["prefix" * 200 + verification.SYNTHETIC_BODY + "\n", *(["later diagnostic\n"] * 20)]))
        client.stderr_contains_synthetic_body = False
        client.stderr_lines = 0
        client._stderr()
        self.assertTrue(client.stderr_contains_synthetic_body)
        self.assertEqual(client.stderr_lines, 21)
        self.assertNotIn(verification.SYNTHETIC_BODY, repr({k: v for k, v in vars(client).items() if k != "process"}))


if __name__ == "__main__":
    unittest.main()
