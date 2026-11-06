"""Verify an installed community plugin through the real Codex MCP supervisor.

This helper does not install plugins or create a Codex thread. It prepares the
locked dependencies in the isolated profile's default uv directories, then asks
app-server to enumerate MCP tools/resources. The caller owns the temporary root
and installed cache, and is responsible for removing them after verification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from verify_install import RpcClient


def _isolated_path(value: str | Path, root: Path) -> Path:
    """Reject escapes and links before creating or reading profile directories."""
    path = Path(value)
    if not path.is_absolute() or path == root or not path.is_relative_to(root):
        raise ValueError("host-path-must-be-inside-temporary-root")
    for part in (path, *path.parents):
        if part == root:
            break
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("host-profile-must-not-use-links")
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("host-path-must-be-inside-temporary-root")
    return resolved


def _host_environment(root: Path, environment: dict[str, str], codex: Path, uv: Path) -> dict[str, str]:
    """Never pass the caller's auth, API, Python, Git, UV_* or XDG_* settings."""
    supplied = {key.upper(): value for key, value in environment.items()}
    if not supplied.get("HOME") or not supplied.get("CODEX_HOME"):
        raise ValueError("host-requires-isolated-home-and-codex-home")
    home = _isolated_path(supplied["HOME"], root)
    profile = _isolated_path(supplied.get("USERPROFILE", str(home)), root)
    if profile != home:
        raise ValueError("host-home-and-userprofile-must-match")
    paths = {"HOME": home, "USERPROFILE": profile,
             "CODEX_HOME": _isolated_path(supplied["CODEX_HOME"], root),
             "APPDATA": _isolated_path(supplied.get("APPDATA", str(home / "AppData/Roaming")), root),
             "LOCALAPPDATA": _isolated_path(supplied.get("LOCALAPPDATA", str(home / "AppData/Local")), root)}
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
               "LC_CTYPE", "TZ", "TERM", "SHELL"}
    result = {key: value for key, value in supplied.items() if key in allowed}
    result.update({key: str(path) for key, path in paths.items()})
    # The manifest invokes bare uv, and the plugin discovers Codex from PATH.
    # Use the supplied real programs without creating forwarding wrappers.
    result["PATH"] = os.pathsep.join([str(uv.parent), str(codex.parent), result.get("PATH", "")]).rstrip(os.pathsep)
    resolved_uv = shutil.which("uv", path=result["PATH"])
    if not resolved_uv or not Path(resolved_uv).samefile(uv):
        raise ValueError("host-path-must-resolve-the-supplied-uv")
    temporary = _isolated_path(root / "host-process-tmp", root)
    temporary.mkdir(parents=True, exist_ok=True)
    result.update({key: str(temporary) for key in ("TMPDIR", "TMP", "TEMP")})
    if os.name == "nt":
        result.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):])
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return result


