"""公共分发包的无安装启动边界；任务读取仍由现有 app/service 实现。

包内 Python 和依赖随插件移动，Codex 可执行文件与用户目录必须在启动时解析。
发现过程只检查明确配置、PATH 和已知桌面应用布局，不执行 shell、下载依赖、
写配置或注册市场。错误仅写固定 stderr 文案，stdout 始终留给 MCP 协议。
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
from pathlib import Path
import platform
import sys


class LauncherError(RuntimeError):
    """只允许启动器自身的固定文案，不包含异常值、用户内容或本机路径。"""


def validate_runtime() -> None:
    """实测包内解释器及 zstd，发现不完整运行包时在 MCP 初始化前失败。"""
    if sys.version_info < (3, 14):
        raise LauncherError("插件 Python 版本不兼容；请重新安装适用于本机的完整插件包。")
    try:
        from compression import zstd
        payload = b"codex-trace-runtime"
        if zstd.decompress(zstd.compress(payload)) != payload:
            raise ValueError("invalid-zstd-result")
    except Exception:
        raise LauncherError("插件 Python 缺少可用的 zstd；请重新安装完整插件包。") from None


def _native_codex(shim: Path, machine: str) -> Path:
    """只识别 npm 官方布局，不解析或运行 .cmd/.bat 中的任意命令。"""
    if shim.stem.lower() != "codex":
        raise LauncherError("Codex 配置不是受支持的原生程序；请指定 codex.exe 的绝对路径。")
    architectures = {"arm64": ("arm64", "aarch64"), "aarch64": ("arm64", "aarch64"),
                     "amd64": ("x64", "x86_64"), "x86_64": ("x64", "x86_64"), "x64": ("x64", "x86_64")}
    if machine.lower() not in architectures:
        raise LauncherError("无法判断 Codex 的原生架构；请指定 codex.exe 的绝对路径。")
    architecture, target = architectures[machine.lower()]
    for modules in (shim.parent / "node_modules", shim.parent.parent):
        for package in (modules / "@openai" / f"codex-win32-{architecture}", modules / "@openai/codex"):
            for directory in ("bin", "codex"):
                candidate = package / "vendor" / f"{target}-pc-windows-msvc" / directory / "codex.exe"
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate.absolute()
    raise LauncherError("未找到 npm 安装中的原生 Codex；请指定 codex.exe 的绝对路径。")


def _candidate(path: Path, platform_name: str, machine: str) -> Path:
    """只接受存在且可执行的文件；Windows 批处理必须归一到已知原生程序。"""
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LauncherError("Codex 程序不可用；请检查 CODEX_TRACE_CODEX_EXECUTABLE 的绝对路径。")
    if platform_name == "win32":
        if path.suffix.lower() in {".cmd", ".bat"}:
            return _native_codex(path, machine)
        if path.suffix.lower() != ".exe":
            raise LauncherError("Codex 配置不是受支持的原生程序；请指定 codex.exe 的绝对路径。")
    return path.absolute()


def _application_candidates(home: Path) -> list[Path]:
    """仅查找已知系统及用户 Applications 目录，不扫描其他用户或执行应用。"""
    relative = ("Codex.app/Contents/Resources/codex",
                "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    return [root / item for root in (Path("/Applications"), home / "Applications") for item in relative]


def discover_codex(environ: Mapping[str, str] | None = None, *, platform_name: str | None = None,
                   machine: str | None = None, home: Path | None = None) -> Path:
    """显式配置优先且失败即停；自动发现跳过无效条目，避免 GUI PATH 差异。

    PATH 只接受绝对目录，忽略空项和相对目录，防止插件启动时的工作目录偷偷
    提供同名程序。Windows 只查原生 exe 或可归一到官方 npm 原生包的包装器。
    返回词法绝对路径，不把用户安装的正常符号链接作为错误。
    """
    environ = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    machine = platform.machine() if machine is None else machine
    if "CODEX_TRACE_CODEX_EXECUTABLE" in environ:
        configured = environ["CODEX_TRACE_CODEX_EXECUTABLE"]
        if not configured or "\x00" in configured:
            raise LauncherError("Codex 显式配置无效；请填写 CODEX_TRACE_CODEX_EXECUTABLE 的绝对路径。")
        # 不使用 expandvars/expanduser：明确配置必须本身完整，不依赖 shell 展开。
        try:
            return _candidate(Path(configured), platform_name, machine)
        except (OSError, ValueError):
            raise LauncherError("Codex 显式配置无效；请填写 CODEX_TRACE_CODEX_EXECUTABLE 的绝对路径。") from None
    names = ("codex.exe", "codex.cmd", "codex.bat") if platform_name == "win32" else ("codex",)
    separator = ";" if platform_name == "win32" else ":"
    candidates = []
    for directory in environ.get("PATH", "").split(separator):
        # Windows PATH 常对含空格目录加引号；只移除配对的最外层引号。
        if platform_name == "win32" and len(directory) >= 2 and directory[0] == directory[-1] == '"':
            directory = directory[1:-1]
        if directory and "\x00" not in directory and Path(directory).is_absolute():
            candidates.extend(Path(directory) / name for name in names)
    if platform_name == "darwin":
        candidates.extend(_application_candidates(Path.home() if home is None else home))
    for path in candidates:
        try:
            return _candidate(path, platform_name, machine)
        except (LauncherError, OSError, ValueError):
            continue
    raise LauncherError("未找到可用的 Codex CLI；请将原生程序加入 PATH，或设置 CODEX_TRACE_CODEX_EXECUTABLE 为其绝对路径。")


def main() -> int:
    """运行时准备仅改变当前进程环境，随后原样进入正式只读 MCP 服务。"""
    try:
        validate_runtime()
        codex = discover_codex()
        os.environ["CODEX_TRACE_CODEX_EXECUTABLE"] = str(codex)
        try:
            from .app import main as serve
        except Exception:
            raise LauncherError("插件服务或依赖不完整；请重新安装完整插件包。") from None
        asyncio.run(serve())
        return 0
    except LauncherError as error:
        print(f"Codex Trace: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("Codex Trace: 本地 MCP 服务未能启动或已停止；请重新打开插件后重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
