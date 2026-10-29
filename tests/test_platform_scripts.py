"""安装/开发平台选择的本机与模拟检查；Windows API 检查仅在 Windows 执行。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import platform_dev
    import platform_runtime as runtime
    import manage_dev_browser as management
finally:
    sys.path.pop(0)


class PlatformScriptSelectionTests(unittest.TestCase):
    """可移植选择逻辑；windows=True 只模拟分支，不证明 Windows 实机可用。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-display-中文 路径-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_venv_paths_and_windows_state_path(self):
        self.assertEqual(runtime.venv_python(self.root, windows=False), self.root / "bin/python")
        self.assertEqual(runtime.venv_python(self.root, windows=True), self.root / "Scripts/python.exe")
        with mock.patch.object(runtime, "IS_WINDOWS", True), mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(runtime.state_root(), self.root / "codex-display/index")

    def test_relative_localappdata_refuses_before_creating_a_runtime_or_manifest(self):
        for value in ("", "relative/profile", "../other-profile"):
            with self.subTest(value=value), mock.patch.object(runtime, "IS_WINDOWS", True), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": value}), \
                    self.assertRaisesRegex(ValueError, "absolute-localappdata-required"):
                runtime.state_root()

    def test_known_codex_batch_shim_resolves_native_architecture_without_executing_shell(self):
        shim = self.root / "codex.cmd"
        shim.write_text("DO NOT EXECUTE THIS BATCH FILE", encoding="utf-8")
        for machine, package, triple in (("AMD64", "codex-win32-x64", "x86_64-pc-windows-msvc"),
                                         ("ARM64", "codex-win32-arm64", "aarch64-pc-windows-msvc")):
            native = self.root / f"node_modules/@openai/{package}/vendor/{triple}/bin/codex.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"synthetic executable")
            with mock.patch.object(runtime, "IS_WINDOWS", True), mock.patch.object(runtime.platform, "machine", return_value=machine):
                self.assertEqual(runtime.executable(shim), native)

    def test_unknown_batch_wrapper_refuses_instead_of_using_cmd(self):
        shim = self.root / "custom.cmd"
        shim.write_text("echo synthetic", encoding="utf-8")
        with mock.patch.object(runtime, "IS_WINDOWS", True), self.assertRaisesRegex(ValueError, "Batch launchers"):
            runtime.executable(shim)

    def test_npm_batch_uses_node_cli_argv_with_literal_special_characters(self):
        shim = self.root / "npm.cmd"
        shim.touch()
        node = self.root / "node.exe"
        node.touch()
        cli = self.root / "node_modules/npm/bin/npm-cli.js"
        cli.parent.mkdir(parents=True)
        cli.touch()
        arguments = ("run", "build", "--", "中文 空格 & %VALUE% !")
        with mock.patch.object(runtime, "IS_WINDOWS", True), mock.patch.object(runtime.shutil, "which", return_value=str(shim)), \
                mock.patch.dict(os.environ, {"npm_execpath": ""}):
            self.assertEqual(runtime.npm_command(*arguments), [str(node), str(cli), *arguments])

    def test_missing_npm_cli_refuses_without_batch_execution(self):
        shim = self.root / "npm.cmd"
        shim.touch()
        with mock.patch.object(runtime, "IS_WINDOWS", True), mock.patch.object(runtime.shutil, "which", return_value=str(shim)), \
                mock.patch.dict(os.environ, {"npm_execpath": ""}), self.assertRaisesRegex(ValueError, "npm-cli.js"):
            runtime.npm_command("run", "build")

    def test_node_python_dispatcher_runs_existing_interpreter_and_passes_arguments(self):
        result = subprocess.run(["node", str(ROOT / "scripts/run_python.mjs"), "package_plugin.py", "--help"],
            cwd=self.root, env={**os.environ, "CODEX_DISPLAY_PYTHON": sys.executable},
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        self.assertIn("--prepare-runtime", result.stdout)

    def test_windows_manager_uses_argv_identity_and_creation_time(self):
        manager = management.ServiceManager(self.root)
        record = {"pid": 4321, "started": "creation-time", "instanceId": "exact-instance", "python": sys.executable}
        argv = [sys.executable, str(management.SERVER), "--instance-id", record["instanceId"]]
        info = {"started": record["started"], "argv": argv}
        with mock.patch.object(management, "IS_WINDOWS", True):
            for changed, expected in ((info, True), ({**info, "started": "reused PID"}, False),
                    ({**info, "argv": [*argv[:-1], "exact-instance-other"]}, False),
                    ({**info, "argv": [sys.executable, str(management.SERVER) + ".other", *argv[2:]]}, False)):
                with self.subTest(changed=changed), mock.patch.object(management, "process_info", return_value=changed):
                    self.assertEqual(manager.owns(record), expected)

    def test_cross_process_management_lock_conflict(self):
        code = """import pathlib,sys
sys.path.insert(0,sys.argv[1])
from platform_dev import exclusive_lock
with pathlib.Path(sys.argv[2]).open('a+b') as lock:
 try:
  with exclusive_lock(lock): print('unexpected-unlocked')
 except BlockingIOError: print('busy')
"""
        lock_path = self.root / "中文 manager.lock"
        with lock_path.open("a+b") as lock, platform_dev.exclusive_lock(lock):
            result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"), str(lock_path)],
                capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), "busy")


@unittest.skipUnless(sys.platform == "win32", "requires a native Windows host; never counted as simulated success")
class WindowsNativeScriptTests(unittest.TestCase):
    def test_command_line_roundtrip_preserves_chinese_spaces_and_shell_characters(self):
        argv = [sys.executable, r"C:\中文 空格\script.py", "a&b", "%PATH%", "quote\"value", "ends\\"]
        self.assertEqual(platform_dev.windows_argv(subprocess.list2cmdline(argv)), argv)

    def test_current_process_identity_uses_creation_time_and_full_command_line(self):
        info = platform_dev.windows_process_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertTrue(info["started"])
        self.assertEqual(Path(info["argv"][0]).resolve(), Path(sys.executable).resolve())

    def test_job_object_can_protect_disposable_child_process(self):
        # Job 的 KILL_ON_CLOSE 包含宿主本身，必须放在独立子进程中验证。
        code = "import sys;sys.path.insert(0,sys.argv[1]);from platform_dev import protect_windows_children;job=protect_windows_children();print(bool(job),flush=True)"
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts")],
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
