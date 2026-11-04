"""Marketplace 启动器使用合成目录验证可移动性；不打开真实任务或注册插件。"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_trace import marketplace


ROOT = Path(__file__).resolve().parents[1]


class MarketplaceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex trace 市场 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.name = "codex.exe" if sys.platform == "win32" else "codex"

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic discovery only; never execute\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def discover(self, env, **kwargs):
        return marketplace.discover_codex(env, platform_name=kwargs.pop("platform_name", "win32" if sys.platform == "win32" else "linux"), **kwargs)

    def test_explicit_absolute_program_wins_over_path(self):
        explicit = self.executable(self.root / "明确 程序" / self.name)
        other = self.executable(self.root / "path" / self.name)
        self.assertEqual(self.discover({"CODEX_TRACE_CODEX_EXECUTABLE": str(explicit), "PATH": str(other.parent)}), explicit)

    def test_invalid_explicit_program_never_falls_back(self):
        fallback = self.executable(self.root / "path" / self.name)
        for value in ("", "codex", "~/codex", str(self.root / "missing"), "bad\x00value"):
            with self.subTest(value=value), self.assertRaises(marketplace.LauncherError):
                self.discover({"CODEX_TRACE_CODEX_EXECUTABLE": value, "PATH": str(fallback.parent)})

    def test_path_preserves_order_and_skips_directories_and_missing_files(self):
        invalid = self.root / "invalid"
        (invalid / self.name).mkdir(parents=True)
        first = self.executable(self.root / "first" / self.name)
        second = self.executable(self.root / "second" / self.name)
        self.assertEqual(self.discover({"PATH": os.pathsep.join(map(str, (invalid, first.parent, second.parent)))}), first)

    @unittest.skipIf(os.name == "nt", "POSIX 可执行位语义")
    def test_non_executable_file_is_rejected(self):
        path = self.executable(self.root / "codex")
        path.chmod(0o600)
        with self.assertRaises(marketplace.LauncherError):
            self.discover({"CODEX_TRACE_CODEX_EXECUTABLE": str(path)})

    def test_empty_and_relative_path_entries_are_not_discovery_roots(self):
        cwd = Path.cwd()
        self.executable(self.root / self.name)
        self.executable(self.root / "relative" / self.name)
        try:
            os.chdir(self.root)
            with self.assertRaises(marketplace.LauncherError):
                self.discover({"PATH": os.pathsep.join(("", "relative", "."))})
        finally:
            os.chdir(cwd)

    def test_macos_known_bundle_fallback_and_path_precedence(self):
        # 模拟 macOS 时也使用 POSIX 路径；Windows 临时目录的盘符冒号不是 macOS PATH 内容。
        home = PurePosixPath("/Users/synthetic")
        bundle = home / "Applications/Codex.app/Contents/Resources/codex"
        path = home / "path/codex"

        def existing_candidate(candidate, platform_name, machine):
            self.assertEqual(platform_name, "darwin")
            if candidate not in (bundle, path):
                raise marketplace.LauncherError("synthetic candidate missing")
            return candidate

        with patch.object(marketplace, "Path", PurePosixPath), \
             patch.object(marketplace, "_candidate", side_effect=existing_candidate):
            self.assertEqual(self.discover({"PATH": ""}, platform_name="darwin", home=home), bundle)
            self.assertEqual(self.discover({"PATH": str(path.parent)}, platform_name="darwin", home=home), path)
            candidates = marketplace._application_candidates(home)
        self.assertIn(bundle, candidates)
        self.assertIn(home / "Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", candidates)

    def test_windows_native_path_and_quoted_directory(self):
        native = self.executable(self.root / "程序 目录/codex.exe")
        self.assertEqual(self.discover({"PATH": f'"{native.parent}"'}, platform_name="win32"), native)

    def test_windows_global_npm_shim_resolves_native_without_execution(self):
        shim = self.executable(self.root / "npm/codex.cmd")
        native = self.executable(shim.parent / "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
        self.assertEqual(self.discover({"CODEX_TRACE_CODEX_EXECUTABLE": str(shim)}, platform_name="win32", machine="AMD64"), native)
        self.assertEqual(shim.read_text(encoding="utf-8"), "synthetic discovery only; never execute\n")

    def test_windows_project_legacy_arm64_shim_resolves_native(self):
        shim = self.executable(self.root / "node_modules/.bin/codex.bat")
        native = self.executable(self.root / "node_modules/@openai/codex/vendor/aarch64-pc-windows-msvc/codex/codex.exe")
        self.assertEqual(self.discover({"PATH": str(shim.parent)}, platform_name="win32", machine="arm64"), native)

    def test_windows_unknown_shim_and_non_native_program_fail(self):
        for name in ("custom.cmd", "codex.cmd", "codex.py"):
            path = self.executable(self.root / name)
            with self.subTest(name=name), self.assertRaises(marketplace.LauncherError):
                self.discover({"CODEX_TRACE_CODEX_EXECUTABLE": str(path)}, platform_name="win32", machine="AMD64")

    def test_windows_production_main_rejects_python_peer_without_fallback(self):
        peer = self.executable(self.root / "synthetic-history.py")
        fallback = self.executable(self.root / "native/codex.exe")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(marketplace.sys, "platform", "win32"), patch.object(marketplace, "validate_runtime"), patch.dict(os.environ, {"CODEX_TRACE_CODEX_EXECUTABLE": str(peer), "PATH": str(fallback.parent)}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(marketplace.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("codex.exe", stderr.getvalue())
        self.assertNotIn("synthetic-history.py", stderr.getvalue())

    def test_runtime_check_rejects_old_python_and_failed_zstd(self):
        with patch.object(marketplace.sys, "version_info", (3, 13)), self.assertRaises(marketplace.LauncherError):
            marketplace.validate_runtime()
        broken = SimpleNamespace(compress=lambda _: b"bad", decompress=lambda _: b"wrong")
        with patch.object(marketplace.sys, "version_info", (3, 14)), patch.dict(sys.modules, {"compression": SimpleNamespace(zstd=broken)}):
            with self.assertRaises(marketplace.LauncherError):
                marketplace.validate_runtime()

    def test_main_keeps_mcp_stdout_clean_on_configuration_failure(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"CODEX_TRACE_CODEX_EXECUTABLE": str(self.root / "PRIVATE_PATH")}, clear=True), patch.object(marketplace, "validate_runtime"), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(marketplace.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("CODEX_TRACE_CODEX_EXECUTABLE", stderr.getvalue())
        self.assertNotIn("PRIVATE_PATH", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_only_sets_codex_and_enters_existing_service(self):
        native = self.executable(self.root / ("codex.exe" if sys.platform == "win32" else "codex"))
        observed = []

        async def serve():
            observed.append(dict(os.environ))

        environment = {"CODEX_HOME": str(self.root / "用户 来源"), "PATH": str(native.parent)}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(marketplace, "validate_runtime"), patch.dict(sys.modules, {"codex_trace.app": SimpleNamespace(main=serve)}), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(marketplace.main(), 0)
        self.assertEqual(observed, [{**environment, "CODEX_TRACE_CODEX_EXECUTABLE": str(native)}])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_unexpected_service_error_does_not_print_details(self):
        async def serve():
            raise ValueError("PRIVATE_TASK_BODY")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(marketplace, "validate_runtime"), patch.object(marketplace, "discover_codex", return_value=self.root / "codex"), patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"codex_trace.app": SimpleNamespace(main=serve)}), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(marketplace.main(), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("PRIVATE_TASK_BODY", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    @unittest.skipIf(sys.version_info < (3, 14), "移动启动验证需要 Python 3.14+ 与 zstd")
    def test_relocated_launch_uses_only_bundle_paths_under_isolated_python(self):
        package = self.root / "原始 包"
        backend = package / "backend/codex_trace"
        backend.mkdir(parents=True)
        (backend / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(ROOT / "backend/codex_trace/marketplace.py", backend / "marketplace.py")
        shutil.copyfile(ROOT / "plugin/launch.py", package / "launch.py")
        (package / "runtime-deps").mkdir()
        (package / "runtime-deps/bundled_probe.py").write_text("VALUE = 'bundled'\n", encoding="utf-8")
        # 模拟 pywin32.pth 的附加搜索路径及初始化代码，避免只测纯 Python 根模块。
        (package / "runtime-deps/native-support").mkdir()
        (package / "runtime-deps/native-support/bundled_native.py").write_text("VALUE = 'native-path'\n", encoding="utf-8")
        (package / "runtime-deps/bundled_bootstrap.py").write_text("import sys\nsys._codex_trace_pth_probe = 'initialized'\n", encoding="utf-8")
        (package / "runtime-deps/native-support.pth").write_text("native-support\nimport bundled_bootstrap\n", encoding="utf-8")
        (backend / "app.py").write_text("import json, os, sys, bundled_probe, bundled_native\nasync def main():\n print(json.dumps({'dependency': bundled_probe.VALUE, 'native': bundled_native.VALUE, 'bootstrap': sys._codex_trace_pth_probe, 'codex': os.environ['CODEX_TRACE_CODEX_EXECUTABLE'], 'home': os.environ['CODEX_HOME']}))\n", encoding="utf-8")
        relocated = self.root / "已移动 插件"
        package.rename(relocated)
        # 当前目录与 PYTHONPATH 的同名模块不能替代随包模块；进程不读取真实来源。
        decoy = self.root / "无关 cwd"
        decoy.mkdir()
        (decoy / "bundled_probe.py").write_text("raise RuntimeError('untrusted cwd imported')\n", encoding="utf-8")
        native = self.executable(self.root / ("codex.exe" if sys.platform == "win32" else "codex"))
        environment = {**os.environ, "PYTHONPATH": str(decoy), "CODEX_TRACE_CODEX_EXECUTABLE": str(native), "CODEX_HOME": str(self.root / "synthetic-home")}
        result = subprocess.run([sys.executable, "-I", "-B", str(relocated / "launch.py")], cwd=decoy, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"dependency": "bundled", "native": "native-path", "bootstrap": "initialized", "codex": str(native), "home": environment["CODEX_HOME"]})
        self.assertEqual(result.stderr, "")
        self.assertFalse(list(relocated.rglob("__pycache__")))
        self.assertFalse((self.root / "synthetic-home").exists())


if __name__ == "__main__":
    unittest.main()
