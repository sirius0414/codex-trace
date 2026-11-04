#!/usr/bin/env python3
"""生成平台与架构独立的 Marketplace 候选包，不注册、提交或发布插件。

运行时必须先通过 prepare_marketplace_runtime.py 准备。依赖只从显式的
wheelhouse 离线安装；启动阶段不下载。每次构建移走整个包后验证依赖和
MCP 初始化，ZIP 根即插件根。通过这些检查不等于公共目录审核通过。
"""
from __future__ import annotations

import argparse
import csv
import io
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

from build_release import RELEASE_DOCUMENTS, ROOT, audit_payload, checked_bytes, collect_payload, digest, json_bytes
from package_plugin import locked_versions
from platform_runtime import npm_command
from prepare_marketplace_runtime import (PROVENANCE, clean_environment, probe_runtime,
                                        runtime_command, runtime_files, validated_runtime)

PACKAGE_MANIFEST = "package-manifest.json"
MAX_ARCHIVE_BYTES = 100_000_000
MAX_ENTRY_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_PATH_SEGMENTS = 20


def project_payload(root: Path, assets: Path, runtime: dict) -> tuple[str, dict[str, bytes]]:
    """复用发布白名单和授权检查；只取插件内容，外部安装器不进入提交包。"""
    version, installer = collect_payload(root, assets)
    files = {name.removeprefix("plugin/"): data for name, data in installer.items() if name.startswith("plugin/")}
    for name in RELEASE_DOCUMENTS:
        files[name] = checked_bytes(root / name, root)
    files["plugin.json"] = checked_bytes(root / "plugin/plugin.json", root)
    files["launch.py"] = checked_bytes(root / "plugin/launch.py", root)
    manifest = json.loads(files["plugin.json"])
    if manifest.get("name") != "codex-trace" or manifest.get("version") != version:
        raise ValueError("Portable plugin manifest version must match the release")
    command = "runtime/python.exe" if runtime["platform"] == "win32" else "runtime/bin/python3"
    # 当前宿主将 ./command 解析为插件根绝对路径；PLUGIN_ROOT 只用于参数/环境/cwd。
    launch = {"command": "./" + command,
              "args": ["-I", "-B", "${PLUGIN_ROOT}/launch.py"], "cwd": "${PLUGIN_ROOT}",
              "env": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}}
    files["mcp.json"] = json_bytes({"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                                   "mcpServers": {"codex-trace": {"type": "stdio", **launch}}})
    # 0.153 CLI 仍识别兼容格式；两份配置只允许 transport type/schema 的差异。
    files[".mcp.json"] = json_bytes({"mcpServers": {"codex-trace": launch}})
    legacy = json.loads(files[".codex-plugin/plugin.json"])
    legacy["mcpServers"] = "./.mcp.json"
    files[".codex-plugin/plugin.json"] = json_bytes(legacy)
    audit_payload(files, root)
    return version, files


def install_dependencies(python: Path, wheelhouse: Path, requirements: Path, target: Path, home: Path) -> None:
    """只接受 wheel，不允许联网、sdist 构建或从用户 pip 配置补充依赖。"""
    if not wheelhouse.is_dir():
        raise ValueError("An existing offline wheelhouse is required")
    env = clean_environment(home)
    env.update(PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1", PIP_NO_COMPILE="1")
    result = subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "install",
                             "--no-index", "--find-links", str(wheelhouse), "--only-binary=:all:",
                             "--no-deps", "--no-compile", "--disable-pip-version-check", "--no-input",
                             "--target", str(target), "--requirement", str(requirements)],
                            env=env, cwd=home, capture_output=True, text=True, encoding="utf-8", timeout=600)
    if result.returncode:
        raise RuntimeError("Offline runtime dependency installation failed:\n" + (result.stdout + result.stderr)[-6000:])
    # wheel 的 console_scripts 带生成时的解释器绝对路径。MCP 只通过 launch.py
    # 导入模块，这些未使用的命令入口不应随包；metadata/许可证均完整保留。
    for relative in ("bin", "Scripts"):
        path = target / relative
        if path.exists():
            shutil.rmtree(path)
    normalize_dependency_records(target)


def normalize_dependency_records(target: Path) -> None:
    """删除已排除命令入口的 RECORD 行，避免其临时 shebang 哈希破坏确定性。

    wheel 中实际保留的模块、数据、授权文本及其内容哈希均不改写；除 pip
    console_scripts 的已知路径以外，越界记录直接拒绝，不悄悄丢弃文件。
    """
    for record in sorted(target.glob("*.dist-info/RECORD")):
        kept = []
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise ValueError("Malformed installed wheel RECORD")
            path = PurePosixPath(row[0])
            parts = list(path.parts)
            while parts and parts[0] == "..":
                parts.pop(0)
            if parts and parts[0] in ("bin", "Scripts"):
                continue
            if path.is_absolute() or ".." in path.parts or "\\" in row[0]:
                raise ValueError("Installed wheel RECORD contains an unsupported external path")
            if not (target / row[0]).is_file():
                raise ValueError("Installed wheel RECORD references a missing packaged file")
            kept.append(row)
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerows(kept)
        record.write_text(buffer.getvalue(), encoding="utf-8", newline="\n")


def verify_dependencies(package: Path, expected: dict[str, str], home: Path) -> dict:
    """在已迁移目录导入锁定依赖，核对实际模块和原生扩展来自随包目录。"""
    program = """import importlib.metadata,json,site,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); deps=root/'runtime-deps'
site.addsitedir(str(deps)); sys.path.insert(0,str(deps)); sys.path.insert(0,str(root/'backend'))
expected=json.loads(sys.argv[2])
actual={name:importlib.metadata.version(name) for name in expected}
assert actual == expected, ('dependency versions differ',actual)
for name in expected:
 assert Path(importlib.metadata.distribution(name).locate_file('')).resolve().is_relative_to(deps), name
import mcp,jsonschema,pydantic_core,cryptography,sqlite3,ssl
from compression import zstd
assert zstd.decompress(zstd.compress(b'candidate')) == b'candidate'
if sys.platform == 'win32':
 import win32api,win32security,pywintypes
print(json.dumps({'lockedDependencyCount':len(actual),'zstdAvailable':True,'nativeImports':True}))
"""
    result = subprocess.run([str(runtime_command(package / "runtime")), "-I", "-B", "-c", program,
                             str(package), json.dumps(expected)], cwd=home, env=clean_environment(home),
                            capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    return json.loads(result.stdout)


def verify_mcp_start(package: Path, version: str, home: Path) -> None:
    """真实 stdio 初始化不调用轨迹工具；隔离 HOME，绝不读取真实任务数据。"""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "marketplace-build-verifier", "version": "1"}}}
    result = subprocess.run([str(runtime_command(package / "runtime")), "-I", "-B", str(package / "launch.py")],
                            input=json.dumps(request) + "\n", cwd=home, env=clean_environment(home),
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise RuntimeError("Relocated MCP exited unsuccessfully: " + result.stderr[-3000:])
    try:
        reply = next(json.loads(line) for line in result.stdout.splitlines() if json.loads(line).get("id") == 1)
    except (ValueError, StopIteration) as error:
        raise RuntimeError("Relocated MCP initialization returned no JSON-RPC result: " + result.stderr[-3000:]) from error
    info = reply.get("result", {}).get("serverInfo", {})
    if info.get("name") != "codex-trace" or info.get("version") != version:
        raise ValueError("Relocated MCP identity/version differs from the candidate")


def trim_build_only_runtime_files(runtime: Path) -> None:
    """删除实测 full distribution 中的编译链接归档和 CPython 自测数据。

    两类文件不参与解释器/stdlib 执行或离线 wheel 导入；动态库、stdlib、
    pip 及所有授权文件保留。来源记录列出删除前的内容哈希，最终清单重算，
    后续迁移/导入/MCP 检查必须使用裁剪后的实际包。
    """
    provenance_path = runtime / PROVENANCE
    original = provenance_path.read_bytes()
    provenance = json.loads(original)
    removed = {}
    for name, path, _ in runtime_files(runtime):
        parts = Path(name).parts
        stdlib_test = ((len(parts) > 3 and parts[0] == "lib" and parts[1].startswith("python3.")
                       and parts[2] == "test") or (len(parts) > 2 and parts[:2] == ("Lib", "test")))
        static_archive = ((parts[0] == "lib" and path.suffix == ".a")
                          or (parts[0] == "libs" and path.suffix.lower() == ".lib"))
        if stdlib_test or static_archive:
            removed[name] = digest(path.read_bytes())
            path.unlink()
    # 空目录不进入ZIP；记录任何裁剪，不把变更伪装成上游原始分发字节。
    provenance["packaging"] = {"preparedProvenanceSha256": digest(original),
                               "excludedBuildAndTestFiles": removed}
    provenance["files"] = {name: digest(path.read_bytes()) for name, path, _ in runtime_files(runtime)
                           if name != PROVENANCE}
    provenance_path.write_bytes(json_bytes(provenance))


def archive_entries(directory: Path) -> list[tuple[str, Path, int]]:
    """在写入前执行门户大小与名称限制，失败不会产生可发布的候选文件。"""
    entries = runtime_files(directory)
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"Marketplace ZIP exceeds {MAX_ENTRIES} entries")
    seen, total = set(), 0
    for name, path, _ in entries:
        if len(Path(name).parts) > MAX_PATH_SEGMENTS:
            raise ValueError(f"Marketplace ZIP path exceeds {MAX_PATH_SEGMENTS} segments: {name}")
        normalized = unicodedata.normalize("NFC", name).casefold()
        if normalized in seen:
            raise ValueError(f"Marketplace ZIP has a case/Unicode-normalized name collision: {name}")
        seen.add(normalized)
        size = path.stat().st_size
        if size > MAX_ENTRY_BYTES:
            raise ValueError(f"Marketplace ZIP entry exceeds 100 MiB: {name}")
        total += size
    if total > MAX_EXPANDED_BYTES:
        raise ValueError("Marketplace ZIP exceeds 512 MiB expanded size")
    return entries