def _fingerprints(directory: Path) -> dict[str, str]:
    """Reject links/special files and include every file in the integrity check."""
    files = {}
    for current, directories, names in os.walk(directory, followlinks=False):
        for path in [Path(current), *(Path(current) / name for name in directories + names)]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("host-installed-package-must-not-contain-links")
            if stat.S_ISREG(info.st_mode):
                files[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("host-installed-package-must-contain-only-regular-files")
    return files


def _run(command: list[str], cwd: Path, environment: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        # Never propagate arbitrary child output into a shareable smoke report.
        raise RuntimeError(f"{Path(command[0]).name} failed with exit code {result.returncode}")
    return result


def verify_host_startup(installed: Path, root: Path, codex: Path, uv: Path,
                        environment: dict, checks: list, *, prepare_runtime: bool = True) -> dict:
    """Gate release on real supervisor startup using only the caller's private cache.

    Appends ``host_*`` checks and raises on failure. The returned report describes
    a prewarmed host startup on this platform, not a cold host startup, desktop UI
    check, business-tool call, or validation on another operating system.
    """
    stage = "host_validate_isolation"

    def check(operation: str, passed: bool):
        checks.append({"operation": operation, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(operation)

    try:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("host-requires-existing-absolute-temporary-root")
        root = root.resolve()
        if root == Path(root.anchor):
            raise ValueError("host-filesystem-root-is-not-a-temporary-profile")
        for program in (codex, uv):
            if not program.is_absolute() or not program.is_file() or not os.access(program, os.X_OK):
                raise ValueError("host-requires-absolute-executable-paths")
        env = _host_environment(root, environment, codex, uv)
        home, codex_home = Path(env["HOME"]), Path(env["CODEX_HOME"])
        installed = _isolated_path(installed, root)
        cache_root = codex_home / "plugins/cache"
        check("host_uses_isolated_installed_cache", installed.is_dir()
              and installed != cache_root and installed.is_relative_to(cache_root))
        before = _fingerprints(installed)
        identity = json.loads((installed / "plugin.json").read_text(encoding="utf-8"))
        check("host_plugin_identity_is_valid", identity.get("name") == "codex-trace"
              and isinstance(identity.get("version"), str) and bool(identity["version"]))
        _isolated_path(codex_home / "config.toml", root)
        absent = [codex_home / name for name in ("auth.json", "sessions", "archived_sessions")]
        absent += [home / "Downloads/Codex Trace", home / ".local/share/codex-trace",
                   Path(env["LOCALAPPDATA"]) / "codex-trace"]
        check("host_profile_has_no_auth_task_or_business_data", not any(path.exists() for path in absent))

        launch = json.loads((installed / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["codex-trace"]
        expected_args = ["run", "--no-project", "--no-config", "--python", "3.14.2",
                         "--with-requirements", "${PLUGIN_ROOT}/requirements.lock", "--", "python",
                         "-I", "-B", "${PLUGIN_ROOT}/launch.py"]
        check("host_uses_portable_modern_launch", launch.get("command") == "uv"
              and launch.get("type") == "stdio" and launch.get("args") == expected_args
              and launch.get("cwd") == "${PLUGIN_ROOT}"
              and launch.get("env", {}) == {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
        args = [argument.replace("${PLUGIN_ROOT}", str(installed)) for argument in launch["args"]]
        check("host_launch_inputs_exist", (installed / "launch.py").is_file()
              and (installed / "requirements.lock").is_file())
        workspace = Path(tempfile.mkdtemp(prefix="host-empty-workspace-", dir=root))
        stage = "host_prepare_default_uv_directories"
        uv_cache = _isolated_path(_run([str(uv), "--no-config", "cache", "dir"], workspace, env).stdout.strip(), root)
        uv_python = _isolated_path(_run([str(uv), "--no-config", "python", "dir"], workspace, env).stdout.strip(), root)
        for directory in (uv_cache, uv_python):
            directory.mkdir(parents=True, exist_ok=True)
        check("host_default_uv_directories_are_isolated", True)

        report = {"verification": "realCodexAppServerStartsInstalledMcp", "platform": sys.platform,
                  "runtimePreparation": "prewarmedIsolatedHomeDefaultUvDirectories" if prepare_runtime else "completedByInstaller",
                  "coldStartVerified": False, "hostUiVerified": False,
                  "otherPlatformsVerified": False, "toolCallsMade": False, "threadCreated": False,
                  "taskDataRead": False, "syntheticTaskDataRead": False,
                  "realUserConfigurationRead": False, "realAuthenticationRead": False,
                  "isolatedUserProfile": True, "manifestPatched": False, "uvWrapperUsed": False,
                  "pluginVersion": identity["version"],
                  "uvCacheRelativeToRoot": uv_cache.relative_to(root).as_posix(),
                  "uvPythonRelativeToRoot": uv_python.relative_to(root).as_posix(),
                  "hostRequests": ["initialize", "initialized", "mcpServerStatus/list"]}
        stage = "host_prewarm_locked_dependencies"
        started = time.monotonic()
        if prepare_runtime:
            _run([str(uv), *args[:-1], "-c", "import compression.zstd, mcp"], workspace, env, timeout=600)
        else:
            # No preparation here: validate the installer's environment without network.
            _run([str(uv), *args[:-1], "-c", "import compression.zstd, mcp"], workspace,
                 {**env, "UV_OFFLINE": "1"}, timeout=30)
        report["dependencyPreparationSeconds"] = round(time.monotonic() - started, 3)
        check("host_locked_dependencies_prepared_in_default_directories", True)
        stage = "host_validate_registered_configuration"
        configured = json.loads(_run([str(codex), "mcp", "list", "--json"], workspace, env).stdout)
        check("host_only_expected_mcp_server_is_configured", isinstance(configured, list)
              and len(configured) == 1 and configured[0].get("name") == "codex-trace")
        transport = configured[0].get("transport", {})
        check("host_cli_resolves_modern_plugin_paths", transport.get("command") == "uv"
              and transport.get("args") == args and Path(transport.get("cwd", "")).is_absolute()
              and Path(transport["cwd"]).resolve() == installed)
        check("host_starts_outside_plugin_from_empty_directory", not any(workspace.iterdir()))

        stage = "host_start_real_app_server"
        started = time.monotonic()
        client = RpcClient([str(codex), "app-server"], workspace, env)
        try:
            initialized = client.request("initialize", {
                "clientInfo": {"name": "codex-trace-release-check", "version": identity["version"]},
                "capabilities": {"experimentalApi": True}}, timeout=30)
            client.send("initialized", {})
            check("host_app_server_uses_isolated_codex_home", bool(initialized)
                  and Path(initialized.get("codexHome", "")).resolve() == codex_home)
            stage = "host_enumerate_plugin_mcp"
            response = client.request("mcpServerStatus/list", {"cursor": None, "limit": 10}, timeout=90)
            matches = [item for item in response.get("data", []) if item.get("name") == "codex-trace"]
            check("host_discovered_installed_plugin", len(matches) == 1
                  and matches[0].get("pluginId") == "codex-trace@codex-trace-community")
            status = matches[0]
            server_info = status.get("serverInfo") or {}
            check("host_received_mcp_server_identity", server_info.get("name") == "codex-trace"
                  and server_info.get("version") == identity["version"])
            tools = status.get("tools") or {}
            expected_tools = sorted(set(json.loads((installed / "contracts/tools.json").read_text(encoding="utf-8"))["tools"])
                                    | {"open_codex_trace_panel"})
            check("host_received_all_ten_plugin_tools", isinstance(tools, dict)
                  and sorted(tools) == expected_tools and len(tools) == 10)
            resources = sorted(item.get("uri", "") for item in (status.get("resources") or []))
            check("host_received_both_ui_resources", resources == ["ui://codex-trace/trajectory-panel-v1.html",
                                                                   "ui://codex-trace/trajectory-v1.html"])
            report.update(toolNames=sorted(tools), resourceUris=resources,
                          hostServerStatus={key: status[key] for key in ("name", "pluginId", "serverInfo", "runtimeStatus") if key in status})
        finally:
            client.close()
            report.update(appServerSeconds=round(time.monotonic() - started, 3), appServerStderrLines=client.stderr_lines)

        stage = "host_verify_post_startup_integrity"
        after = _fingerprints(installed)
        report["installedFileChanges"] = {"added": sorted(after.keys() - before.keys()),
                                          "removed": sorted(before.keys() - after.keys()),
                                          "changed": sorted(name for name in before.keys() & after.keys() if before[name] != after[name])}
        check("host_installed_package_files_remain_unchanged", after == before)
        check("host_created_no_auth_task_or_business_data", not any(path.exists() for path in absent))
        # Recheck containment in case preparation changed a path component.
        for path in (home, codex_home, uv_cache, uv_python):
            _isolated_path(path, root)
        report["passed"] = True
        return report
    except Exception as error:
        checks.append({"operation": stage, "passed": False, "errorType": type(error).__name__})
        raise
