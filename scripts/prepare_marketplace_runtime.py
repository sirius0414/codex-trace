#!/usr/bin/env python3
"""把已下载的独立 CPython 准备成可迁移、可追溯的插件运行时。

本工具不下载、不创建 venv，也不改动输入。仅接受当前主机可执行的独立
CPython，展开包内相对链接、保留授权文本，并在改名后的目录验证解释器。
来源 URL、源树哈希及可选下载归档哈希是出处记录，不代表平台审核通过。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

PROVENANCE = "runtime-provenance.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def architecture(machine: str) -> str:
    aliases = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}
    try:
        return aliases[machine.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported runtime architecture: {machine}") from error


def runtime_command(root: Path, platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform not in ("darwin", "win32"):
        raise ValueError("Marketplace runtime supports native macOS and Windows only")
    return root / ("python.exe" if platform == "win32" else "bin/python3")


def runtime_files(root: Path) -> list[tuple[str, Path, int]]:
    """只展开根内相对链接，拒绝 venv、循环、特殊文件及 Windows 重解析点。"""
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Runtime root must be a real directory")
    resolved_root = root.resolve()
    result = []

    def walk(path: Path, relative: str, ancestors: set[Path]):
        info = path.lstat()
        if path.name == "pyvenv.cfg":
            raise ValueError("A virtualenv is not a portable CPython runtime")
        if path.name in {"__pycache__", ".DS_Store"} or path.suffix == ".pyc":
            return
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if Path(target).is_absolute():
                raise ValueError(f"Absolute runtime symlink: {relative}")
            actual = path.resolve(strict=True)
            if not actual.is_relative_to(resolved_root):
                raise ValueError(f"Runtime symlink escapes its root: {relative}")
        elif getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Runtime contains a reparse point: {relative}")
        actual = path.resolve(strict=True)
        if actual.is_dir():
            if actual in ancestors:
                raise ValueError(f"Runtime symlink cycle: {relative}")
            for child in sorted(path.iterdir()):
                walk(child, f"{relative}/{child.name}" if relative else child.name, ancestors | {actual})
        elif actual.is_file():
            mode = actual.stat().st_mode
            result.append((relative, actual, 0o755 if mode & 0o111 else 0o644))
        else:
            raise ValueError(f"Runtime contains a special file: {relative}")

    walk(root, "", set())
    if not result:
        raise ValueError("Runtime is empty")
    return result


def tree_hash(files: list[tuple[str, Path, int]]) -> str:
    digest = hashlib.sha256()
    for name, path, mode in files:
        data = path.read_bytes()
        digest.update(f"{name}\0{mode:o}\0{len(data)}\0".encode("utf-8"))
        digest.update(data)
    return digest.hexdigest()


def portable_sysconfig(data: bytes, source: Path) -> bytes:
    """uv 会写入安装前缀；仅重写静态 sysconfig 字典，改为启动时解析前缀。"""
    text = data.decode("utf-8")
    if str(source) not in text:
        return data
    module = ast.parse(text)
    assignments = [node for node in module.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "build_time_vars"]
    if len(assignments) != 1:
        raise ValueError("Cannot safely relocate runtime sysconfig metadata")
    values = ast.literal_eval(assignments[0].value)
    if not isinstance(values, dict):
        raise ValueError("Runtime sysconfig metadata must be a literal dictionary")
    marker = "__CODEX_TRACE_RUNTIME_PREFIX__"
    values = {key: value.replace(str(source), marker) if isinstance(value, str) else value
              for key, value in values.items()}
    output = "# Relocated build metadata; prefix follows this bundled interpreter.\nimport sys\n"
    output += "build_time_vars = " + repr(values) + "\n"
    output += f"build_time_vars = {{k: v.replace({marker!r}, sys.base_prefix) if isinstance(v, str) else v for k, v in build_time_vars.items()}}\n"
    return output.encode("utf-8")


def verify_runtime_licenses(licenses: dict[str, bytes]) -> dict:
    """以完整分发 PYTHON.json 的递归许可引用为准，不用单一 CPython LICENSE 代替。"""
    if "PYTHON.json" not in licenses:
        raise ValueError("Include full distribution PYTHON.json and every referenced runtime license with --license-file")
    metadata = json.loads(licenses["PYTHON.json"])
    required = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("license_path", "license_paths"):
                    paths = [item] if key == "license_path" else item
                    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                        raise ValueError("Invalid runtime license paths in PYTHON.json")
                    for path in paths:
                        if not path.startswith("licenses/") or ".." in path.split("/") or "\\" in path:
                            raise ValueError("Unsafe runtime license path in PYTHON.json")
                        required.add(Path(path).name)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(metadata)
    if not required or required - licenses.keys():
        missing = ", ".join(sorted(required - licenses.keys())) or "no license inventory"
        raise ValueError("Runtime distribution licenses are incomplete: " + missing)
    if metadata.get("python_implementation_name") != "cpython" or not isinstance(metadata.get("python_version"), str):
        raise ValueError("PYTHON.json must describe the supplied CPython distribution")
    return metadata


def clean_environment(home: Path) -> dict[str, str]:
    """隔离探测不继承 Python 注入或用户 Codex 路径，禁止写入输入运行时。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "CODEX_TRACE_")) and key != "CODEX_HOME"}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"),
               LOCALAPPDATA=str(home / "AppData/Local"), APPDATA=str(home / "AppData/Roaming"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK="1")
    return env


