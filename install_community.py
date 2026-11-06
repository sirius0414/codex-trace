#!/usr/bin/env python3
"""Install a released Codex Trace community plugin and prepare it before use.

Standalone, standard-library-only entry point: it can be downloaded and run by
uv without a checkout. The ZIP installer also uses its read-only MCP probe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

VERSION = "0.2.2"
PYTHON_VERSION = "3.14.2"
REPOSITORY = "https://github.com/sirius0414/codex-trace.git"
MARKET = "codex-trace-community"
SELECTOR = "codex-trace@" + MARKET


def executable(value):
    path = Path(shutil.which(str(value)) or str(value)).expanduser().absolute()
    if sys.platform == "win32" and path.suffix.lower() in {".cmd", ".bat"}:
        if path.stem.lower() != "codex":
            raise ValueError("Specify a native executable, not a batch wrapper")
        architecture = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        triple = "aarch64-pc-windows-msvc" if architecture == "arm64" else "x86_64-pc-windows-msvc"
        for modules in (path.parent / "node_modules", path.parent.parent):
            main = modules / "@openai/codex"
            for package in (modules / f"@openai/codex-win32-{architecture}",
                            main / f"node_modules/@openai/codex-win32-{architecture}", main):
                for part in ("bin", "codex"):
                    native = package / f"vendor/{triple}/{part}/codex.exe"
                    if native.is_file():
                        return native
        raise ValueError("Cannot find native codex.exe; specify --codex with its absolute path")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"Required executable not found: {value}")
    return path


def runtime_environment(environment=None):
    """Use the host's default uv locations, not terminal-only cache overrides."""
    source = os.environ if environment is None else environment
    result = {key: value for key, value in source.items()
              if not key.upper().startswith(("UV_", "PYTHON", "XDG_"))
              and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}
    result.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return result


def run(command, cwd, env, timeout=300):
    result = subprocess.run([str(value) for value in command], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        # Network/CLI output can contain credentials; do not echo it into reports.
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode}); check network, PATH and permissions, then rerun the installer")
    return result.stdout


def verify_mcp(command, cwd, env, version, tool_names, timeout=25):
    """Check actual startup and bundled UI without calling any business tool."""
    incoming = queue.Queue()
    process = subprocess.Popen([str(value) for value in command], cwd=cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)

    def read():
        try:
            for line in process.stdout:
                incoming.put(json.loads(line))
        except (ValueError, OSError):
            incoming.put({"invalid": True})
        finally:
            incoming.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    sequence = 0

    def send(message):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        process.stdin.flush()

    def request(method, params):
        nonlocal sequence
        sequence += 1
        send({"id": sequence, "method": method, "params": params})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP startup check timed out")
            try:
                message = incoming.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("MCP startup check timed out") from None
            if not isinstance(message, dict) or message.get("invalid"):
                raise RuntimeError("MCP exited or returned an invalid startup response")
            if "method" in message and "id" in message:
                send({"id": message["id"], "error": {"code": -32601, "message": "Read-only installation check"}})
            elif message.get("id") == sequence:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise RuntimeError("MCP startup request failed")
                return message["result"]

    try:
        identity = request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "codex-trace-installer", "version": version}}).get("serverInfo", {})
        if identity.get("name") != "codex-trace" or identity.get("version") != version:
            raise ValueError("Started MCP identity/version differs from the installed package")
        send({"method": "notifications/initialized", "params": {}})
        tools = request("tools/list", {}).get("tools", [])
        if {item.get("name") for item in tools} != set(tool_names):
            raise ValueError("MCP tool inventory differs from the installed package")
        resources = request("resources/list", {}).get("resources", [])
        uris = {item.get("uri") for item in resources}
        if uris != {"ui://codex-trace/trajectory-v1.html", "ui://codex-trace/trajectory-panel-v1.html"}:
            raise ValueError("MCP UI resources are incomplete")
        for uri in sorted(uris):
            contents = request("resources/read", {"uri": uri}).get("contents", [])
            if not contents or not any('id="root"' in item.get("text", "") for item in contents):
                raise ValueError("MCP UI content is missing")
        return {"passed": True, "version": version, "toolCount": len(tools),
                "uiResourceCount": len(uris), "businessToolsCalled": False}
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdout.close()


