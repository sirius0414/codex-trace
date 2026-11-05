#!/usr/bin/env python3
"""Build a portable, deterministic release without reading a local Codex profile.

Run after npm ci. The default command builds the UI itself; --assets is intended
for release tests and accepts an already built UI. No interpreter, virtualenv,
credentials, development checkout or session data are copied into the archive.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

from package_plugin import locked_versions, offline_html
from platform_runtime import npm_command
from third_party_licenses import check_snapshot

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("platform_runtime.py", "package_plugin.py", "prepare_marketplace.py",
           "update_plugin.py", "third_party_licenses.py")
# 所有分发共用显式文档白名单，语言切换不能指向未随包提供的译本。
PUBLIC_DOCUMENTS = tuple(f"{name}{locale}.md" for name in (
    "README", "INSTALL", "WINDOWS", "MARKETPLACE", "CONTRIBUTING", "SECURITY",
    "PRIVACY", "SUPPORT", "TERMS", "contracts/README", "third_party/README")
    for locale in ("", ".zh-CN")) + ("LICENSE.zh-CN.md", "NOTICE.zh-CN.md")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_bytes(path: Path, root: Path) -> bytes:
    """Every copied file must be regular and remain under its declared source root."""
    if not path.is_relative_to(root):
        raise ValueError(f"Source escapes release root: {path}")
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Release source contains a symlink or reparse point: {path}")
        if parent == root:
            break
    if not path.is_file():
        raise ValueError(f"Missing release input: {path}")
    return path.read_bytes()


def release_version(root: Path) -> str:
    package = json.loads(checked_bytes(root / "package.json", root))
    lock = json.loads(checked_bytes(root / "package-lock.json", root))
    manifest = json.loads(checked_bytes(root / "plugin/.codex-plugin/plugin.json", root))
    module = ast.parse(checked_bytes(root / "backend/codex_trace/__init__.py", root))
    backend = next((ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)), None)
    version = package["version"]
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Release version must be a safe semantic version")
    if {version, lock["version"], lock["packages"][""]["version"], manifest["version"], backend} != {version}:
        raise ValueError("Version mismatch between package, lockfile, plugin and backend")
    return version


def audit_payload(files: dict[str, bytes], root: Path) -> None:
    """Fail closed on common accidental workstation paths and credential material.

    This complements the source allowlist; it is not a general secret scanner.
    Generic executable discovery paths in code (e.g. /Applications) are portable.
    """
    # Windows 路径写入 Python/JSON 时常被反斜杠转义；仍然属于本机路径泄漏。
    roots = {value for path in (root, Path.home()) for value in (str(path), path.as_posix())}
    private_roots = {variant.encode("utf-8") for value in roots for variant in (
        value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1])}
    secret = re.compile(rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:proj-|svcacct-)[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{20,})")
    workstation = re.compile(rb"(?:/Users/[^/\s\"']+/|/home/[^/\s\"']+/|[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+[\\/])")
    for name, data in files.items():
        if any(value and value in data for value in private_roots) or workstation.search(data):
            raise ValueError(f"Developer absolute path found in release input: {name}")
        if secret.search(data):
            raise ValueError(f"Possible credential found in release input: {name}")


def collect_payload(root: Path, assets: Path) -> tuple[str, dict[str, bytes]]:
    version = release_version(root)
    locked_versions(root / "requirements.lock")
    checked_bytes(root / "backend/codex_trace/app.py", root)
    for asset in (assets / "app.js", assets / "build-info.json", *assets.glob("*.css")):
        checked_bytes(asset, assets)
    third_party = check_snapshot(root)
    files: dict[str, bytes] = {}

    def add(source: str, target: str | None = None):
        files[target or source] = checked_bytes(root / source, root)

    # Explicit top-level allowlist: reviewed user documentation only; no source fixtures,
    # generated configs, probe output, .git, .env or arbitrary JSON data.
    for name in ("install.py", "install.sh", "install.ps1", *PUBLIC_DOCUMENTS, "LICENSE", "NOTICE", "requirements.lock", "package-lock.json"):
        add(name)
    for name in SCRIPTS:
        add("scripts/" + name)
    add("plugin/.codex-plugin/plugin.json")
    for path in sorted((root / "plugin/assets").glob("*.svg")):
        add(path.relative_to(root).as_posix())
    for path in sorted((root / "backend/codex_trace").rglob("*.py")):
        if "__pycache__" not in path.parts:
            add(path.relative_to(root).as_posix(), "plugin/" + path.relative_to(root).as_posix())
    for name in ("trajectory.schema.json", "tools.json"):
        add("contracts/" + name, "plugin/contracts/" + name)
    for name in ("requirements.lock", "LICENSE", "NOTICE"):
        add(name, "plugin/" + name)
    add("third_party/manifest.json", "plugin/third-party-manifest.json")
    for relative in third_party["files"]:
        add("third_party/" + relative, "plugin/" + relative)
    files["plugin/ui/trajectory-v1.html"] = offline_html(assets).encode("utf-8")
    info = json.loads((assets / "build-info.json").read_text(encoding="utf-8"))
    if info.get("version") != version:
        raise ValueError("Frontend version mismatch; rebuild the UI")
    files["plugin/build-info.json"] = json_bytes(info)
    audit_payload(files, root)
    files["release.json"] = json_bytes({"schemaVersion": 1, "name": "codex-trace", "version": version,
        "platforms": ["darwin", "win32"], "minimumPython": "3.14", "runtimeBundled": False,
        "files": {name: digest(data) for name, data in sorted(files.items())}})
    return version, files


def write_archive(path: Path, files: dict[str, bytes], prefix: str) -> None:
    # ZIP_STORED avoids zlib-version differences; fixed timestamps and permissions
    # make two builds of the same payload byte-for-byte identical on every host.
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(prefix + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            archive.writestr(info, data)


def build_release(output: Path, *, root: Path = ROOT, assets: Path | None = None) -> dict:
    root, output = root.resolve(), output.resolve()
    version = release_version(root)
    if assets is None:
        environment = dict(os.environ, SOURCE_DATE_EPOCH=os.environ.get("SOURCE_DATE_EPOCH", "0"))
        subprocess.run(npm_command("run", "build"), cwd=root, env=environment, check=True)
        assets = root / "dist/ui"
    version, files = collect_payload(root, assets)
    output.mkdir(parents=True, exist_ok=True)
    name = f"codex-trace-{version}"
    target = output / f"{name}.zip"
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as temporary:
        archive = Path(temporary) / target.name
        write_archive(archive, files, name)
        checksum = digest(archive.read_bytes())
        # Re-running is allowed only when the versioned artifact is identical.
        if target.exists() and target.read_bytes() != archive.read_bytes():
            raise ValueError(f"Versioned archive already exists with different content: {target}. Use a new output directory or version.")
        archive.replace(target)
        checksums = Path(temporary) / "SHA256SUMS"
        checksums.write_bytes(f"{checksum}  {target.name}\n".encode("ascii"))
        checksums.replace(output / "SHA256SUMS")
    return {"archive": str(target), "sha256": checksum, "checksums": str(output / "SHA256SUMS"),
            "version": version, "fileCount": len(files)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/releases")
    parser.add_argument("--assets", type=Path, help="Use an already built frontend (normally omit)")
    args = parser.parse_args()
    try:
        print(json.dumps(build_release(args.output, assets=args.assets), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