def write_archive(path: Path, directory: Path) -> dict[str, str]:
    """固定元数据、执行位和压缩等级；相同工具链可生成相同 ZIP 字节。"""
    fingerprints = {}
    entries = archive_entries(directory)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source, mode in entries:
            data = source.read_bytes()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            fingerprints[name] = digest(data)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Marketplace ZIP exceeds 100 MB compressed size")
    return fingerprints


def build_marketplace(output: Path, *, runtime: Path, wheelhouse: Path,
                      root: Path = ROOT, assets: Path | None = None) -> dict:
    root, output, runtime, wheelhouse = (path.resolve() for path in (root, output, runtime, wheelhouse))
    if output.is_relative_to(runtime):
        raise ValueError("Marketplace output must be outside the input runtime")
    provenance = validated_runtime(runtime)
    if assets is None:
        subprocess.run(npm_command("run", "build"), cwd=root, check=True,
                       env=dict(os.environ, SOURCE_DATE_EPOCH=os.environ.get("SOURCE_DATE_EPOCH", "0")))
        assets = root / "dist/ui"
    version, files = project_payload(root, assets, provenance)
    pins = locked_versions(root / "requirements.lock", platform=provenance["platform"])
    target_id = provenance["platform"] + "-" + provenance["architecture"]
    name = f"codex-trace-{version}-{target_id}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".marketplace-build-", dir=output) as temporary:
        temporary = Path(temporary)
        package = temporary / "staged"
        package.mkdir()
        for relative, data in files.items():
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # 与来源校验使用同一枚举，忽略的字节码不会被 copytree 跟随到包外链接。
        for relative, source, mode in runtime_files(runtime):
            target = package / "runtime" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            target.chmod(mode)
        home = temporary / "isolated home"
        home.mkdir()
        install_dependencies(runtime_command(package / "runtime"), wheelhouse, package / "requirements.lock",
                             package / "runtime-deps", home)
        trim_build_only_runtime_files(package / "runtime")
        moved = temporary / "moved 插件 candidate"
        package.rename(moved)
        runtime_check = probe_runtime(moved / "runtime")
        dependency_check = verify_dependencies(moved, pins, home)
        verify_mcp_start(moved, version, home)
        # 元数据只记录相对资源路径；不把构建解释器/目录写到用户安装中。
        metadata = {"schemaVersion": 1, "platform": provenance["platform"], "architecture": provenance["architecture"],
                    "minimumPython": "3.14", "runtimeBundled": True, "startupNetworkInstallation": False,
                    "entrypoint": "codex_trace.marketplace", "runtimeExecutable":
                    "runtime/python.exe" if provenance["platform"] == "win32" else "runtime/bin/python3",
                    "appServerExecutable": "discovered-at-startup", "runtime": runtime_check,
                    "uiSha256": digest(files["ui/trajectory-v1.html"]),
                    "requirementsSha256": digest(files["requirements.lock"]),
                    "thirdPartyManifestSha256": digest(files["third-party-manifest.json"]),
                    "runtimeProvenanceSha256": digest((moved / "runtime" / PROVENANCE).read_bytes())}
        (moved / "runtime.json").write_bytes(json_bytes(metadata))
        inventory = {relative: digest(path.read_bytes()) for relative, path, _ in runtime_files(moved)}
        manifest = {"schemaVersion": 1, "name": "codex-trace", "version": version,
                    "target": target_id, "platform": provenance["platform"], "architecture": provenance["architecture"],
                    "runtimeBundled": True, "startupNetworkInstallation": False, "files": inventory}
        (moved / PACKAGE_MANIFEST).write_bytes(json_bytes(manifest))
        # 只检查由项目生成的文本，不对二进制运行时套用通用 secret 正则。
        generated = {"runtime.json": json_bytes(metadata), PACKAGE_MANIFEST: json_bytes(manifest)}
        audit_payload(generated, root)
        archive = temporary / f"{name}.zip"
        written = write_archive(archive, moved)
        if {key: value for key, value in written.items() if key != PACKAGE_MANIFEST} != inventory:
            raise ValueError("Candidate changed while archiving")
        checksum = digest(archive.read_bytes())
        with zipfile.ZipFile(archive) as candidate:
            expanded_bytes = sum(info.file_size for info in candidate.infolist())
        target = output / archive.name
        if target.exists() and target.read_bytes() != archive.read_bytes():
            raise ValueError("Versioned marketplace candidate already exists with different content; use a new output or version")
        archive.replace(target)
        (output / "SHA256SUMS").write_text(f"{checksum}  {target.name}\n", encoding="ascii")
        report = {"passed": True, "archive": target.name, "sha256": checksum, "version": version,
                  "target": target_id, "fileCount": len(written), "runtime": runtime_check,
                  "archiveBytes": target.stat().st_size, "expandedBytes": expanded_bytes,
                  "portalArchiveLimitsVerified": True,
                  "dependencies": dependency_check, "relocatedPackageVerified": True,
                  "mcpInitializeVerified": True, "isolatedUserProfile": True,
                  "runtimeBundled": True, "startupNetworkInstallation": False,
                  "desktopUiVerified": False, "publicSubmissionApproved": False,
                  "runtimeProvenanceSha256": metadata["runtimeProvenanceSha256"]}
        (output / f"{name}.verification.json").write_bytes(json_bytes(report))
    return {**report, "archive": str(target), "checksums": str(output / "SHA256SUMS")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/marketplace")
    parser.add_argument("--assets", type=Path, help="Use an existing frontend build (normally omit)")
    args = parser.parse_args()
    try:
        print(json.dumps(build_marketplace(args.output, runtime=args.runtime, wheelhouse=args.wheelhouse,
                                          assets=args.assets), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Marketplace build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
