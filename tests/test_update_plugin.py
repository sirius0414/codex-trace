"""Update transactions use disposable packages and never touch the user registry."""
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
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("update_plugin", ROOT / "scripts/update_plugin.py")
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
finally:
    sys.path.pop(0)


class UpdatePluginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-trace-update-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.output = self.root / "update"
        self.package = self.output / "plugin"
        self.package.mkdir(parents=True)
        self.launch = {"command": sys.executable, "args": ["-m", "codex_trace.app"],
                       "cwd": "./backend", "env": {"PYTHONDONTWRITEBYTECODE": "1"}}
        files = {
            ".codex-plugin/plugin.json": json.dumps({"name": update.PLUGIN, "version": "0.1.0"}),
            ".mcp.json": json.dumps({"mcpServers": {update.PLUGIN: self.launch}}),
            "requirements.lock": "mcp==1.30.0\n",
            "ui/trajectory-v1.html": "<html>new UI</html>",
            "backend/codex_trace/app.py": "# synthetic app\n",
        }
        for relative, content in files.items():
            path = self.package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.marketplace = self.output / "marketplace"
        shutil.copytree(self.package, self.marketplace / "plugins" / update.PLUGIN)
        self.catalog = self.marketplace / ".agents/plugins/marketplace.json"
        self.catalog.parent.mkdir(parents=True)
        update.write_json(self.catalog, {"name": update.MARKET, "plugins": [{"name": update.PLUGIN,
            "source": {"source": "local", "path": "./plugins/codex-trace"}}]})
        self.expected = update.fingerprints(self.package)
        update.write_json(self.output / "verification.json", {"passed": True})
        update.write_json(self.output / "update-plan.json", {"schemaVersion": 1,
            "codexHome": str(self.home), "python": sys.executable, "codex": sys.executable,
            "catalogSha256": hashlib.sha256(self.catalog.read_bytes()).hexdigest(),
            "files": self.expected})
        self.old_root = self.root / "previous-marketplace"
        self.previous = {"name": update.MARKET, "root": str(self.old_root),
                         "marketplaceSource": {"sourceType": "local", "source": str(self.old_root)}}
        self.cache = self.home / "plugins/cache" / update.MARKET / update.PLUGIN / "0.1.0"
        self.receipt = {"pluginId": update.SELECTOR, "version": "0.1.0", "installedPath": str(self.cache)}
        self.server = {"name": update.PLUGIN, "enabled": True,
                       "transport": {**self.launch, "cwd": str(self.cache / "backend")}}
        self.quiet = contextlib.ExitStack()
        self.quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(self.quiet.close)

    def cli(self, _codex, *arguments):
        if arguments == ("plugin", "marketplace", "list", "--json"):
            return {"marketplaces": [self.previous]}
        if arguments == ("plugin", "add", update.SELECTOR, "--json"):
            shutil.copytree(self.package, self.cache)
            return self.receipt
        if arguments == ("mcp", "get", update.PLUGIN, "--json"):
            return self.server
        self.fail(f"Unexpected CLI invocation: {arguments}")

    def test_changed_package_or_marketplace_refuses_before_cli(self):
        for root in (self.package, self.marketplace / "plugins" / update.PLUGIN):
            with self.subTest(root=root):
                target = root / "ui/trajectory-v1.html"
                original = target.read_text()
                target.write_text("changed after verification")
                with mock.patch.object(update, "cli_json") as cli, mock.patch.object(update, "run") as run:
                    with self.assertRaisesRegex(ValueError, "发生变化"):
                        update.install(self.output)
                cli.assert_not_called()
                run.assert_not_called()
                target.write_text(original)

    def test_changed_marketplace_catalog_refuses_before_cli(self):
        self.catalog.write_text('{"name":"unexpected-marketplace","plugins":[]}')
        with mock.patch.object(update, "cli_json") as cli, mock.patch.object(update, "run") as run:
            with self.assertRaises(ValueError):
                update.install(self.output)
        cli.assert_not_called()
        run.assert_not_called()

    def test_success_checks_cache_and_mcp_and_writes_report(self):
        with mock.patch.object(update, "verify_runtime"), mock.patch.object(update, "run", return_value="") as run, \
                mock.patch.object(update, "cli_json", side_effect=self.cli) as cli:
            report = update.install(self.output)
        self.assertEqual(run.call_args_list, [
            mock.call([sys.executable, "plugin", "marketplace", "remove", update.MARKET]),
            mock.call([sys.executable, "plugin", "marketplace", "add", str(self.marketplace)]),
        ])
        self.assertEqual(cli.call_args_list[-1], mock.call(Path(sys.executable), "mcp", "get", update.PLUGIN, "--json"))
        self.assertTrue(report["passed"])
        self.assertTrue(report["mcpCwdVerified"])
        self.assertFalse(report["hostUiReloaded"])
        self.assertEqual(report["verifiedFileCount"], len(self.expected))
        self.assertEqual(report["uiSha256"], self.expected["ui/trajectory-v1.html"])
        self.assertEqual(json.loads((self.output / "installed.json").read_text()), report)
        self.assertFalse((self.output / "install-failed.json").exists())

    def test_failed_marketplace_add_restores_previous_registration(self):
        commands = []

        def run(command):
            commands.append(command)
            if command == [sys.executable, "plugin", "marketplace", "add", str(self.marketplace)]:
                raise RuntimeError("synthetic add failure")
            return ""

        with mock.patch.object(update, "verify_runtime"), mock.patch.object(update, "run", side_effect=run), \
                mock.patch.object(update, "cli_json", side_effect=self.cli):
            with self.assertRaisesRegex(RuntimeError, "synthetic add failure"):
                update.install(self.output)
        self.assertEqual(commands[-1], [sys.executable, "plugin", "marketplace", "add", str(self.old_root)])
        failure = json.loads((self.output / "install-failed.json").read_text())
        self.assertTrue(failure["marketplaceRegistrationRestored"])
        self.assertFalse(failure["cacheRestored"])
        self.assertFalse(self.cache.exists())

    def test_postinstall_hash_mismatch_restores_registration_without_claiming_cache_rollback(self):
        def cli(codex, *arguments):
            result = self.cli(codex, *arguments)
            if arguments == ("plugin", "add", update.SELECTOR, "--json"):
                (self.cache / "ui/trajectory-v1.html").write_text("unexpected stale cache")
            return result

        with mock.patch.object(update, "verify_runtime"), mock.patch.object(update, "run", return_value="") as run, \
                mock.patch.object(update, "cli_json", side_effect=cli):
            with self.assertRaisesRegex(ValueError, "安装缓存"):
                update.install(self.output)
        self.assertEqual(run.call_args_list[-2:], [
            mock.call([sys.executable, "plugin", "marketplace", "remove", update.MARKET]),
            mock.call([sys.executable, "plugin", "marketplace", "add", str(self.old_root)]),
        ])
        failure = json.loads((self.output / "install-failed.json").read_text())
        self.assertFalse(failure["passed"])
        self.assertTrue(failure["marketplaceRegistrationRestored"])
        self.assertFalse(failure["cacheRestored"])
        self.assertEqual((self.cache / "ui/trajectory-v1.html").read_text(), "unexpected stale cache")
        self.assertFalse((self.output / "installed.json").exists())

    def test_failed_plugin_add_removes_new_registration_and_restores_previous(self):
        def cli(codex, *arguments):
            if arguments == ("plugin", "add", update.SELECTOR, "--json"):
                raise RuntimeError("synthetic plugin installation failure")
            return self.cli(codex, *arguments)

        with mock.patch.object(update, "verify_runtime"), mock.patch.object(update, "run", return_value="") as run, \
                mock.patch.object(update, "cli_json", side_effect=cli):
            with self.assertRaisesRegex(RuntimeError, "synthetic plugin installation failure"):
                update.install(self.output)
        self.assertEqual(run.call_args_list[-2:], [
            mock.call([sys.executable, "plugin", "marketplace", "remove", update.MARKET]),
            mock.call([sys.executable, "plugin", "marketplace", "add", str(self.old_root)]),
        ])
        failure = json.loads((self.output / "install-failed.json").read_text())
        self.assertTrue(failure["marketplaceRegistrationRestored"])
        self.assertFalse(failure["cacheRestored"])
        self.assertFalse(self.cache.exists())

    def test_mcp_configuration_mismatch_rejects_installation(self):
        self.server["transport"]["cwd"] = str(self.old_root / "backend")
        with mock.patch.object(update, "verify_runtime"), mock.patch.object(update, "run", return_value=""), \
                mock.patch.object(update, "cli_json", side_effect=self.cli):
            with self.assertRaisesRegex(ValueError, "MCP"):
                update.install(self.output)
        self.assertFalse((self.output / "installed.json").exists())

    def test_different_codex_home_refuses_before_cli(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "other-home")}), \
                mock.patch.object(update, "cli_json") as cli, mock.patch.object(update, "run") as run:
            with self.assertRaisesRegex(ValueError, "CODEX_HOME"):
                update.install(self.output)
        cli.assert_not_called()
        run.assert_not_called()

    def test_update_plan_from_other_platform_refuses_before_cli(self):
        plan = self.output / "update-plan.json"
        value = json.loads(plan.read_text(encoding="utf-8"))
        value["platform"] = "win32" if sys.platform != "win32" else "darwin"
        update.write_json(plan, value)
        with mock.patch.object(update, "cli_json") as cli, mock.patch.object(update, "run") as run:
            with self.assertRaisesRegex(ValueError, "其他操作系统"):
                update.install(self.output)
        cli.assert_not_called()
        run.assert_not_called()

    def test_package_symlink_refuses_fingerprint(self):
        link = self.package / "linked-ui.html"
        try:
            link.symlink_to(self.package / "ui/trajectory-v1.html")
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "重解析点"):
            update.fingerprints(self.package)

    def test_runtime_discovery_uses_selected_home_and_preserves_venv_symlink(self):
        interpreter = self.root / "runtime/bin/python"
        interpreter.parent.mkdir(parents=True)
        try:
            interpreter.symlink_to(sys.executable)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        self.cache.mkdir(parents=True)
        update.write_json(self.cache / "runtime.json", {"runtimePython": str(interpreter),
                          "appServerExecutable": sys.executable})
        python, codex = update.runtime_paths(None, None)
        self.assertEqual(python, interpreter)
        self.assertNotEqual(python, interpreter.resolve())
        self.assertEqual(codex, Path(sys.executable))

    def test_failed_build_preserves_stdout_diagnostics_in_log(self):
        log = self.output / "build.log"
        result = subprocess.CompletedProcess(["npm", "run", "build"], 1, "error TS2322: synthetic error\n", "build failed\n")
        with mock.patch.object(update.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "完整日志"):
                update.run(["npm", "run", "build"], log_path=log)
        self.assertIn("TS2322", log.read_text())
        self.assertIn("build failed", log.read_text())

    def test_failed_configuration_command_does_not_print_json_stdout(self):
        result = subprocess.CompletedProcess(["codex", "mcp", "get"], 1, "SYNTHETIC_PRIVATE_CONFIGURATION", "configuration read failed")
        with mock.patch.object(update.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as error:
                update.run(["codex", "mcp", "get"])
        self.assertNotIn("SYNTHETIC_PRIVATE_CONFIGURATION", str(error.exception))


if __name__ == "__main__":
    unittest.main()
