#!/usr/bin/env python3
"""生成 GitHub 仓库市场的可提交插件目录，或检查目录与当前源码是否一致。

前端在发布时编译，Python 和锁定依赖由用户安装的 uv 在运行时准备。输出
仅来自明确白名单；生成目录原子替换以删除陈旧文件，任何源/目标链接均拒绝。
构建不注册市场、不修改用户 Codex 配置，也不会启动用户任务。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from build_release import (PUBLIC_DOCUMENTS, PUBLIC_DOCUMENT_ASSETS, ROOT, audit_payload,
                           checked_bytes, collect_payload, digest, json_bytes)
from platform_runtime import npm_command

PYTHON_VERSION = "3.14.2"
MARKETPLACE_NAME = "codex-trace-community"
PACKAGE_MANIFEST = "package-manifest.json"
CATALOG_PATH = ".agents/plugins/marketplace.json"
DISTRIBUTION = "github-repository-marketplace"
USER_ENV_VARS = ["CODEX_HOME", "CODEX_TRACE_CODEX_EXECUTABLE", "CODEX_TRACE_STATE_ROOT", "CODEX_TRACE_EXPORT_ROOT"]


def catalog() -> dict:
    """仓库本身即市场；相对插件目录随 Git checkout 一起分发。"""
    return {"name": MARKETPLACE_NAME, "interface": {"displayName": "Codex Trace Community"},
            "plugins": [{"name": "codex-trace", "source": {"source": "local", "path": "./plugins/codex-trace"},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": "Developer Tools"}]}


def check_regular_tree(directory: Path) -> dict[str, bytes]:
    """不跟随符号链接或重解析点，逐字节比较时连多余的生成文件也不能忽略。"""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Generated plugin must be a real directory")
    files = {}
    for path in [directory, *sorted(directory.rglob("*"))]:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Generated plugin contains a symlink or reparse point: {path.name}")
        if path.is_file():
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Generated plugin contains a special file")
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError("Generated plugin contains a special file")
    return files


def check_output_path(output: Path) -> None:
    """检查尚不存在目录的祖先，避免通过目录链接把替换写入另一位置。"""
    for path in (output, *output.parents):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Output path must not contain symlinks or reparse points")


def reject_source_links(directory: Path) -> None:
    """连不会被 rglob 跟随的目录链接也拒绝，不能静默漏掉应分发的源文件。"""
    if not directory.exists() and not directory.is_symlink():
        return
    for path in [directory, *directory.rglob("*")]:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Marketplace source contains a symlink or reparse point")


def community_payload(root: Path, assets: Path) -> tuple[str, dict[str, bytes]]:
    """沿用安装包的代码、授权和路径审计，另生成不绑定开发机器的 uv 启动配置。"""
    for directory in (root / "plugin", root / "backend/codex_trace", root / "contracts", root / "third_party", assets):
        reject_source_links(directory)
    version, installer = collect_payload(root, assets)
    files = {name.removeprefix("plugin/"): data for name, data in installer.items() if name.startswith("plugin/")}
    for name in (*PUBLIC_DOCUMENTS, *PUBLIC_DOCUMENT_ASSETS):
        files[name] = checked_bytes(root / name, root)
    files["plugin.json"] = checked_bytes(root / "plugin/plugin.json", root)
    files["launch.py"] = checked_bytes(root / "plugin/launch.py", root)
    portable = json.loads(files["plugin.json"])
    if portable.get("name") != "codex-trace" or portable.get("version") != version:
        raise ValueError("Portable plugin manifest must match the release identity and version")
    # legacy 0.153 不展开参数中的 PLUGIN_ROOT；现代配置显式定位缓存资源，
    # 使资源定位不受宿主或运行时对子进程工作目录的处理影响。
    launch = {"command": "uv", "args": ["run", "--no-project", "--no-config", "--python", PYTHON_VERSION,
              "--with-requirements", "./requirements.lock", "--", "python", "-I", "-B",
              "./launch.py"], "cwd": ".",
              "env": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}}
    modern = {"type": "stdio", **launch, "cwd": "${PLUGIN_ROOT}",
              "args": [argument.replace("./", "${PLUGIN_ROOT}/", 1)
                       if argument.startswith("./") else argument for argument in launch["args"]]}
    files["mcp.json"] = json_bytes({"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                                   "mcpServers": {"codex-trace": modern}})
    # Codex 0.153 只从 legacy overlay 读取 env_vars；现代 schema 不接受此字段。
    # 仅透传已有的本机配置入口，保留自定义 Codex 用户目录及存储位置的语义。
    files[".mcp.json"] = json_bytes({"mcpServers": {"codex-trace": {**launch, "env_vars": USER_ENV_VARS}}})
    legacy = json.loads(files[".codex-plugin/plugin.json"])
    legacy["mcpServers"] = "./.mcp.json"
    files[".codex-plugin/plugin.json"] = json_bytes(legacy)
    metadata = {"schemaVersion": 1, "distribution": DISTRIBUTION, "runtimeManagement": "uv",
                "pythonVersion": PYTHON_VERSION, "minimumPython": "3.14", "platforms": ["darwin", "win32"],
                "runtimeBundled": False, "startupDependencyPreparation": True, "startupNetworkInstallation": True,
                "dependencyPreparation": "when-not-cached", "entrypoint": "codex_trace.marketplace",
                "launcherCommand": "uv", "appServerExecutable": "discovered-at-startup",
                "uiSha256": digest(files["ui/trajectory-v1.html"]),
                "requirementsSha256": digest(files["requirements.lock"]),
                "thirdPartyManifestSha256": digest(files["third-party-manifest.json"])}
    files["runtime.json"] = json_bytes(metadata)
    audit_payload(files, root)
    manifest = {"schemaVersion": 1, "name": "codex-trace", "version": version,
                "distribution": DISTRIBUTION, "runtimeManagement": "uv", "runtimeBundled": False,
                "startupDependencyPreparation": True,
                "files": {name: digest(data) for name, data in sorted(files.items())}}
    files[PACKAGE_MANIFEST] = json_bytes(manifest)
    return version, files


def publish_directory(output: Path, files: dict[str, bytes]) -> None:
    """只替换本构建器管理的目录；失败时恢复旧目录，成功后不遗留旧版本文件。"""
    check_output_path(output)
    if output.exists():
        previous = check_regular_tree(output)
        try:
            owner = json.loads(previous[PACKAGE_MANIFEST])
        except (KeyError, ValueError) as error:
            raise ValueError("Refusing to replace an unmanaged directory") from error
        if owner.get("name") != "codex-trace" or owner.get("distribution") != DISTRIBUTION:
            raise ValueError("Refusing to replace a different package")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".community-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / "new"
        stage.mkdir()
        for name, data in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        backup = Path(temporary) / "previous"
        had_previous = output.exists()
        if had_previous:
            output.rename(backup)
        try:
            stage.rename(output)
        except BaseException:
            if had_previous:
                backup.rename(output)
            raise


def build_community(output: Path | None = None, *, root: Path = ROOT,
                    assets: Path | None = None, check: bool = False) -> dict:
    root = root.absolute()
    output = (output or root / "plugins/codex-trace").absolute()
    check_output_path(output)
    # 目录清单也是发布输入，必须与约定市场名称和源路径完全一致。
    if json.loads(checked_bytes(root / CATALOG_PATH, root)) != catalog():
        raise ValueError("Repository marketplace catalog differs from the community package")
    if assets is None:
        for relative in ("frontend/src", "plugin", "backend/codex_trace", "contracts", "third_party"):
            reject_source_links(root / relative)
        subprocess.run(npm_command("run", "build"), cwd=root, check=True,
                       env=dict(os.environ, SOURCE_DATE_EPOCH="0"))
        assets = root / "dist/ui"
    version, files = community_payload(root, assets.absolute())
    if check:
        actual = check_regular_tree(output)
        missing = sorted(files.keys() - actual.keys())
        extra = sorted(actual.keys() - files.keys())
        changed = sorted(name for name in files.keys() & actual.keys() if files[name] != actual[name])
        if missing or extra or changed:
            raise ValueError("Committed community package is stale: " + json.dumps(
                {"missing": missing, "extra": extra, "changed": changed}, ensure_ascii=False))
    else:
        publish_directory(output, files)
    return {"passed": True, "checked": check, "output": str(output), "version": version,
            "fileCount": len(files), "packageManifestSha256": digest(files[PACKAGE_MANIFEST]),
            "marketplace": MARKETPLACE_NAME, "runtimeManagement": "uv", "runtimeBundled": False,
            "startupDependencyPreparation": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assets", type=Path, help="Use already built frontend assets (normally omit)")
    parser.add_argument("--check", action="store_true", help="Fail if committed files differ; do not replace them")
    args = parser.parse_args()
    try:
        print(json.dumps(build_community(args.output, assets=args.assets, check=args.check), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Community marketplace build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
