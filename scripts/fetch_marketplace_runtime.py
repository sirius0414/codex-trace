#!/usr/bin/env python3
"""从固定发行归档准备原生 Marketplace Python，不依赖构建者的虚拟环境。

下载或复用归档后先核对锁定 SHA-256，再有界提取运行时、发行元数据和完整
许可目录。元数据引用的许可证缺失时在执行解释器前失败；最终重定位验证由
prepare_marketplace_runtime 完成。此工具不执行上游安装脚本。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen

import prepare_marketplace_runtime as prepare

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "marketplace-runtime.lock.json"
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 50_000
CHUNK_BYTES = 1024 * 1024
TARGETS = {"darwin-arm64", "darwin-x64", "win32-x64"}
UPSTREAM_PREFIX = "https://github.com/astral-sh/python-build-standalone/releases/download/20260114/"


class RuntimeFetchError(ValueError):
    """下载、来源或归档检查失败；不将不完整输入交给运行时准备器。"""


def native_target(platform_name: str | None = None, machine: str | None = None) -> str:
    """只支持已锁定的原生目标，禁止把其他系统的包当作本机验证结果。"""
    target = f"{sys.platform if platform_name is None else platform_name}-{prepare.architecture(platform.machine() if machine is None else machine)}"
    if target not in TARGETS:
        raise RuntimeFetchError(f"Unsupported native runtime target: {target}")
    return target


def locked_runtime(target: str, lock_path: Path = LOCK) -> dict:
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    if record.get("schemaVersion") != 1 or record.get("pythonVersion") != "3.14.2" or record.get("release") != "20260114":
        raise RuntimeFetchError("Unsupported marketplace runtime lock format or release")
    if target not in TARGETS or set(record.get("targets", {})) != TARGETS:
        raise RuntimeFetchError("Runtime lock must contain exactly the supported native targets")
    entry = record["targets"][target]
    if (not isinstance(entry, dict) or not isinstance(entry.get("url"), str)
            or not entry["url"].startswith(UPSTREAM_PREFIX)
            or not entry["url"].endswith("-full.tar.zst")
            or any(value in entry["url"] for value in ("?", "#"))
            or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
            or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_EXTRACTED_BYTES
            or not isinstance(entry.get("targetTriple"), str)):
        raise RuntimeFetchError("Runtime lock contains an invalid archive declaration")
    supplements = record.get("supplementalLicenses", {})
    for name, declaration in supplements.items():
        if (not safe_name(name).startswith("licenses/") or "/" in name.removeprefix("licenses/")
                or not isinstance(declaration, dict)
                or not declaration.get("url", "").startswith("https://raw.githubusercontent.com/python/cpython-source-deps/")
                or any(value in declaration["url"] for value in ("?", "#"))
                or not re.fullmatch(r"[0-9a-f]{64}", declaration.get("sha256", ""))
                or type(declaration.get("size")) is not int or not 0 < declaration["size"] <= CHUNK_BYTES
                or not declaration.get("version") or not declaration.get("component")):
            raise RuntimeFetchError("Runtime lock contains an invalid supplemental license declaration")
    return {**entry, "target": target, "pythonVersion": record["pythonVersion"], "supplementalLicenses": supplements}


def verify_archive(path: Path, entry: dict) -> None:
    """逐块校验用户提供或缓存的归档，避免先解压再发现来源不符。"""
    if path.is_symlink() or not path.is_file():
        raise RuntimeFetchError("Runtime source must be a regular file, not a symbolic link")
    if path.stat().st_size != entry["size"]:
        raise RuntimeFetchError("Runtime source size differs from marketplace-runtime.lock.json")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"]:
        raise RuntimeFetchError("Runtime source SHA-256 differs from marketplace-runtime.lock.json")


def acquire_archive(entry: dict, cache_dir: Path, archive: Path | None = None) -> Path:
    """下载只写新的缓存临时文件；校验通过后发布，失败不覆盖已有归档。"""
    if archive is not None:
        archive = archive.absolute()
        verify_archive(archive, entry)
        return archive
    return download_verified(entry, cache_dir, f"{entry['target']}-{entry['sha256']}.tar.zst")


def download_verified(entry: dict, cache_dir: Path, name: str) -> Path:
    """归档与补充许可复用同一校验缓存；下载失败不发布半成品。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / name
    if cached.exists() or cached.is_symlink():
        verify_archive(cached, entry)
        return cached
    request = Request(entry["url"], headers={"User-Agent": "codex-trace-runtime-fetch/0.1"})
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".runtime-download-", dir=cache_dir, delete=False) as output:
            temporary = Path(output.name)
            with urlopen(request, timeout=60) as response:
                size = 0
                while chunk := response.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise RuntimeFetchError("Runtime download exceeds its locked size")
                    output.write(chunk)
        verify_archive(temporary, entry)
        # 缓存只在完整校验后可见，其他构建不得读到半截下载。
        os.replace(temporary, cached)
        temporary = None
        return cached
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeFetchError("Runtime download failed; retry with network access or provide the locked archive and supplemental license cache") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def safe_name(name: str) -> str:
    """同时拒绝 POSIX 与 Windows 路径别名；不依赖 data_filter 的去斜线行为。"""
    value = name.rstrip("/")
    parts = value.split("/")
    if (not value or value.startswith("/") or "\\" in value or "\0" in value
            or any(part in ("", ".", "..") or ":" in part for part in parts)):
        raise RuntimeFetchError("Runtime archive contains an unsafe member path")
    return value


