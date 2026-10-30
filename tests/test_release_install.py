"""Release lifecycle contracts in disposable homes; no real Codex configuration.

The fake CLI models only documented plugin mutations. Runtime setup is replaced
at its boundary so failed dependency downloads and partial installs are
deterministic. Real runtime/CLI coverage lives in the opt-in release smoke test.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
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


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class FakeCodex:
    """A persistent isolated registry with failures after real cache mutation."""

    def __init__(self, home):
        self.home = home
        self.registry = home / "fake-marketplaces.json"
        self.commands = []
        self.fail_next_install = False
        self.corrupt_next_install = False
        self.fail_all_installs = False
        write_json(self.registry, {"unrelated": {"name": "unrelated", "root": str(home / "other-market"),
            "marketplaceSource": {"sourceType": "local"}}})

    def run(self, command, **kwargs):
        arguments = [str(part) for part in command[1:]]
        self.commands.append(arguments)
        registries = json.loads(self.registry.read_text(encoding="utf-8"))
        output = {}
        if arguments == ["--version"]:
            return "codex-cli 0.153.0\n"
        if "--help" in arguments:
            return "plugin marketplace add remove list --json\n"
        if arguments[:3] == ["plugin", "marketplace", "list"]:
            output = {"marketplaces": list(registries.values())}
        elif arguments[:3] == ["plugin", "marketplace", "add"]:
            root = Path(arguments[3])
            catalog = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
            name = catalog["name"]
            if name in registries:
                raise RuntimeError("marketplace already exists")
            registries[name] = {"name": name, "root": str(root),
                "marketplaceSource": {"sourceType": "local", "source": str(root)}}
            write_json(self.registry, registries)
        elif arguments[:3] == ["plugin", "marketplace", "remove"]:
            registries.pop(arguments[3], None)
            write_json(self.registry, registries)
        elif arguments[:2] == ["plugin", "add"]:
            name, market = arguments[2].split("@")
            source = Path(registries[market]["root"]) / "plugins" / name
            version = json.loads((source / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
            cache = self.home / "plugins/cache" / market / name / version
            if cache.exists():
                shutil.rmtree(cache)
            shutil.copytree(source, cache)
            write_json(self.home / "fake-installed.json", {"pluginId": arguments[2], "path": str(cache)})
            if self.fail_next_install or self.fail_all_installs:
                self.fail_next_install = False
                (cache / "ui/trajectory-v1.html").write_text("partial install", encoding="utf-8")
                raise RuntimeError("injected failure after cache mutation")
            if self.corrupt_next_install:
                self.corrupt_next_install = False
                (cache / "ui/trajectory-v1.html").write_text("stale cache", encoding="utf-8")
            output = {"pluginId": arguments[2], "name": name, "marketplaceName": market,
                "version": version, "installedPath": str(cache), "authPolicy": "ON_INSTALL"}
        elif arguments[:2] == ["plugin", "remove"]:
            name, market = arguments[2].split("@")
            cache = self.home / "plugins/cache" / market / name
            if cache.exists():
                shutil.rmtree(cache)
            (self.home / "fake-installed.json").unlink(missing_ok=True)
            output = {"pluginId": arguments[2], "name": name, "marketplaceName": market}
        elif arguments[:2] == ["mcp", "get"]:
            record = json.loads((self.home / "fake-installed.json").read_text(encoding="utf-8"))
            cache = Path(record["path"])
            launch = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][arguments[2]]
            output = {"name": arguments[2], "enabled": True,
                "transport": {**launch, "cwd": str((cache / launch["cwd"]).resolve())}}
        else:
            raise AssertionError(f"Unexpected Codex command: {arguments}")
        return json.dumps(output)


class ReleaseInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("release_installer_tests", ROOT / "install.py")
        cls.install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.install)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex trace 发布 lifecycle ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "user home"
        self.home.mkdir()
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        self.destination = self.home / "runtime installation"
        self.codex = self.home / "codex.exe"
        shutil.copyfile(sys.executable, self.codex)
        self.codex.chmod(0o700)
        self.sentinels = {
            self.codex_home / "plugins/cache/unrelated/plugin/1.0.0/important.txt": b"other plugin",
            self.codex_home / "sessions/session.jsonl": b"private task content",
            self.codex_home / "auth.json": b"credential sentinel",
            self.codex_home / "config.toml": b'[plugins."other@unrelated"]\nenabled = true\n',
            self.home / "Downloads/Codex Trace/export.zip": b"keep user export",
        }
        for path, value in self.sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.fake = FakeCodex(self.codex_home)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {"HOME": str(self.home),
            "USERPROFILE": str(self.home), "CODEX_HOME": str(self.codex_home),
            "LOCALAPPDATA": str(self.home / "AppData/Local")}))
        self.stack.enter_context(mock.patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(mock.patch.object(self.install.update, "run", side_effect=self.fake.run))
        self.stack.enter_context(mock.patch.object(self.install.update, "verify_runtime", return_value={}))
        self.stack.enter_context(mock.patch.object(self.install, "check_python"))
        self.stack.enter_context(mock.patch.object(self.install, "verify_backend"))
        self.runtime_calls = []
        self.stack.enter_context(mock.patch.object(self.install, "create_runtime", side_effect=self.create_runtime))

    def create_runtime(self, python, runtime, requirements, log):
        self.runtime_calls.append(runtime)
        interpreter = self.install.venv_python(runtime)
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sys.executable, interpreter)
        interpreter.chmod(0o700)
        return {"python": "3.14.0", "versionInfo": [3, 14, 0], "zstdAvailable": True,
            "packages": {"mcp": "1.30.0"}}

    def bundle(self, version="0.1.0"):
        bundle = self.root / ("release " + version)
        plugin = bundle / "plugin"
        manifest = {"name": "codex-trace", "version": version, "mcpServers": "./.mcp.json"}
        write_json(plugin / ".codex-plugin/plugin.json", manifest)
        write_json(plugin / "plugin.json", {"name": "codex-trace", "version": version})
        files = {
            "requirements.lock": "mcp==1.30.0\n",
            "ui/trajectory-v1.html": f"<html>release {version}</html>\n",
            "backend/codex_trace/__init__.py": "",
            "backend/codex_trace/app.py": "# isolated synthetic backend\n",
            "contracts/tools.json": "{}\n",
            "third-party-manifest.json": "{}\n",
        }
        for relative, contents in files.items():
            path = plugin / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        (bundle / "requirements.lock").write_text("mcp==1.30.0\n", encoding="utf-8")
        (bundle / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/prepare_marketplace.py", bundle / "scripts/prepare_marketplace.py")
        hashes = {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bundle.rglob("*") if path.is_file()}
        write_json(bundle / "release.json", {"schemaVersion": 1, "name": "codex-trace",
            "version": version, "minimumPython": "3.14", "platforms": ["darwin", "win32"],
            "runtimeBundled": False, "files": hashes})
        return bundle

    def perform_install(self, bundle):
        return self.install.install(bundle, self.destination, Path(sys.executable), self.codex)

    def assert_preserved(self):
        for path, value in self.sentinels.items():
            self.assertEqual(path.read_bytes(), value, str(path))
        registry = json.loads(self.fake.registry.read_text(encoding="utf-8"))
        self.assertIn("unrelated", registry)

    def current(self):
        return json.loads((self.destination / "current.json").read_text(encoding="utf-8"))

    def installed_cache(self):
        return Path(json.loads((self.codex_home / "fake-installed.json").read_text(encoding="utf-8"))["path"])

    def test_fresh_repeat_update_and_uninstall_preserve_unrelated_data(self):
        first = self.bundle()
        self.perform_install(first)
        cache = self.installed_cache()
        self.assertIn("release 0.1.0", (cache / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        launch = json.loads((cache / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["codex-trace"]
        self.assertTrue(Path(launch["command"]).is_relative_to(self.destination))
        self.assertNotIn(str(ROOT), json.dumps(launch))
        self.perform_install(first)
        self.assert_preserved()
        second = self.bundle("0.2.0")
        self.perform_install(second)
        self.assertIn("release 0.2.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assertTrue(all(path.exists() for path in self.runtime_calls), "live sessions retain old runtimes")
        self.install.uninstall(self.destination, self.codex)
        self.install.uninstall(self.destination, self.codex)
        self.assert_preserved()

    def test_new_identity_preserves_legacy_installation_state_and_exports(self):
        """更名后的安装和卸载只管理新标识，不接管旧市场、回执或导出。"""
        legacy = {
            self.codex_home / "plugins/cache/codex-display-local/codex-display/0.1.0/runtime.json": b"legacy runtime",
            self.home / ".local/share/codex-display/index/receipt.json": b"legacy pending export",
            self.home / "AppData/Local/codex-display/index/receipt.json": b"legacy Windows pending export",
            self.home / "Downloads/Codex Display/export.zip": b"legacy user export",
        }
        for path, value in legacy.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        registry = json.loads(self.fake.registry.read_text(encoding="utf-8"))
        for market in ("codex-display", "codex-display-local"):
            registry[market] = {"name": market, "root": str(self.home / market),
                "marketplaceSource": {"sourceType": "local"}}
        write_json(self.fake.registry, registry)
        self.perform_install(self.bundle())
        self.install.uninstall(self.destination, self.codex)
        for path, value in legacy.items():
            self.assertEqual(path.read_bytes(), value)
        self.assertEqual(json.loads(self.fake.registry.read_text(encoding="utf-8")), registry)
        self.assert_preserved()

    def test_dependency_failure_leaves_previous_install_and_can_retry(self):
        self.perform_install(self.bundle())
        previous = self.current()
        target = self.bundle("0.2.0")
        with mock.patch.object(self.install, "create_runtime", side_effect=RuntimeError("offline dependency index")):
            with self.assertRaises(RuntimeError) as error:
                self.perform_install(target)
            self.assertIn("offline", str(error.exception.__cause__))
        self.assertEqual(self.current(), previous)
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.perform_install(target)
        self.assertIn("release 0.2.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assert_preserved()

    def test_partial_cli_failure_reinstalls_previous_cache_and_retry_succeeds(self):
        self.perform_install(self.bundle())
        previous = self.current()
        target = self.bundle("0.2.0")
        self.fake.fail_next_install = True
        with self.assertRaises(RuntimeError):
            self.perform_install(target)
        self.assertEqual(self.current(), previous)
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.perform_install(target)
        self.assert_preserved()

    def test_rollback_failure_keeps_journal_for_explicit_recovery(self):
        self.perform_install(self.bundle())
        target = self.bundle("0.2.0")
        self.fake.fail_all_installs = True
        with self.assertRaises(Exception):
            self.perform_install(target)
        journal = json.loads((self.destination / "journal.json").read_text(encoding="utf-8"))
        self.assertIn("recover", json.dumps(journal).lower())
        self.fake.fail_all_installs = False
        self.install.recover(self.destination, self.codex)
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assert_preserved()

    def test_first_install_failure_is_recoverable_without_previous_version(self):
        self.fake.fail_next_install = True
        with self.assertRaises(RuntimeError):
            self.perform_install(self.bundle())
        self.install.recover(self.destination, self.codex)
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assert_preserved()

    def test_tampered_release_fails_before_codex_mutation(self):
        bundle = self.bundle()
        (bundle / "plugin/ui/trajectory-v1.html").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.perform_install(bundle)
        self.assertFalse(any(command[:2] in (["plugin", "add"], ["plugin", "remove"])
            or command[:3] in (["plugin", "marketplace", "add"], ["plugin", "marketplace", "remove"])
            for command in self.fake.commands))
        self.assert_preserved()

    def test_success_receipt_with_corrupt_cache_rolls_back(self):
        self.perform_install(self.bundle())
        self.fake.corrupt_next_install = True
        with self.assertRaises(RuntimeError):
            self.perform_install(self.bundle("0.2.0"))
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assert_preserved()

    def test_interrupted_install_journal_requires_recovery_and_restores_previous(self):
        self.perform_install(self.bundle())
        previous = self.current()["transaction"]
        second = self.bundle("0.2.0")
        self.perform_install(second)
        target = self.current()["transaction"]
        write_json(self.destination / "journal.json", {"phase": "installing", "previous": previous,
            "target": target, "codexHome": str(self.codex_home)})
        commands_before = len(self.fake.commands)
        with self.assertRaisesRegex(ValueError, "recover"):
            self.perform_install(second)
        self.assertEqual(len(self.fake.commands), commands_before)
        self.install.recover(self.destination, self.codex)
        self.assertIn("release 0.1.0", (self.installed_cache() / "ui/trajectory-v1.html").read_text(encoding="utf-8"))
        self.assert_preserved()

    def test_foreign_same_named_marketplace_is_not_replaced(self):
        registry = json.loads(self.fake.registry.read_text(encoding="utf-8"))
        registry["codex-trace"] = {"name": "codex-trace", "root": str(self.root / "someone else's market"),
            "marketplaceSource": {"sourceType": "local"}}
        write_json(self.fake.registry, registry)
        with self.assertRaisesRegex(ValueError, "市场"):
            self.perform_install(self.bundle())
        self.assertEqual(json.loads(self.fake.registry.read_text(encoding="utf-8")), registry)
        self.assertFalse(self.runtime_calls)
        self.assert_preserved()

    def test_first_install_recovers_orphaned_lock_and_partial_owner_write(self):
        # A crash before os.replace(owner.json.tmp, owner.json) must not make
        # the otherwise empty managed directory permanently unusable.
        self.destination.mkdir()
        (self.destination / ".install.lock").write_bytes(b"0")
        (self.destination / "owner.json.tmp").write_bytes(b'{"application":')
        result = self.perform_install(self.bundle())
        self.assertTrue(result["passed"])
        owner = json.loads((self.destination / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(owner, {"application": "codex-trace", "codexHome": str(self.codex_home.resolve())})
        self.assertFalse((self.destination / "owner.json.tmp").exists())
        self.assert_preserved()

    def test_orphan_lock_exception_does_not_claim_nonempty_user_directory(self):
        self.destination.mkdir()
        (self.destination / ".install.lock").write_bytes(b"0")
        unrelated = self.destination / "personal-file.txt"
        unrelated.write_bytes(b"keep this user file")
        with self.assertRaisesRegex(ValueError, "空的安装目录"):
            self.perform_install(self.bundle())
        self.assertEqual(unrelated.read_bytes(), b"keep this user file")
        self.assertFalse((self.destination / "owner.json").exists())
        self.assertFalse(self.runtime_calls)
        self.assert_preserved()

    def test_installation_root_cannot_be_reused_for_another_codex_home(self):
        release = self.bundle()
        self.perform_install(release)
        previous = self.current()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "another codex home")}):
            with self.assertRaisesRegex(ValueError, "CODEX_HOME"):
                self.perform_install(release)
            with self.assertRaisesRegex(ValueError, "CODEX_HOME"):
                self.install.uninstall(self.destination, self.codex)
        self.assertEqual(self.current(), previous)
        self.assert_preserved()

    def test_symlinked_payload_is_rejected_before_install(self):
        release = self.bundle()
        link = release / "plugin/ui/linked.html"
        try:
            link.symlink_to(release / "plugin/ui/trajectory-v1.html")
        except OSError as error:
            self.skipTest(f"Symlink privileges unavailable: {error}")
        with self.assertRaises(ValueError):
            self.perform_install(release)
        self.assertFalse(self.runtime_calls)
        self.assert_preserved()


class ReleasePrerequisiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("release_installer_prerequisites", ROOT / "install.py")
        cls.install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.install)

    def test_unsupported_python_explains_required_runtime(self):
        with mock.patch.object(self.install.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "python")):
            with self.assertRaisesRegex(ValueError, r"Python 3\.14.*venv.*compression.zstd"):
                self.install.check_python(Path(sys.executable))

    def test_missing_codex_explains_native_executable_override(self):
        with mock.patch.object(self.install, "executable", side_effect=ValueError("not installed")):
            with self.assertRaisesRegex(ValueError, r"Codex.*--codex.*codex.exe"):
                self.install.find_codex("missing-codex")


if __name__ == "__main__":
    unittest.main()
