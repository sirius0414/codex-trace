#!/usr/bin/env python3
"""在临时用户目录验收 Marketplace 原始 ZIP，不读取真实聊天或改变真实插件。

核对完整文件清单后解压，使用真实 CLI 安装（可显式跳过），删除安装源并迁移
缓存目录，再复用正式 MCP/导出验收。服务仅处理合成记录，PATH 中无系统 Python。
macOS 可加 --deny-network，以系统沙箱禁止被测进程联网及写入包目录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

from platform_runtime import executable
from verify_install import verify_cache


def extract_verified(archive: Path, destination: Path) -> dict:
    """只接收唯一的包内普通文件，先核对全量 SHA-256，拒绝路径穿越和链接。"""
    if archive.stat().st_size > 100_000_000:
        raise ValueError("invalid-archive-size")
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [entry.filename for entry in entries]
        normalized = [unicodedata.normalize("NFC", name).casefold() for name in names]
        if (len(names) != len(set(normalized)) or len(names) > 5000
                or sum(e.file_size for e in entries) > 512 * 1024 ** 2
                or any(e.file_size > 100 * 1024 ** 2 for e in entries)):
            raise ValueError("invalid-archive-inventory")
        for entry in entries:
            name = entry.filename
            if (not name or "\\" in name or ":" in name or "\0" in name
                    or len(name.split("/")) > 20
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or PurePosixPath(name).is_absolute()
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError("invalid-archive-path-or-file")
        manifest = json.loads(source.read("package-manifest.json"))
        files = manifest["files"]
        if set(names) != set(files) | {"package-manifest.json"}:
            raise ValueError("archive-inventory-mismatch")
        for name, expected in files.items():
            if hashlib.sha256(source.read(name)).hexdigest() != expected:
                raise ValueError("archive-checksum-mismatch")
        # 完整检查之后才写入目标；临时目录由调用方负责生命周期。
        destination.mkdir(parents=True)
        for entry in entries:
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(entry))
            target.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)
    return manifest


def fingerprints(directory: Path) -> dict[str, str]:
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


def set_readonly(directory: Path, readonly: bool) -> None:
    """Unix 按文件执行位保留可启动性；退出时恢复写权限以清理临时产物。"""
    for path in [directory, *directory.rglob("*")]:
        executable_file = path.is_dir() or bool(path.stat().st_mode & 0o111)
        path.chmod((0o555 if executable_file else 0o444) if readonly else (0o755 if executable_file else 0o644))


def smoke(archive: Path, *, codex: Path | None = None, deny_network: bool = False) -> dict:
    """针对同一 ZIP 验证安装与运行；外部门户、签名策略和桌面点击需单独验收。"""
    checks: list[dict] = []
    report = {"archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "platform": sys.platform, "isolatedUserProfile": True, "taskDataRead": False,
              "syntheticTaskDataRead": True, "hostUiVerified": False,
              "networkSandboxEnforced": False, "checks": checks}
    def check(operation, passed):
        checks.append({"operation": operation, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(operation)
    try:
        if deny_network and (sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file()):
            raise ValueError("network-sandbox-unavailable")
        with tempfile.TemporaryDirectory(prefix="codex-marketplace-中文 空格-") as temporary:
            root = Path(temporary).resolve()
            source = root / "market/plugins/codex-trace"
            manifest = extract_verified(archive, source)
            check("archive_inventory_and_checksums", True)
            check("native_target_platform", manifest["platform"] == sys.platform)
            report["target"] = manifest["target"]
            home = root / "cli-home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            installed = source
            if codex:
                env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                       "LOCALAPPDATA": str(home / "AppData/Local"), "CODEX_HOME": str(codex_home)}
                for key in ("PYTHONPATH", "PYTHONHOME", "CODEX_TRACE_CODEX_EXECUTABLE"):
                    env.pop(key, None)
                def cli(*arguments):
                    return subprocess.run([str(codex), *arguments], cwd=root, env=env,
                                          capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
                catalog = root / "market/.agents/plugins/marketplace.json"
                catalog.parent.mkdir(parents=True)
                catalog.write_text(json.dumps({"name": "codex-trace-portable-smoke",
                    "interface": {"displayName": "Codex Trace Portable Smoke"},
                    "plugins": [{"name": "codex-trace", "source": {"source": "local", "path": "./plugins/codex-trace"},
                                 "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                                 "category": "Developer Tools"}]}), encoding="utf-8")
                report["codexVersion"] = cli("--version").stdout.strip()
                cli("plugin", "marketplace", "add", str(root / "market"))
                cli("plugin", "add", "codex-trace@codex-trace-portable-smoke", "--json")
                check("isolated_cli_install", True)
                copies = list((codex_home / "plugins/cache").rglob("package-manifest.json"))
                check("one_installed_package", len(copies) == 1)
                installed = copies[0].parent
                configured = json.loads(cli("mcp", "list", "--json").stdout)
                server = next((s for s in configured if s.get("name") == "codex-trace"), {})
                transport = server.get("transport", {})
                expected_python = installed / json.loads((installed / "runtime.json").read_text())["runtimeExecutable"]
                check("cli_expands_plugin_root", transport.get("command") == str(expected_python)
                      and transport.get("args") == ["-I", "-B", str(installed / "launch.py")])
            # 缓存及原包不再可用，避免迁移验收意外回退到构建目录或下载目录。
            relocated = root / "moved 中文 package"
            shutil.copytree(installed, relocated)
            shutil.rmtree(root / "market")
            if installed != source:
                shutil.rmtree(installed)
            check("source_removed_and_cache_relocated", relocated.is_dir() and not source.exists())
            before = fingerprints(relocated)
            prefix = []
            if deny_network:
                policy = '(version 1)(allow default)(deny network*)(deny file-write* (subpath ' + json.dumps(str(relocated)) + '))'
                prefix = ["/usr/bin/sandbox-exec", "-p", policy]
                report["networkSandboxEnforced"] = True
            try:
                if sys.platform != "win32":
                    set_readonly(relocated, True)
                    check("package_is_readonly", all(not (p.stat().st_mode & 0o222) for p in relocated.rglob("*")))
                    report["packageWriteProtection"] = "sandboxAndUnixModes" if deny_network else "unixModes"
                else:
                    # Windows 的 chmod 只改只读属性，不能证明 DACL 禁止目录写入。
                    report["packageWriteProtection"] = "notEnforcedOnWindows"
                report.update(verify_cache(relocated, root / "verification", checks, portable=True, command_prefix=prefix))
                check("package_files_unchanged", fingerprints(relocated) == before)
            finally:
                set_readonly(relocated, False)
    except Exception as error:
        checks.append({"operation": "marketplace_smoke_completed", "passed": False,
                       "errorType": type(error).__name__})
    report["passed"] = bool(checks) and all(check["passed"] for check in checks)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--skip-cli", action="store_true", help="Only test an extracted/relocated package; no CLI installation claim")
    parser.add_argument("--deny-network", action="store_true", help="macOS: enforce network and package-write denial for MCP processes")
    args = parser.parse_args()
    try:
        result = smoke(args.archive.resolve(), codex=None if args.skip_cli else executable(args.codex), deny_network=args.deny_network)
    except (OSError, ValueError) as error:
        result = {"passed": False, "errorType": type(error).__name__}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