def selected_name(name: str) -> bool:
    return (name == "python/PYTHON.json" or name in ("python/install", "python/licenses")
            or name.startswith(("python/install/", "python/licenses/")))


def validate_link(member: tarfile.TarInfo, name: str) -> None:
    """链接只能指向运行时内部；许可证与元数据必须为真实文件。"""
    link = member.linkname
    if (not name.startswith("python/install/") or not link or link.startswith("/")
            or "\\" in link or "\0" in link or ":" in link):
        raise RuntimeFetchError("Runtime archive contains an unsafe link")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link) if member.issym() else link)
    if not target.startswith("python/install/"):
        raise RuntimeFetchError("Runtime archive link escapes the runtime root")


def extract_runtime(archive: Path, destination: Path) -> Path:
    """先检查完整成员表与解压预算，再仅以 tarfile data filter 提取白名单。"""
    with tarfile.open(archive, "r:*") as source:
        selected = []
        names = set()
        total = 0
        for count, member in enumerate(source, start=1):
            if count > MAX_MEMBERS:
                raise RuntimeFetchError("Runtime archive has too many members")
            name = safe_name(member.name)
            if name in names:
                raise RuntimeFetchError("Runtime archive contains duplicate member paths")
            names.add(name)
            total += member.size
            if member.size < 0 or total > MAX_EXTRACTED_BYTES:
                raise RuntimeFetchError("Runtime archive exceeds the 512 MiB extraction limit")
            if not selected_name(name):
                continue
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()) or member.issparse():
                raise RuntimeFetchError("Runtime archive contains a special or sparse file")
            if member.issym() or member.islnk():
                validate_link(member, name)
            elif name == "python/PYTHON.json" and not member.isfile():
                raise RuntimeFetchError("Runtime metadata must be a regular file")
            selected.append(member)
        source.extractall(destination, members=selected, filter="data")
    root = destination / "python"
    if not (root / "install").is_dir() or not (root / "licenses").is_dir():
        raise RuntimeFetchError("Runtime archive lacks its install or licenses directory")
    return root


