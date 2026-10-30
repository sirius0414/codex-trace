#!/usr/bin/env python3
"""Build a self-contained UI/backend plugin, bound to an explicit local runtime.

Builds never register a plugin or touch Codex session data. Dependencies are
verified against requirements.lock; startup never downloads dependencies. Use
--prepare-runtime DIR to create a separate runtime during installation, or
--python PATH to bind an already prepared interpreter.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from platform_runtime import executable, state_root, venv_python
from third_party_licenses import check_snapshot, verify_documents, python_pins

ROOT = Path(__file__).resolve().parents[1]


def locked_versions(lockfile: Path, *, platform: str | None = None) -> dict[str, str]:
    """Return exact pins applicable to the target runtime platform."""
    versions = {}
    for item in python_pins(lockfile):
        if item.get("sys_platform") not in (None, platform or sys.platform):
            continue
        versions[item["name"]] = item["version"]
    if not versions or "mcp" not in versions:
        raise ValueError("requirements.lock must include a pinned mcp SDK")
    return versions


def verify_runtime(python: Path, versions: dict[str, str]) -> dict:
    """在目标解释器中实测 zstd 和锁定依赖，避免误用打包脚本自身的环境。"""
    command = """import importlib.metadata,json,sys
try:
 from compression import zstd
 available = zstd.decompress(zstd.compress(b'codex-trace-runtime')) == b'codex-trace-runtime'
except ImportError:
 available = False
