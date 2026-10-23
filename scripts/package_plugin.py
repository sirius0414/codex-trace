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
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def executable(value: str | Path) -> Path:
    # Do not resolve venv Python symlinks: doing so loses its isolated site-packages.
    candidate = shutil.which(str(value)) if not Path(value).is_absolute() else str(value)
    if not candidate:
        raise ValueError(f"Executable not found: {value}")
    path = Path(candidate).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Not executable: {path}")
    return path


def locked_versions(lockfile: Path) -> dict[str, str]:
    versions = {}
    for line in lockfile.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", text)
        if not match:
            raise ValueError(f"requirements.lock must use exact package==version pins: {text}")
        versions[match[1]] = match[2]
    if not versions or "mcp" not in versions:
        raise ValueError("requirements.lock must include a pinned mcp SDK")
    return versions


def verify_runtime(python: Path, versions: dict[str, str]) -> dict:
    command = """import importlib.metadata,json,sys
try:
 from compression import zstd
 available = zstd.decompress(zstd.compress(b'codex-display-runtime')) == b'codex-display-runtime'
except ImportError:
 available = False
names=json.loads(sys.argv[1])
print(json.dumps({'python':sys.version.split()[0],'versionInfo':list(sys.version_info[:3]),'zstdAvailable':available,'packages':{name:importlib.metadata.version(name) for name in names}}))
"""
    run = subprocess.run([str(python), "-c", command, json.dumps(list(versions))], capture_output=True, text=True, check=True, timeout=30)
    metadata = json.loads(run.stdout)
    if tuple(metadata.get("versionInfo", [])) < (3, 14) or metadata.get("zstdAvailable") is not True:
        raise ValueError("Python >=3.14 with compression.zstd is required for compressed Codex sources")
    mismatches = {name: {"expected": version, "actual": metadata["packages"].get(name)} for name, version in versions.items() if metadata["packages"].get(name) != version}
    if mismatches:
        raise ValueError(f"Runtime does not match requirements.lock: {json.dumps(mismatches)}")
    return metadata


def offline_html(assets: Path) -> str:
    javascript = (assets / "app.js").read_text()
    if re.search(r"\bprocess\s*\.\s*env\s*\.\s*NODE_ENV\b", javascript):
        raise ValueError("Browser UI still references process.env.NODE_ENV; configure Vite to define the production value before packaging")
    css_files = sorted(assets.glob("*.css"))
    if len(css_files) != 1:
        raise ValueError(f"Expected one bundled CSS file in {assets}, found {len(css_files)}")
    css = css_files[0].read_text()
    # Source map comments can trigger unnecessary devtools network/file requests.
    javascript = re.sub(r"\n?//# sourceMappingURL=.*", "", javascript)
    css = re.sub(r"/\*# sourceMappingURL=.*?\*/", "", css)
    if re.search(r"@import\s|url\(\s*['\"]?(?:https?:|//)", css, re.I):
        raise ValueError("UI CSS contains a remote dependency")
    javascript = re.sub(r"</script", r"<\\/script", javascript, flags=re.I)
    css = re.sub(r"</style", r"<\\/style", css, flags=re.I)
    return '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>执行轨迹</title><style>' + css + '</style></head><body><div id="root"></div><script>' + javascript + '</script></body></html>\n'


def build(output: Path, python: Path, codex: Path, assets: Path) -> dict:
    backend = ROOT / "backend" / "codex_display"
    if not (backend / "app.py").is_file():
        raise ValueError("Production backend app.py is missing")
    versions = locked_versions(ROOT / "requirements.lock")
    runtime = verify_runtime(python, versions)
    codex_version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    html = offline_html(assets)
    if output.exists():
        raise ValueError(f"Output already exists; select a new output or remove this generated package first: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-display-package-", dir=output.parent) as temporary:
        staged = Path(temporary) / "plugin"
        shutil.copytree(ROOT / "plugin", staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        shutil.copytree(backend, staged / "backend" / "codex_display", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "contracts", staged / "contracts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staged / "ui").mkdir()
        (staged / "ui" / "trajectory-v1.html").write_text(html)
        shutil.copyfile(ROOT / "requirements.lock", staged / "requirements.lock")
        shutil.copyfile(ROOT / "LICENSE", staged / "LICENSE")
        launch = {"mcpServers": {"codex-display": {
            "command": str(python), "args": ["-m", "codex_display.app"], "cwd": "./backend",
            "env": {"CODEX_DISPLAY_CODEX_EXECUTABLE": str(codex), "CODEX_DISPLAY_STATE_ROOT": str(Path.home() / ".local/share/codex-display/index"),
                    "CODEX_DISPLAY_EXPORT_ROOT": str(Path.home() / "Downloads" / "Codex Display"), "PYTHONDONTWRITEBYTECODE": "1"}
        }}}
        (staged / ".mcp.json").write_text(json.dumps(launch, ensure_ascii=False, indent=2) + "\n")
        metadata = {"schemaVersion": 1, "builtAt": datetime.now(timezone.utc).isoformat(),
                    "runtimePython": str(python), "appServerExecutable": str(codex), "appServerVersion": codex_version,
                    "runtime": runtime, "uiSha256": hashlib.sha256(html.encode()).hexdigest(),
                    "requirementsSha256": hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest(),
                    "runtimeBundled": False, "startupNetworkInstallation": False,
                    "minimumPython": "3.14", "entrypoint": "codex_display.app",
                    "limitations": ["This local package binds to an existing absolute Python runtime and Codex executable on this machine.", "Keep those paths installed. Rebuild/reinstall after relocating them; the cache copy contains UI/backend code, not the interpreter."]}
        (staged / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (staged / "INSTALL.txt").write_text("Codex Display 0.1.0 local package\n\nUI, contracts and backend code are included in this cacheable package.\nThe MCP cwd is relative to the installed package; runtime executables are absolute.\nPython >=3.14 with compression.zstd is required. No dependencies are installed at startup.\nKeep runtime.json paths installed; rebuild and reinstall after relocating them.\nOpen Execution Trace from a chat's New Tab menu. The global entry asks you to select a task.\nReload/restart Codex when convenient after an update to refresh plugin resources.\nUninstalling the plugin does not remove ZIPs in ~/Downloads/Codex Display/.\n")
        staged.rename(output)
    return {"output": str(output), "uiBytes": len(html.encode()), "python": str(python), "codex": str(codex)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "plugin")
    parser.add_argument("--python", default=str(ROOT / ".venv" / "bin" / "python"))
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
            python = runtime_dir / "bin" / "python"
            subprocess.run([str(python), "-m", "pip", "install", "--requirement", str(ROOT / "requirements.lock")], check=True)
        result = build(args.output.absolute(), python, executable(args.codex), args.assets.absolute())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Package build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