def verify_package(package, codex_home, *, marketplace=False):
    package = Path(package).absolute()
    boundaries = ([codex_home / ".tmp/marketplaces", codex_home / "plugins/marketplaces"]
                  if marketplace else [codex_home / "plugins/cache"])
    boundary = next((root for root in boundaries if package.is_relative_to(root)), boundaries[0])
    if (not package.is_relative_to(boundary) or package == boundary
            or not package.resolve().is_relative_to(boundary.resolve())):
        raise ValueError("Codex returned a package path outside its plugin directory")
    for path in (package, *package.parents):
        if path == codex_home:
            break
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Plugin directory contains a link or reparse point")
    files = {}
    for path in package.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Plugin contains a link or reparse point")
        if stat.S_ISREG(info.st_mode):
            files[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("Plugin contains a special file")
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    files.pop("package-manifest.json", None)
    if (manifest.get("name") != "codex-trace" or manifest.get("version") != VERSION
            or manifest.get("files") != files):
        raise ValueError("Plugin version or file inventory differs from this installer")
    return package, manifest


def install(*, repository=REPOSITORY, ref=None, codex=None, uv=None, environment=None):
    url = urlsplit(repository)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(ord(c) < 32 for c in repository)):
        raise ValueError("Repository must be an HTTPS Git URL without credentials")
    ref = ref or "v" + VERSION
    if ref.startswith("-") or any(ord(c) < 32 for c in ref):
        raise ValueError("Invalid Git ref")
    env = runtime_environment(environment)
    codex, uv = executable(codex or "codex"), executable(uv or "uv")
    executable("git")
    codex_home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="codex-trace-install-") as temp:
        cwd = Path(temp)
        print("Registering Codex Trace " + VERSION + "...", file=sys.stderr, flush=True)
        added = json.loads(run([codex, "plugin", "marketplace", "add", repository, "--ref", ref, "--json"], cwd, env))
        if added.get("marketplaceName") != MARKET:
            raise ValueError("Unexpected marketplace identity")
        source, manifest = verify_package(Path(added["installedRoot"]) / "plugins/codex-trace", codex_home,
                                          marketplace=True)

        def launch_args(package):
            return ["run", "--no-project", "--no-config", "--python", PYTHON_VERSION,
                    "--with-requirements", str(package / "requirements.lock"), "--", "python", "-I", "-B"]

        print("Preparing Python and locked dependencies (first install may take a few minutes)...", file=sys.stderr, flush=True)
        run([uv, *launch_args(source), "-c", "import compression.zstd, mcp"], cwd, env, timeout=900)
        result = json.loads(run([codex, "plugin", "add", SELECTOR, "--json"], cwd, env))
        installed, installed_manifest = verify_package(result["installedPath"], codex_home)
        if installed_manifest != manifest:
            raise ValueError("Installed plugin differs from the prepared marketplace snapshot")
        # Check the real resolved transport, including bare uv discovery in PATH.
        configured = json.loads(run([codex, "mcp", "get", "codex-trace", "--json"], cwd, env))
        transport = configured.get("transport", {})
        expected = [*launch_args(installed), str(installed / "launch.py")]
        if (not configured.get("enabled") or transport.get("command") != "uv"
                or transport.get("args") != expected or Path(transport.get("cwd", "")).resolve() != installed):
            raise ValueError("Codex resolved a different plugin; disable other Codex Trace copies and rerun")
        print("Checking installed MCP startup and UI resources without downloading...", file=sys.stderr, flush=True)
        contracts = json.loads((installed / "contracts/tools.json").read_text(encoding="utf-8"))["tools"]
        names = set(contracts) | {"open_codex_trace_panel"}
        ready = verify_mcp([uv, *expected], cwd, {**env, "UV_OFFLINE": "1"}, VERSION, names)
        verify_package(installed, codex_home)
        return {"installed": True, "ready": True, "version": VERSION, "installedPath": str(installed),
                "marketplace": MARKET, "ref": ref, "dependencyPreparation": "completedDuringInstallation",
                "startupCheck": ready, "nextStep": "Open New Tab > Codex Trace. If Codex was already running, start a new chat or restart it to load the installed plugin."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--ref", help="Maintainer/test override; defaults to this installer's release tag")
    parser.add_argument("--codex")
    parser.add_argument("--uv")
    args = parser.parse_args()
    try:
        if sys.platform not in {"darwin", "win32"}:
            raise ValueError("Supported platforms: macOS and native Windows")
        print(json.dumps(install(**vars(args)), ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(f"Codex Trace installation is not ready: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Installation interrupted; rerun the same installer to finish.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