def probe_runtime(root: Path) -> dict:
    """实测独立前缀、zstd 和原生模块；返回值不包含本机绝对路径。"""
    program = """import json,sys,platform,ssl,sqlite3,sysconfig
from pathlib import Path
from compression import zstd
r=Path(sys.argv[1]).resolve()
assert sys.implementation.name == 'cpython' and sys.version_info >= (3,14)
assert sys.prefix == sys.base_prefix, 'virtualenv runtime'
assert Path(sys.base_prefix).resolve() == r, 'external runtime prefix'
assert Path(sys.executable).resolve().is_relative_to(r), 'external executable'
assert Path(ssl.__file__).resolve().is_relative_to(r), 'external standard library'
assert zstd.decompress(zstd.compress(b'portable')) == b'portable'
print(json.dumps({'pythonVersion':platform.python_version(),'platform':sys.platform,'machine':platform.machine(), 'zstdAvailable':True}))
"""
    with tempfile.TemporaryDirectory(prefix="codex-trace-runtime-probe-") as temporary:
        result = subprocess.run([str(runtime_command(root)), "-I", "-B", "-c", program, str(root)],
                                cwd=temporary, env=clean_environment(Path(temporary)), capture_output=True,
                                text=True, encoding="utf-8", check=True, timeout=45)
    record = json.loads(result.stdout)
    if record["platform"] != sys.platform:
        raise ValueError("Runtime platform must match this native validation host")
    record["architecture"] = architecture(record.pop("machine"))
    return record


def prepare_runtime(source: Path, output: Path, *, source_url: str,
                    license_files: list[Path], source_archive: Path | None = None) -> dict:
    source, output = source.absolute(), output.absolute()
    output = output.parent.resolve() / output.name
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Runtime provenance requires a public HTTPS source URL without credentials or query")
    if output.exists() or output.is_symlink() or output.is_relative_to(source.resolve()):
        raise ValueError("Runtime output must be a new directory outside the source runtime")
    if not license_files:
        raise ValueError("Provide the runtime distribution license files with --license-file")
    licenses = {}
    for path in license_files:
        if path.is_symlink() or not path.is_file() or not path.read_bytes().strip():
            raise ValueError(f"Runtime license must be a nonempty regular file: {path.name}")
        if path.name in licenses:
            raise ValueError(f"Duplicate runtime license filename: {path.name}")
        licenses[path.name] = path.read_bytes()
    distribution = verify_runtime_licenses(licenses)
    files = runtime_files(source)
    original_hash = tree_hash(files)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".runtime-prepare-", dir=output.parent) as temporary:
        stage = Path(temporary) / "before relocation"
        stage.mkdir()
        normalized = []
        for name, path, mode in files:
            data = path.read_bytes()
            if Path(name).name.startswith("_sysconfigdata_") and path.suffix == ".py":
                fixed = portable_sysconfig(data, source)
                if fixed != data:
                    normalized.append(name)
                data = fixed
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        # 所有文件的根内链接已展开；最终 ZIP 不依赖解压器的符号链接支持。
        for name, data in licenses.items():
            target = stage / "LICENSES/cpython-runtime" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # 二进制不能按文本替换。仅拒绝准备过程中已知的本机路径残留。
        for name, path, _ in runtime_files(stage):
            data = path.read_bytes()
            if b"\0" not in data and any(value.encode() in data for value in (str(source), str(Path.home()))):
                raise ValueError(f"Local absolute path remains in runtime text: {name}")
        moved = stage.with_name("relocated 运行时")
        stage.rename(moved)
        runtime = probe_runtime(moved)
        if distribution["python_version"] != runtime["pythonVersion"]:
            raise ValueError("Runtime version differs from the licensed PYTHON.json distribution")
        record = {"schemaVersion": 1, "kind": "standalone-cpython", **runtime,
                  "source": {"url": source_url, "treeSha256": original_hash},
                  "normalizations": {"internalSymlinksMaterialized": True,
                                     "bytecodeExcluded": True, "dynamicPrefixSysconfig": normalized},
                  "licenses": {"LICENSES/cpython-runtime/" + name: sha256(data) for name, data in sorted(licenses.items())},
                  "files": {name: sha256(path.read_bytes()) for name, path, _ in runtime_files(moved)},
                  "relocationVerified": True}
        if source_archive:
            record["source"]["archiveSha256"] = sha256(source_archive.read_bytes())
        (moved / PROVENANCE).write_bytes(json_bytes(record))
        moved.rename(output)
    return {"runtime": str(output), "platform": runtime["platform"], "architecture": runtime["architecture"],
            "pythonVersion": runtime["pythonVersion"], "provenanceSha256": sha256((output / PROVENANCE).read_bytes())}


def validated_runtime(root: Path) -> dict:
    """组包前核对准备记录及全部内容，拒绝准备后修改或添加的运行时代码。"""
    files = runtime_files(root)
    record = json.loads((root / PROVENANCE).read_bytes())
    if record.get("schemaVersion") != 1 or record.get("kind") != "standalone-cpython" or not record.get("relocationVerified"):
        raise ValueError("Runtime requires prepare_marketplace_runtime.py provenance")
    actual = {name: sha256(path.read_bytes()) for name, path, _ in files if name != PROVENANCE}
    if actual != record.get("files"):
        raise ValueError("Prepared runtime files do not match their provenance")
    if not record.get("licenses") or any(actual.get(name) != digest for name, digest in record["licenses"].items()):
        raise ValueError("Runtime license provenance is incomplete")
    distribution = verify_runtime_licenses({Path(name).name: (root / name).read_bytes() for name in record["licenses"]})
    runtime = probe_runtime(root)
    if distribution["python_version"] != runtime["pythonVersion"] or any(record.get(key) != value for key, value in runtime.items()):
        raise ValueError("Runtime platform or interpreter differs from provenance")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument("--license-file", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_runtime(args.source, args.output, source_url=args.source_url,
              license_files=args.license_file, source_archive=args.source_archive), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Marketplace runtime preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