def license_references(root: Path, entry: dict) -> set[str]:
    """递归读取发行元数据的许可路径，所有路径都限制在许可证目录。"""
    metadata_path = root / "PYTHON.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise RuntimeFetchError("Runtime archive lacks regular PYTHON.json metadata")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("python_version") != entry["pythonVersion"] or metadata.get("target_triple") != entry["targetTriple"]:
        raise RuntimeFetchError("PYTHON.json version or target differs from the runtime lock")
    references = set()

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("license_path", "license_paths"):
                    paths = [child] if key == "license_path" else child
                    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
                        raise RuntimeFetchError("PYTHON.json contains malformed license references")
                    for path in paths:
                        if not safe_name(path).startswith("licenses/"):
                            raise RuntimeFetchError("PYTHON.json license reference escapes its licenses directory")
                        references.add(path)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(metadata)
    if not references:
        raise RuntimeFetchError("PYTHON.json declares no runtime licenses")
    return references


def complete_licenses(root: Path, entry: dict, cache_dir: Path) -> dict:
    """仅用锁定的同版本来源补齐发行归档漏装的许可，并记录独立出处。"""
    supplements = {}
    for name in sorted(license_references(root, entry)):
        destination = root / name
        if destination.exists():
            continue
        declaration = entry.get("supplementalLicenses", {}).get(name)
        if declaration is None:
            continue  # runtime_licenses 统一列出仍缺失的引用，不能凭名称猜测许可。
        source = download_verified(declaration, cache_dir, destination.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        supplements[name] = declaration
    return supplements


def runtime_licenses(root: Path, entry: dict) -> list[Path]:
    """核对 PYTHON.json 声明的所有许可；缺项不能靠单个 CPython LICENSE 补位。"""
    references = license_references(root, entry)
    missing = sorted(path for path in references if not (root / path).is_file()
                     or (root / path).is_symlink() or not (root / path).read_bytes().strip())
    if missing:
        raise RuntimeFetchError("Runtime distribution is missing licenses referenced by PYTHON.json: " + ", ".join(missing))
    licenses = []
    for path in sorted((root / "licenses").rglob("*")):
        if path.is_symlink():
            raise RuntimeFetchError("Runtime license collection contains a symbolic link")
        if path.is_file():
            if not path.read_bytes().strip():
                raise RuntimeFetchError("Runtime license collection contains an empty file")
            licenses.append(path)
    return [*licenses, root / "PYTHON.json"]


def fetch_runtime(output: Path, *, cache_dir: Path, archive: Path | None = None,
                  lock_path: Path = LOCK) -> dict:
    if sys.version_info < (3, 14):
        raise RuntimeFetchError("Fetching a Marketplace runtime requires Python 3.14+ with tarfile zstd support")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise RuntimeFetchError("Runtime output already exists; choose a new directory")
    target = native_target()
    entry = locked_runtime(target, lock_path)
    downloaded = acquire_archive(entry, cache_dir.absolute(), archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".runtime-fetch-", dir=output.parent) as temporary:
        root = extract_runtime(downloaded, Path(temporary))
        supplements = complete_licenses(root, entry, cache_dir.absolute())
        licenses = runtime_licenses(root, entry)
        prepared = Path(temporary) / "prepared"
        result = prepare.prepare_runtime(root / "install", prepared, source_url=entry["url"],
                                         source_archive=downloaded, license_files=licenses)
        provenance_path = prepared / prepare.PROVENANCE
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["source"]["supplementalLicenses"] = supplements
        provenance_path.write_bytes(prepare.json_bytes(provenance))
        if output.exists() or output.is_symlink():
            raise RuntimeFetchError("Runtime output appeared during preparation; refusing to replace it")
        prepared.rename(output)
    return {**result, "runtime": str(output), "target": target, "archiveSha256": entry["sha256"],
            "provenanceSha256": hashlib.sha256((output / prepare.PROVENANCE).read_bytes()).hexdigest(),
            "runtimeLicenseFiles": len(licenses) - 1, "upstreamLicenseReferencesVerified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build/marketplace-runtime-cache")
    parser.add_argument("--archive", type=Path, help="Use an existing archive after checking its locked SHA-256")
    args = parser.parse_args()
    try:
        print(json.dumps(fetch_runtime(args.output, cache_dir=args.cache_dir, archive=args.archive), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"Marketplace runtime fetch failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
