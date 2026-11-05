"""开发与安装脚本的平台边界：发现本机运行环境并生成无 shell 的 argv。

Windows 的 npm/Codex 经常由 .cmd 包装；不能把含用户目录的路径交给 cmd.exe。
这里只识别已知的 npm 安装布局，直接选择 Node CLI 或 Codex 原生二进制；未知
包装器要求显式指定实际程序，不执行或解析包装器中的任意命令。
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import sys

IS_WINDOWS = sys.platform == "win32"


def venv_python(directory: Path, *, windows: bool | None = None) -> Path:
    windows = IS_WINDOWS if windows is None else windows
    return directory / ("Scripts/python.exe" if windows else "bin/python")


def state_root(home: Path | None = None) -> Path:
    home = home or Path.home()
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
        if not base.is_absolute():
            raise ValueError("absolute-localappdata-required")
        return base / "codex-trace/index"
    return home / ".local/share/codex-trace/index"


def codex_native(shim: Path) -> Path:
    """把标准 npm Codex .cmd 定位到对应架构的 exe；不执行批处理。"""
    if shim.stem.lower() != "codex":
        raise ValueError(f"Batch launchers are unsupported; specify a native executable: {shim}")
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    triple = "aarch64-pc-windows-msvc" if architecture == "arm64" else "x86_64-pc-windows-msvc"
    # npm 全局安装以及项目 node_modules/.bin 的已知布局。兼容早期 vendor 包和
    # 当前 optional platform package；非标准安装可通过 --codex 指定真实 exe。
    module_roots = [shim.parent / "node_modules", shim.parent.parent]
    for modules in module_roots:
        main_package = modules / "@openai/codex"
        # npm 全局安装可把可选平台包保留在主包自己的 node_modules，仍只识别固定官方名称。
        for package in (modules / "@openai" / f"codex-win32-{architecture}",
                        main_package / "node_modules/@openai" / f"codex-win32-{architecture}", main_package):
            for relative in (f"vendor/{triple}/bin/codex.exe", f"vendor/{triple}/codex/codex.exe"):
                candidate = package / relative
                if candidate.is_file():
                    return candidate.absolute()
    raise ValueError(f"Cannot locate native codex.exe beside {shim}; specify --codex with its absolute path")


def executable(value: str | Path) -> Path:
    """保留 venv 的词法路径；Windows 批处理只接受可识别的 Codex 安装。"""
    value = Path(value).expanduser() if any(mark in str(value) for mark in ("/", "\\", "~")) else value
    candidate = shutil.which(str(value)) if not Path(value).is_absolute() else str(value)
    if not candidate:
        raise ValueError(f"Executable not found: {value}")
    path = Path(candidate).absolute()
    if IS_WINDOWS and path.suffix.lower() in {".cmd", ".bat"}:
        return codex_native(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Not executable: {path}")
    return path


def npm_command(*arguments: str) -> list[str]:
    """Windows 通过 node.exe 运行 npm-cli.js，路径和参数始终作为独立 argv。"""
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm was not found on PATH")
    path = Path(npm).absolute()
    if not IS_WINDOWS or path.suffix.lower() not in {".cmd", ".bat"}:
        return [str(path), *arguments]
    candidates = [path.parent / "node_modules/npm/bin/npm-cli.js"]
    # npm 调用的子脚本继承其实际 CLI 路径；避免信任任意脚本名的环境覆盖。
    configured = os.environ.get("npm_execpath")
    if configured and Path(configured).name.lower() == "npm-cli.js":
        candidates.insert(0, Path(configured))
    cli = next((candidate for candidate in candidates if candidate.is_file()), None)
    if cli is None:
        raise ValueError("Cannot locate npm-cli.js; use a standard Node.js/npm installation")
    node = path.parent / "node.exe"
    return [str(node if node.is_file() else executable("node")), str(cli), *arguments]
