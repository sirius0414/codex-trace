#!/usr/bin/env python3
"""从锁定的 npm/Python 安装收录授权原文；打包只读取经校验的仓库快照。

npm 清单覆盖全部生产依赖（含可能被 tree-shaking 移除的传递依赖）。Python
运行环境不随插件复制，其授权文件作为配套依赖说明收录。此脚本不下载依赖。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCKFILES = ("package-lock.json", "requirements.lock")
LEGAL_NAME = re.compile(r"^(?:licen[cs]es?|notices?|copying|copyright|authors)(?:[._-].*)?$", re.I)
LICENSE_NAME = re.compile(r"^(?:licen[cs]es?|copying)(?:[._-].*)?$", re.I)
REFRESH = "Run .venv/bin/python scripts/third_party_licenses.py --write after installing the locked dependencies."


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock_fingerprints(root: Path) -> dict[str, str]:
    # Git 的平台换行转换不改变依赖版本；授权原文则按原始字节校验。
    return {name: sha256((root / name).read_text(encoding="utf-8").encode("utf-8")) for name in LOCKFILES}


def relative_path(value: str) -> Path:
    """清单使用跨平台相对路径，不能将源环境的绝对路径带入发行包。"""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in (".", "..") for part in value.split("/")) or re.search(r"[\\:\x00]", value):
        raise ValueError(f"Invalid third-party document path: {value!r}")
    return Path(*path.parts)


def dependencies(root: Path) -> list[dict]:
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    components = []
    for location, package in sorted(lock["packages"].items()):
        if location and not package.get("dev"):
            relative_path(location)
            name = location.rsplit("node_modules/", 1)[-1]
            components.append({"ecosystem": "npm", "location": location, "name": name, "version": package["version"]})
    for line in (root / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line.strip())
        if not match:
            raise ValueError("Third-party inventory requires exact Python dependency pins")
        components.append({"ecosystem": "python", "location": match[1], "name": match[1], "version": match[2]})
    return components


def collect(root: Path) -> tuple[dict, dict[str, bytes]]:
    """保留安装包内 LICENSE、NOTICE、AUTHORS 等文件的完整字节，而非 SPDX 摘要。"""
    files = {}
    components = []
    for expected in dependencies(root):
        item = dict(expected)
        documents = {}
        if item["ecosystem"] == "npm":
            package_root = root / relative_path(item["location"])
            metadata = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
            if metadata["name"] != item["name"] or metadata["version"] != item["version"]:
                raise ValueError(f"Installed npm package differs from lockfile: {item['location']}")
            item["declaredLicense"] = metadata.get("license")
            item["source"] = metadata.get("repository") or metadata.get("homepage")
            for path in sorted(package_root.iterdir()):
                if LEGAL_NAME.fullmatch(path.name):
                    candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
                    for candidate in candidates:
                        if candidate.is_file():
                            documents[candidate.relative_to(package_root).as_posix()] = candidate.read_bytes()
        else:
            distribution = importlib.metadata.distribution(item["name"])
            if distribution.version != item["version"]:
                raise ValueError(f"Installed Python package differs from lockfile: {item['name']}")
            item["declaredLicense"] = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
            item["source"] = distribution.metadata.get_all("Project-URL") or distribution.metadata.get("Home-page")
            for path in distribution.files or []:
                parts = path.parts
                # 新 wheel 的整个 licenses/ 树均保留；旧 wheel 使用 dist-info 根授权文件。
                if not any(part.endswith(".dist-info") for part in parts):
                    continue
                if "licenses" in parts:
                    name = PurePosixPath(*parts[parts.index("licenses") + 1:]).as_posix()
                elif LEGAL_NAME.fullmatch(path.name):
                    name = path.name
                else:
                    continue
                documents[name] = Path(distribution.locate_file(path)).read_bytes()
        if not any(LICENSE_NAME.fullmatch(PurePosixPath(name).name) for name in documents):
            raise ValueError(f"Missing upstream license text: {item['name']}@{item['version']}")
        item["files"] = []
        for name, data in sorted(documents.items()):
            if not data.strip():
                raise ValueError(f"Empty upstream legal document: {item['name']}/{name}")
            destination = f"LICENSES/{item['ecosystem']}/{item['name']}@{item['version']}/{name}"
            relative_path(destination)
            if destination in files and files[destination] != data:
                raise ValueError(f"Conflicting upstream legal documents: {destination}")
            files[destination] = data
            item["files"].append(destination)
        components.append(item)
    notice = ["Codex Display — Third-party software notices", "",
              "Full upstream copyright, permission, disclaimer and NOTICE texts are preserved in LICENSES/.",
              "Declared license identifiers below are informational; the upstream texts govern.",
              "npm: all locked production dependencies, including dependencies that may be tree-shaken.",
              "Python: accompanying license information for the separately installed runtime; its code is not bundled.",
              "Development-only npm tools, Python itself and the external Codex executable are not bundled.", ""]
    for item in components:
        notice += [f"{item['ecosystem']}: {item['name']} {item['version']}",
                   f"Declared license: {item['declaredLicense'] or 'See upstream license texts'}"]
        notice += [f"  {name}" for name in item["files"]]
        notice += [""]
    files["THIRD_PARTY_NOTICES.txt"] = ("\n".join(notice).rstrip("\n") + "\n").encode("utf-8")
    manifest = {"schemaVersion": 1, "lockfiles": lock_fingerprints(root),
                "components": components, "files": {name: sha256(data) for name, data in sorted(files.items())}}
    return manifest, files


def verify_documents(directory: Path, manifest: dict) -> None:
    """源快照和安装缓存共用校验，保证完整授权文件随复制与移动保留。"""
    if manifest.get("schemaVersion") != 1 or not manifest.get("components") or not manifest.get("files"):
        raise ValueError("Invalid third-party license manifest")
    referenced = {"THIRD_PARTY_NOTICES.txt"}
    for item in manifest["components"]:
        names = item.get("files", [])
        if not names or not any(LICENSE_NAME.fullmatch(PurePosixPath(name).name) for name in names):
            raise ValueError(f"Missing license in third-party inventory: {item.get('name')}")
        if any(not name.startswith("LICENSES/") for name in names):
            raise ValueError("Third-party component documents must be under LICENSES/")
        referenced.update(names)
    if referenced != set(manifest["files"]):
        raise ValueError("Third-party inventory has missing or unreferenced documents")
    for name, expected in manifest["files"].items():
        path = directory / relative_path(name)
        if not path.is_file() or not path.read_bytes().strip() or sha256(path.read_bytes()) != expected:
            raise ValueError(f"Third-party license document is missing or changed: {name}")


def check_snapshot(root: Path) -> dict:
    directory = root / "third_party"
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        expected_locks = lock_fingerprints(root)
        if manifest.get("lockfiles") != expected_locks:
            raise ValueError("Third-party license snapshot is stale for the dependency lockfiles")
        expected = dependencies(root)
        actual = [{key: item.get(key) for key in ("ecosystem", "location", "name", "version")} for item in manifest["components"]]
        if actual != expected:
            raise ValueError("Third-party license inventory does not cover the locked dependencies")
        verify_documents(directory, manifest)
        if not (root / "NOTICE").read_bytes().strip():
            raise ValueError("Project NOTICE is empty")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Third-party license validation failed: {error}. {REFRESH}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Refresh third_party/ from already installed locked dependencies")
    args = parser.parse_args()
    try:
        if args.write:
            manifest, files = collect(ROOT)
            directory = ROOT / "third_party"
            # 仅在全部依赖成功收录后写入；旧文件可留作历史，但只分发清单列出的文件。
            for name, data in files.items():
                path = directory / relative_path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = check_snapshot(ROOT)
        print(json.dumps({"passed": True, "components": len(manifest["components"]), "documents": len(manifest["files"]) - 1}))
        return 0
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as error:
        print(f"Third-party licenses: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
