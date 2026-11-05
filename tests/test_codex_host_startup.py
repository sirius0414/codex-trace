"""Fail closed before a real supervisor can access an unisolated profile."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import verify_codex_host_startup as host
finally:
    sys.path.pop(0)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class HostStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="host-test-中文 空格-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "isolated home"
        self.codex_home = self.home / ".codex"
        self.installed = self.codex_home / "plugins/cache/codex-trace-community/codex-trace/0.2.0"
        self.installed.mkdir(parents=True)
        self.codex = self.root / ("codex.exe" if os.name == "nt" else "codex")
        self.uv = self.root / ("uv.exe" if os.name == "nt" else "uv")
        for executable in (self.codex, self.uv):
            executable.write_text("synthetic executable is never launched", encoding="utf-8")
            executable.chmod(0o700)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home),
                    "USERPROFILE": str(self.home), "CODEX_HOME": str(self.codex_home)}
        self.launch = {"type": "stdio", "command": "uv", "cwd": "${PLUGIN_ROOT}",
                       "args": ["run", "--no-project", "--no-config", "--python", "3.14.2",
                                "--with-requirements", "${PLUGIN_ROOT}/requirements.lock", "--",
                                "python", "-I", "-B", "${PLUGIN_ROOT}/launch.py"],
                       "env": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}}
        write_json(self.installed / "mcp.json", {"mcpServers": {"codex-trace": self.launch}})
        write_json(self.installed / "plugin.json", {"name": "codex-trace", "version": "0.2.0"})
        contracts = json.loads((Path(__file__).resolve().parents[1] / "contracts/tools.json").read_text(encoding="utf-8"))
        write_json(self.installed / "contracts/tools.json", contracts)
        for filename in ("launch.py", "requirements.lock"):
            (self.installed / filename).write_text("# synthetic\n", encoding="utf-8")
        self.cache = self.home / ".cache/uv"
        self.python = self.home / ".local/share/uv/python"
        self.status = {"name": "codex-trace", "pluginId": "codex-trace@codex-trace-community",
                       "serverInfo": {"name": "codex-trace", "version": "0.2.0"},
                       "tools": {name: {} for name in [*contracts["tools"], "open_codex_trace_panel"]},
                       "resources": [{"uri": "ui://codex-trace/trajectory-v1.html"},
                                     {"uri": "ui://codex-trace/trajectory-panel-v1.html"}]}
        self.checks = []
        self.requests = []
        self.processes = []

    def run_command(self, command, cwd, environment, timeout=60):
        self.processes.append((command, cwd, environment))
        if command[1:] == ["--no-config", "cache", "dir"]:
            output = str(self.cache)
        elif command[1:] == ["--no-config", "python", "dir"]:
            output = str(self.python)
        elif command[1:] == ["mcp", "list", "--json"]:
            output = json.dumps([{"name": "codex-trace", "transport": {
                "command": "uv", "cwd": str(self.installed),
                "args": [arg.replace("${PLUGIN_ROOT}", str(self.installed)) for arg in self.launch["args"]]}}])
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def rpc_client(self, command, cwd, env):
        self.processes.append((command, cwd, env))
        test = self

        class Client:
            stderr_lines = 0

            def request(self, method, params, timeout):
                test.requests.append((method, params))
                if method == "initialize":
                    return {"codexHome": str(test.codex_home)}
                if method == "mcpServerStatus/list":
                    return {"data": [test.status], "nextCursor": None}
                raise AssertionError("No other host requests are allowed")

            def send(self, method, params):
                test.requests.append((method, params))

            def close(self):
                pass

        return Client()

    def verify(self):
        return host.verify_host_startup(self.installed, self.root, self.codex, self.uv, self.env, self.checks)

    def test_success_filters_credentials_and_uses_real_host_protocol_only(self):
        self.env.update({"OPENAI_API_KEY": "never-forward", "GH_TOKEN": "never-forward",
                         "CODEX_API_KEY": "never-forward", "CODEX_TRACE_STATE_ROOT": "/real/state",
                         "UV_CACHE_DIR": "/external/cache", "UV_PYTHON_INSTALL_DIR": "/external/python",
                         "UV_OFFLINE": "1", "XDG_CACHE_HOME": "/real/cache", "PYTHONPATH": "/real/python",
                         "GIT_CONFIG_GLOBAL": "/real/gitconfig", "TMPDIR": "/real/tmp"})
        original = dict(self.env)
        with patch.object(host, "_run", side_effect=self.run_command), patch.object(host, "RpcClient", side_effect=self.rpc_client):
            result = self.verify()
        self.assertEqual(self.env, original)
        self.assertTrue(result["passed"])
        self.assertTrue(all(check["passed"] for check in self.checks))
        self.assertFalse(result["coldStartVerified"])
        self.assertFalse(result["hostUiVerified"])
        self.assertFalse(result["otherPlatformsVerified"])
        self.assertEqual([name for name, _ in self.requests], ["initialize", "initialized", "mcpServerStatus/list"])
        for command, cwd, environment in self.processes:
            self.assertTrue(cwd.is_relative_to(self.root))
            self.assertNotEqual(cwd, self.installed)
            self.assertFalse(any(cwd.iterdir()))
            self.assertNotIn("never-forward", environment.values())
            self.assertFalse(any(key.startswith(("UV_", "XDG_", "GIT_", "PYTHON")) for key in environment))
            self.assertNotIn("CODEX_TRACE_STATE_ROOT", environment)
            self.assertEqual(environment["CODEX_HOME"], str(self.codex_home))
            for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMPDIR", "TMP", "TEMP"):
                self.assertTrue(Path(environment[key]).is_relative_to(self.root))
        preparation = next(command for command, _, _ in self.processes if "-c" in command)
        self.assertEqual(preparation[-2:], ["-c", "import compression.zstd, mcp"])
        requirements = preparation[preparation.index("--with-requirements") + 1]
        self.assertEqual(Path(requirements), self.installed / "requirements.lock")
        self.assertFalse(any(Path(argument) == self.installed / "launch.py" for argument in preparation))
        self.assertEqual(self.processes[-1][0], [str(self.codex), "app-server"])
        self.assertEqual(result["installedFileChanges"], {"added": [], "removed": [], "changed": []})

    def test_external_or_missing_profile_is_rejected_before_any_program_runs(self):
        for key in ("HOME", "CODEX_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            with self.subTest(key=key), patch.object(host, "_run") as command, patch.object(host, "RpcClient") as rpc:
                with patch.dict(self.env, {key: str(self.root.parent / "real-profile")}):
                    with self.assertRaises(ValueError):
                        self.verify()
                command.assert_not_called()
                rpc.assert_not_called()
        self.env.pop("HOME")
        with patch.object(host, "_run") as command, self.assertRaises(ValueError):
            self.verify()
        command.assert_not_called()

    def test_installed_package_must_be_inside_isolated_codex_cache(self):
        self.installed = self.root / "elsewhere"
        self.installed.mkdir()
        with patch.object(host, "_run") as command, self.assertRaisesRegex(RuntimeError, "host_uses_isolated_installed_cache"):
            self.verify()
        command.assert_not_called()

    def test_auth_or_task_data_is_rejected_before_any_program_runs(self):
        for name in ("auth.json", "sessions", "archived_sessions"):
            path = self.codex_home / name
            path.write_text("must not be read", encoding="utf-8")
            try:
                with self.subTest(name=name), patch.object(host, "_run") as command, self.assertRaisesRegex(RuntimeError, "host_profile_has_no_auth_task_or_business_data"):
                    self.verify()
                command.assert_not_called()
            finally:
                path.unlink()

    def test_default_uv_directory_cannot_escape_isolation(self):
        for destination in (str(self.root.parent / "external-cache"), "relative/cache", str(self.root)):
            self.cache = destination
            with self.subTest(destination=destination), patch.object(host, "_run", side_effect=self.run_command) as command, \
                    patch.object(host, "RpcClient") as rpc, self.assertRaises(ValueError):
                self.verify()
            self.assertEqual(command.call_count, 1)
            rpc.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Symlink creation is not guaranteed on Windows test hosts")
    def test_default_uv_directory_cannot_be_a_link_even_inside_root(self):
        destination = self.root / "other-cache"
        destination.mkdir()
        self.cache.parent.mkdir(parents=True)
        self.cache.symlink_to(destination, target_is_directory=True)
        with patch.object(host, "_run", side_effect=self.run_command), patch.object(host, "RpcClient") as rpc, \
                self.assertRaisesRegex(ValueError, "host-profile-must-not-use-links"):
            self.verify()
        rpc.assert_not_called()

    def test_relative_launch_arguments_fail_before_dependency_preparation(self):
        self.launch["args"][-1] = "./launch.py"
        write_json(self.installed / "mcp.json", {"mcpServers": {"codex-trace": self.launch}})
        with patch.object(host, "_run") as command, self.assertRaisesRegex(RuntimeError, "host_uses_portable_modern_launch"):
            self.verify()
        command.assert_not_called()

    def test_unexpected_mcp_servers_fail_before_real_host_starts(self):
        def run(*args, **kwargs):
            result = self.run_command(*args, **kwargs)
            if args[0][1:] == ["mcp", "list", "--json"]:
                result.stdout = json.dumps([{"name": "another-server"}])
            return result

        with patch.object(host, "_run", side_effect=run), patch.object(host, "RpcClient") as rpc, \
                self.assertRaisesRegex(RuntimeError, "host_only_expected_mcp_server_is_configured"):
            self.verify()
        rpc.assert_not_called()

    def test_static_listing_without_initialized_server_is_not_a_pass(self):
        self.status.update(serverInfo=None, tools={}, resources=[])
        with patch.object(host, "_run", side_effect=self.run_command), patch.object(host, "RpcClient", side_effect=self.rpc_client), \
                self.assertRaisesRegex(RuntimeError, "host_received_mcp_server_identity"):
            self.verify()
        self.assertTrue(any(not check["passed"] for check in self.checks))

    def test_host_package_mutation_is_detected(self):
        def client(*args):
            value = self.rpc_client(*args)
            value.close = lambda: (self.installed / "unexpected-bytecode.pyc").write_bytes(b"unexpected")
            return value

        with patch.object(host, "_run", side_effect=self.run_command), patch.object(host, "RpcClient", side_effect=client), \
                self.assertRaisesRegex(RuntimeError, "host_installed_package_files_remain_unchanged"):
            self.verify()

    def test_failed_preparation_does_not_expose_child_output(self):
        failure = subprocess.CompletedProcess(["uv"], 7, stdout="PRIVATE_DATA", stderr="SECRET_TOKEN")
        with patch.object(host.subprocess, "run", return_value=failure), self.assertRaisesRegex(RuntimeError, "exit code 7") as error:
            host._run([str(self.uv), "run"], self.root, self.env)
        self.assertNotIn("PRIVATE_DATA", str(error.exception))
        self.assertNotIn("SECRET_TOKEN", str(error.exception))