names=json.loads(sys.argv[1])
print(json.dumps({'python':sys.version.split()[0],'versionInfo':list(sys.version_info[:3]),'zstdAvailable':available,'packages':{name:importlib.metadata.version(name) for name in names}}))
"""
    run = subprocess.run([str(python), "-c", command, json.dumps(list(versions))], capture_output=True, text=True, check=True, timeout=30, encoding="utf-8")
    metadata = json.loads(run.stdout)
    if tuple(metadata.get("versionInfo", [])) < (3, 14) or metadata.get("zstdAvailable") is not True:
        raise ValueError("Python >=3.14 with compression.zstd is required for compressed Codex sources")
    mismatches = {name: {"expected": version, "actual": metadata["packages"].get(name)} for name, version in versions.items() if metadata["packages"].get(name) != version}
    if mismatches:
        raise ValueError(f"Runtime does not match requirements.lock: {json.dumps(mismatches)}")
    return metadata


def offline_html(assets: Path) -> str:
    """将单份 JS/CSS 内联为宿主可直接加载的 HTML，并检查已知的远程依赖入口。"""
    javascript = (assets / "app.js").read_text(encoding="utf-8")
    if re.search(r"\bprocess\s*\.\s*env\s*\.\s*NODE_ENV\b", javascript):
        raise ValueError("Browser UI still references process.env.NODE_ENV; configure Vite to define the production value before packaging")
    css_files = sorted(assets.glob("*.css"))
    if len(css_files) != 1:
        raise ValueError(f"Expected one bundled CSS file in {assets}, found {len(css_files)}")
    css = css_files[0].read_text(encoding="utf-8")
    # Source map comments can trigger unnecessary devtools network/file requests.
    javascript = re.sub(r"\n?//# sourceMappingURL=.*", "", javascript)
    css = re.sub(r"/\*# sourceMappingURL=.*?\*/", "", css)
    if re.search(r"@import\s|url\(\s*['\"]?(?:https?:|//)", css, re.I):
        raise ValueError("UI CSS contains a remote dependency")
    # 内联内容可能带 HTML 结束标签；必须转义，避免浏览器提前结束资源节点。
    javascript = re.sub(r"</script", r"<\\/script", javascript, flags=re.I)
    css = re.sub(r"</style", r"<\\/style", css, flags=re.I)
    return '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>执行轨迹</title><style>' + css + '</style></head><body><div id="root"></div><script>' + javascript + '</script></body></html>\n'


def build(output: Path, python: Path, codex: Path, assets: Path) -> dict:
    """验证外部运行环境后在同级临时目录组包，完整写入后才发布到新目录。"""
    backend = ROOT / "backend" / "codex_trace"
    if not (backend / "app.py").is_file():
        raise ValueError("Production backend app.py is missing")
    versions = locked_versions(ROOT / "requirements.lock")
    runtime = verify_runtime(python, versions)
    codex_version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, timeout=15, check=True, encoding="utf-8").stdout.strip()
    html = offline_html(assets)
    build_info_path = assets / "build-info.json"
    frontend_build = json.loads(build_info_path.read_text(encoding="utf-8")) if build_info_path.is_file() else None
    if frontend_build and frontend_build.get("version") != json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]:
        raise ValueError("UI build version does not match plugin version; rebuild the frontend first")
    if output.exists():
        raise ValueError(f"Output already exists; select a new output or remove this generated package first: {output}")
    # 授权快照与锁文件绑定；缺失或陈旧时拒绝发布，不能只复制项目自身的 MIT。
    third_party = check_snapshot(ROOT)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-trace-package-", dir=output.parent) as temporary:
        staged = Path(temporary) / "plugin"
        shutil.copytree(ROOT / "plugin", staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        shutil.copytree(backend, staged / "backend" / "codex_trace", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "contracts", staged / "contracts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staged / "ui").mkdir()
        (staged / "ui" / "trajectory-v1.html").write_text(html, encoding="utf-8")
        shutil.copyfile(ROOT / "requirements.lock", staged / "requirements.lock")
        shutil.copyfile(ROOT / "LICENSE", staged / "LICENSE")
        shutil.copyfile(ROOT / "NOTICE", staged / "NOTICE")
        shutil.copyfile(ROOT / "third_party/manifest.json", staged / "third-party-manifest.json")
        for relative in third_party["files"]:
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "third_party" / relative, destination)
        verify_documents(staged, third_party)
        # 代码随安装缓存移动，cwd 因此相对包根；解释器保留绝对路径以摆脱 GUI 的 PATH。
        launch = {"mcpServers": {"codex-trace": {
            "command": str(python), "args": ["-m", "codex_trace.app"], "cwd": "./backend",
            "env": {"CODEX_TRACE_CODEX_EXECUTABLE": str(codex), "CODEX_TRACE_STATE_ROOT": str(state_root()),
                    "CODEX_TRACE_EXPORT_ROOT": str(Path.home() / "Downloads" / "Codex Trace"),
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        }}}
        (staged / ".mcp.json").write_text(json.dumps(launch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        metadata = {"schemaVersion": 1, "platform": sys.platform, "builtAt": datetime.now(timezone.utc).isoformat(),
                    "runtimePython": str(python), "appServerExecutable": str(codex), "appServerVersion": codex_version,
                    "runtime": runtime, "uiSha256": hashlib.sha256(html.encode()).hexdigest(),
                    "requirementsSha256": hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest(),
                    "thirdPartyManifestSha256": hashlib.sha256((staged / "third-party-manifest.json").read_bytes()).hexdigest(),
                    "runtimeBundled": False, "startupNetworkInstallation": False,
                    "minimumPython": "3.14", "entrypoint": "codex_trace.app",
                    "limitations": ["This local package binds to an existing absolute Python runtime and Codex executable on this machine.", "Keep those paths installed. Rebuild/reinstall after relocating them; the cache copy contains UI/backend code, not the interpreter."]}
        if frontend_build:
            metadata["frontendBuild"] = frontend_build
        (staged / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        (staged / "INSTALL.txt").write_text("Codex Trace 0.1.0 local package\n\nUI, contracts and backend code are included in this cacheable package.\nThe MCP cwd is relative to the installed package; runtime executables are absolute.\nPython >=3.14 with compression.zstd is required. No dependencies are installed at startup.\nKeep runtime.json paths installed; rebuild and reinstall after relocating them.\nOpen Execution Trace from a chat's New Tab menu. The global entry asks you to select a task.\nReload/restart Codex when convenient after an update to refresh plugin resources.\nUninstalling the plugin does not remove ZIPs in ~/Downloads/Codex Trace/.\n", encoding="utf-8")
        # 在同一父目录完成重命名，避免失败的组包结果被当作可安装目录使用。
        staged.rename(output)
    return {"output": str(output), "uiBytes": len(html.encode()), "python": str(python), "codex": str(codex)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "plugin")
    parser.add_argument("--python", default=str(venv_python(ROOT / ".venv")))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--assets", type=Path, default=ROOT / "dist" / "ui")
    parser.add_argument("--prepare-runtime", type=Path, help="Create a new dedicated venv here and install the locked dependencies now")
    args = parser.parse_args()
    try:
        python = executable(args.python)
        if args.prepare_runtime:
            runtime_dir = args.prepare_runtime.absolute()
            if runtime_dir.exists():
                raise ValueError("--prepare-runtime requires a new directory; existing environments are never overwritten")
            if runtime_dir.is_relative_to(args.output.absolute()):
                raise ValueError("Runtime must live outside the cacheable plugin package")
            subprocess.run([str(python), "-m", "venv", str(runtime_dir)], check=True)
            python = venv_python(runtime_dir)
            subprocess.run([str(python), "-m", "pip", "install", "--requirement", str(ROOT / "requirements.lock")], check=True)
        result = build(args.output.absolute(), python, executable(args.codex), args.assets.absolute())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Package build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
