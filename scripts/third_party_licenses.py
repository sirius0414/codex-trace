#!/usr/bin/env python3
"""从锁定的安装或 wheel 收录授权原文；打包只读取经校验的仓库快照。

npm 清单覆盖全部生产依赖（含可能被 tree-shaking 移除的传递依赖）。Python
运行环境不随插件复制，其授权文件作为配套依赖说明收录。此脚本不下载依赖。
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCKFILES = ("package-lock.json", "requirements.lock")
LEGAL_NAME = re.compile(r"^(?:licen[cs]es?|notices?|copying|copyright|authors)(?:[._-].*)?$", re.I)
LICENSE_NAME = re.compile(r"^(?:licen[cs]es?|copying)(?:[._-].*)?$", re.I)
REFRESH = "Run .venv/bin/python scripts/third_party_licenses.py --write after installing the locked dependencies; use --wheelhouse DIR for unavailable platform wheels."


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


def python_pins(lockfile: Path) -> list[dict]:
    """Parse exact pins and a deliberately narrow, stdlib-only platform marker.

    Unsupported marker expressions fail closed rather than silently omitting
    dependencies. License inventory retains all platforms; runtime filters them.
    """
    pins = []
    identities = set()
    pattern = r'''([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*sys_platform\s*==\s*(['"])([a-z0-9_]+)\3)?'''
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = re.fullmatch(pattern, text)
        if not match:
            raise ValueError(f"requirements.lock requires exact package==version pins; only sys_platform == 'value' markers are supported: {text}")
        identity = re.sub(r"[-_.]+", "-", match[1]).lower()
        if identity in identities:
            raise ValueError(f"Duplicate Python dependency pin: {match[1]}")
        identities.add(identity)
        item = {"name": match[1], "version": match[2]}
        if match[4]:
            item["sys_platform"] = match[4]
        pins.append(item)
    return pins


def dependencies(root: Path) -> list[dict]:
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    components = []
    for location, package in sorted(lock["packages"].items()):
        if location and not package.get("dev"):
            relative_path(location)
            name = location.rsplit("node_modules/", 1)[-1]
            components.append({"ecosystem": "npm", "location": location, "name": name, "version": package["version"]})
    for pin in python_pins(root / "requirements.lock"):
        item = {"ecosystem": "python", "location": pin["name"], "name": pin["name"], "version": pin["version"]}
        if "sys_platform" in pin:
            item["marker"] = f"sys_platform == '{pin['sys_platform']}'"
        components.append(item)
    return components


def legal_document_name(path: PurePosixPath) -> str | None:
    parts = path.parts
    if any(part.endswith(".dist-info") for part in parts):
        if "licenses" in parts:
            return PurePosixPath(*parts[parts.index("licenses") + 1:]).as_posix()
        if LEGAL_NAME.fullmatch(path.name):
            return path.name
    elif LEGAL_NAME.fullmatch(path.name):
        # Some wheels (including pywin32) place licenses beside package code,
        # rather than under dist-info. Preserve each package's attribution.
        return path.as_posix()
    return None


def wheel_documents(item: dict, wheelhouse: Path) -> tuple[dict, dict[str, bytes]] | None:
    """Read matching wheel metadata and legal text without installing its code."""
    normalized = re.sub(r"[-_.]+", "-", item["name"]).lower()
    for wheel in sorted(wheelhouse.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                continue
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            if re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower() != normalized or metadata.get("Version") != item["version"]:
                continue
            documents = {}
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                name = legal_document_name(PurePosixPath(entry.filename))
                if name:
                    relative_path(entry.filename)
                    relative_path(name)
                    data = archive.read(entry)
                    if name in documents and documents[name] != data:
                        raise ValueError(f"Conflicting wheel legal documents: {name}")
                    documents[name] = data
            details = {"declaredLicense": metadata.get("License-Expression") or metadata.get("License"),
                "source": metadata.get_all("Project-URL") or metadata.get("Home-page"),
                "licenseArtifact": {"filename": wheel.name, "sha256": sha256(wheel.read_bytes())}}
            return details, documents
    return None


def snapshot_documents(root: Path, expected: dict) -> tuple[dict, dict[str, bytes]] | None:
    """Reuse only a matching component from a fully checksum-verified snapshot.

    Old lock fingerprints may differ during refresh; component identity and
    platform marker must still exactly match the new inventory.
    """
    directory = root / "third_party"
    path = directory / "manifest.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    verify_documents(directory, manifest)
    for component in manifest["components"]:
        if all(component.get(key) == value for key, value in expected.items()) and component.get("marker") == expected.get("marker"):
            prefix = f"LICENSES/python/{expected['name']}@{expected['version']}/"
            if any(not name.startswith(prefix) for name in component["files"]):
                raise ValueError("Snapshot component has an invalid document prefix")
            documents = {name[len(prefix):]: (directory / relative_path(name)).read_bytes() for name in component["files"]}
            details = {key: component[key] for key in ("declaredLicense", "source", "licenseArtifact") if key in component}
            return details, documents
    return None


def collect(root: Path, wheelhouse: Path | None = None) -> tuple[dict, dict[str, bytes]]:
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
            try:
                distribution = importlib.metadata.distribution(item["name"])
            except importlib.metadata.PackageNotFoundError:
                found = wheel_documents(item, wheelhouse) if wheelhouse else None
                found = found or snapshot_documents(root, expected)
                if not found:
                    raise ValueError(f"Missing locked Python distribution or verified license snapshot: {item['name']}@{item['version']}; provide its wheel using --wheelhouse DIR")
                details, documents = found
                item.update(details)
            else:
                if distribution.version != item["version"]:
                    raise ValueError(f"Installed Python package differs from lockfile: {item['name']}")
                item["declaredLicense"] = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
                item["source"] = distribution.metadata.get_all("Project-URL") or distribution.metadata.get("Home-page")
                for path in distribution.files or []:
                    name = legal_document_name(PurePosixPath(path.as_posix()))
                    if name:
                        relative_path(name)
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
    notice = ["Codex Trace — Third-party software notices", "",
              "Full upstream copyright, permission, disclaimer and NOTICE texts are preserved in LICENSES/.",
              "Declared license identifiers below are informational; the upstream texts govern.",
              "npm: all locked production dependencies, including dependencies that may be tree-shaken.",
              "Python: accompanying license information for the separately installed runtime; its code is not bundled.",
              "Development-only npm tools, Python itself and the external Codex executable are not bundled.", ""]
    for item in components:
        notice += [f"{item['ecosystem']}: {item['name']} {item['version']}",
                   f"Declared license: {item['declaredLicense'] or 'See upstream license texts'}"]
        if item.get("marker"):
            notice.append(f"Platform condition: {item['marker']}")
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
        actual = []
        for item in manifest["components"]:
            identity = {key: item.get(key) for key in ("ecosystem", "location", "name", "version")}
            if "marker" in item:
                identity["marker"] = item["marker"]
            actual.append(identity)
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
    parser.add_argument("--write", action="store_true", help="Refresh third_party/ from locked installed packages, wheels, or a verified snapshot")
    parser.add_argument("--wheelhouse", type=Path, help="Read missing platform dependency licenses from wheels in this directory; never installs wheel code")
    args = parser.parse_args()
    try:
        if args.write:
            manifest, files = collect(ROOT, args.wheelhouse)
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
    except (OSError, ValueError, zipfile.BadZipFile, importlib.metadata.PackageNotFoundError) as error:
        print(f"Third-party licenses: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
