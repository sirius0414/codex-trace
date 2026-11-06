"""End-user installation prepares dependencies before enabling and checks real MCP."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("community_installer", ROOT / "install_community.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CommunityInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="installer 中文 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.source = self.codex_home / ".tmp/marketplaces/source/plugins/codex-trace"
        self.cache = self.codex_home / "plugins/cache/codex-trace-community/codex-trace/0.2.2"
        for directory in (self.source, self.cache):
            directory.mkdir(parents=True)
            (directory / "contracts").mkdir()
            (directory / "contracts/tools.json").write_text('{"tools":{"open_codex_trace":{}}}')
            (directory / "requirements.lock").write_text("mcp==1.30.0\n")
            (directory / "launch.py").write_text("# synthetic\n")
            files = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in directory.rglob("*") if p.is_file()}
            (directory / "package-manifest.json").write_text(json.dumps(
                {"name": "codex-trace", "version": installer.VERSION, "files": files}))
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "CODEX_HOME": str(self.codex_home), "UV_CACHE_DIR": str(self.root / "wrong-cache")}
        self.calls = []

    def fake_run(self, command, cwd, env, timeout=300):
        args = [str(x) for x in command[1:]]
        self.calls.append(args)
        self.assertNotIn("UV_CACHE_DIR", env)
        self.assertNotEqual(cwd, ROOT)
        if args[:3] == ["plugin", "marketplace", "add"]:
            return json.dumps({"marketplaceName": installer.MARKET, "installedRoot": str(self.source.parents[1])})
        if args[:2] == ["run", "--no-project"]:
            self.assertIn("import compression.zstd, mcp", args)
            return ""
        if args[:2] == ["plugin", "add"]:
            return json.dumps({"installedPath": str(self.cache)})
        if args[:2] == ["mcp", "get"]:
            return json.dumps({"enabled": True, "transport": {"command": "uv", "cwd": str(self.cache),
                "args": ["run", "--no-project", "--no-config", "--python", "3.14.2", "--with-requirements",
                         str(self.cache / "requirements.lock"), "--", "python", "-I", "-B", str(self.cache / "launch.py")]}})
        self.fail(f"Unexpected command {args}")

    def install(self):
        return installer.install(environment=self.env)

    def test_installer_prepares_before_enabling_and_probes_offline(self):
        with patch.object(installer, "executable", return_value=Path(sys.executable)), \
                patch.object(installer, "run", side_effect=self.fake_run), \
                patch.object(installer, "verify_mcp", return_value={"passed": True}) as probe:
            result = self.install()
        self.assertTrue(result["ready"])
        self.assertEqual([x[0] for x in self.calls], ["plugin", "run", "plugin", "mcp"])
        self.assertEqual(self.calls[0][-3:], ["--ref", "v0.2.2", "--json"])
        self.assertEqual(probe.call_args.args[2]["UV_OFFLINE"], "1")
        self.assertEqual(probe.call_args.args[-1], {"open_codex_trace", "open_codex_trace_panel"})

    def test_dependency_failure_does_not_enable_plugin_or_report_ready(self):
        def fail(command, *args, **kwargs):
            if command[1] == "run":
                raise RuntimeError("network unavailable")
            return self.fake_run(command, *args, **kwargs)
        with patch.object(installer, "executable", return_value=Path(sys.executable)), \
                patch.object(installer, "run", side_effect=fail), \
                patch.object(installer, "verify_mcp") as probe, self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse(any(args[:2] == ["plugin", "add"] for args in self.calls))
        probe.assert_not_called()

    def test_startup_failure_is_not_success_and_can_be_retried(self):
        with patch.object(installer, "executable", return_value=Path(sys.executable)), \
                patch.object(installer, "run", side_effect=self.fake_run), \
                patch.object(installer, "verify_mcp", side_effect=[TimeoutError("not ready"), {"passed": True}]):
            with self.assertRaises(TimeoutError):
                self.install()
            self.assertTrue(self.install()["ready"])

    def test_modified_package_and_foreign_directory_are_rejected(self):
        (self.source / "launch.py").write_text("modified")
        with self.assertRaisesRegex(ValueError, "inventory"):
            installer.verify_package(self.source, self.codex_home, marketplace=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            installer.verify_package(self.root, self.codex_home)

    def test_terminal_only_runtime_overrides_are_removed(self):
        env = installer.runtime_environment({"HOME": "/synthetic", "CODEX_HOME": "/synthetic/.codex",
            "UV_CACHE_DIR": "/different", "UV_PYTHON_INSTALL_DIR": "/other", "UV_OFFLINE": "1",
            "XDG_CACHE_HOME": "/elsewhere", "PYTHONPATH": "/injected", "VIRTUAL_ENV": "/venv"})
        self.assertEqual(env, {"HOME": "/synthetic", "CODEX_HOME": "/synthetic/.codex",
                              "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})

    def test_run_does_not_echo_subprocess_credentials(self):
        result = subprocess.CompletedProcess(["uv"], 1, "SECRET", "PASSWORD")
        with patch.object(subprocess, "run", return_value=result), self.assertRaises(RuntimeError) as caught:
            installer.run(["uv"], self.root, {})
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("PASSWORD", str(caught.exception))

    def probe(self, *, version=None, hang=False):
        program = '''import json,sys,time
version = sys.argv[1]
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if sys.argv[2] == "hang": time.sleep(30)
    if method == "initialize": value={"serverInfo":{"name":"codex-trace","version":version}}
    elif method == "tools/list": value={"tools":[{"name":"open_codex_trace"}]}
    elif method == "resources/list": value={"resources":[{"uri":"ui://codex-trace/trajectory-v1.html"},{"uri":"ui://codex-trace/trajectory-panel-v1.html"}]}
    elif method == "resources/read": value={"contents":[{"text":'<div id="root"></div>'}]}
    else: raise SystemExit("Unexpected business request")
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":value}),flush=True)
'''
        return installer.verify_mcp([sys.executable, "-I", "-u", "-c", program,
            version or installer.VERSION, "hang" if hang else "respond"], self.root, dict(os.environ),
            installer.VERSION, {"open_codex_trace"}, timeout=0.1 if hang else 10)

    def test_real_stdio_probe_checks_identity_tools_and_both_ui_resources(self):
        result = self.probe()
        self.assertTrue(result["passed"])
        self.assertEqual(result["uiResourceCount"], 2)
        self.assertFalse(result["businessToolsCalled"])

    def test_real_stdio_probe_rejects_wrong_version_and_bounds_timeout(self):
        with self.assertRaisesRegex(ValueError, "identity/version"):
            self.probe(version="0.0.0")
        with self.assertRaises(TimeoutError):
            self.probe(hang=True)


if __name__ == "__main__":
    unittest.main()
