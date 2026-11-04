#!/usr/bin/env python3
"""在隔离用户目录验收真实 Git 市场安装、更新和迁移后的 uv/MCP 导出。

输入只接受干净本地仓库或 HTTPS Git 源。所有注册、缓存和合成聊天都位于
临时目录；本地输入先克隆，再通过私有 Git URL 映射走 CLI 的真实 Git 分支。
更新探针仅修改私有副本的 SUPPORT.md 和校验清单，不改变产品版本或业务代码。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit

from platform_runtime import executable
from verify_install import verify_cache

MARKETPLACE = "codex-trace-community"
SELECTOR = "codex-trace@" + MARKETPLACE
PLUGIN_PATH = Path("plugins/codex-trace")
LOCAL_GIT_ALIAS = "https://community-smoke.invalid/repository.git"
FIXTURE_BRANCH = "codex-trace-smoke-validation"
UPDATE_MARKER = "\nCommunity marketplace synthetic update verification.\n"
UV_OPTIONS = {"UV_FIND_LINKS", "UV_NO_INDEX", "UV_OFFLINE"}


def isolated_environment(home: Path, uv_cache_dir: Path) -> dict[str, str]:
    """保留系统运行条件，清除宿主的任务、Python、Git 和 uv 配置入口。"""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("CODEX", "PYTHON", "UV_", "GIT_"))}
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "APPDATA": str(home / "AppData/Roaming"),
                "LOCALAPPDATA": str(home / "AppData/Local"),
                "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "UV_CACHE_DIR": str(uv_cache_dir),
                "UV_PYTHON_INSTALL_DIR": str(uv_cache_dir.parent / "python-managed"),
                "UV_PYTHON_PREFERENCE": "only-managed", "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    # Windows 的用户目录还可能由这两个变量重建；不能留下真实用户路径。
    if os.name == "nt":
        env.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive):])
    return env


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 180) -> subprocess.CompletedProcess:
    """失败只暴露程序名和退出码，不把凭据辅助程序的原始输出写入验收报告。"""
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed with exit code {result.returncode}")
    return result


def repository_source(value: str, ref: str | None) -> tuple[str, bool]:
    """只接受无嵌入凭据的 HTTPS 源或现有本地目录，拒绝参数和配置注入。"""
    if ref is not None and (not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref)):
        raise ValueError("invalid-git-ref")
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid-repository-source")
    path = Path(value).expanduser()
    if path.is_dir():
        return str(path.resolve()), True
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or not url.path.strip("/")
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("repository-must-be-a-local-directory-or-https-git-url")
    return value, False


def fingerprints(directory: Path) -> dict[str, str]:
    """安装器会跳过链接，因此验收必须拒绝链接、重解析点和跨平台路径碰撞。"""
    if not directory.is_dir():
        raise ValueError("missing-plugin-directory")
    files: dict[str, str] = {}
    normalized: set[str] = set()
    for path in [directory, *sorted(directory.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("plugin-must-not-contain-links")
        name = path.relative_to(directory).as_posix()
        key = unicodedata.normalize("NFC", name).casefold()
        if key in normalized:
            raise ValueError("plugin-path-collision")
        normalized.add(key)
        if stat.S_ISREG(info.st_mode):
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("plugin-must-contain-only-regular-files")
    return files


def validate_plugin(directory: Path) -> dict:
    files = fingerprints(directory)
    manifest = json.loads((directory / "package-manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("name") != "codex-trace"
            or manifest.get("distribution") != "github-repository-marketplace"
            or manifest.get("runtimeManagement") != "uv"
            or manifest.get("runtimeBundled") is not False):
        raise ValueError("invalid-community-package-metadata")
    actual = {name: digest for name, digest in files.items() if name != "package-manifest.json"}
    if manifest.get("files") != actual:
        raise ValueError("community-package-inventory-or-checksum-mismatch")
    for name in ("plugin.json", ".codex-plugin/plugin.json"):
        identity = json.loads((directory / name).read_text(encoding="utf-8"))
        if identity.get("name") != "codex-trace" or identity.get("version") != manifest.get("version"):
            raise ValueError("community-package-version-mismatch")
    expected = ["run", "--no-project", "--no-config", "--python", "3.14.2",
                "--with-requirements", "./requirements.lock", "--", "python", "-I", "-B",
                "./launch.py"]
    for name in (".mcp.json", "mcp.json"):
        launch = json.loads((directory / name).read_text(encoding="utf-8"))["mcpServers"]["codex-trace"]
        cwd = "." if name == ".mcp.json" else "${PLUGIN_ROOT}"
        if launch.get("command") != "uv" or launch.get("args") != expected or launch.get("cwd") != cwd:
            raise ValueError("invalid-community-uv-command")
    return manifest


def validate_repository(directory: Path) -> Path:
    catalog = json.loads((directory / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    entries = [entry for entry in catalog.get("plugins", []) if entry.get("name") == "codex-trace"]
    if (catalog.get("name") != MARKETPLACE or len(entries) != 1
            or entries[0].get("source") != {"source": "local", "path": "./plugins/codex-trace"}):
        raise ValueError("invalid-community-marketplace-catalog")
    directory = directory / PLUGIN_PATH
    validate_plugin(directory)
    return directory


def prepare_repository(source: str, local: bool, ref: str | None, destination: Path,
                       env: dict[str, str], git: Path) -> tuple[str, str | None, str]:
    """仅克隆已提交内容；本地源通过私有 URL 映射保持真实 Git 市场更新路径。"""
    def git_run(*args, cwd=destination.parent):
        return run([str(git), *args], cwd=cwd, env=env)
    if local:
        original = Path(source)
        if git_run("status", "--porcelain", "--untracked-files=all", cwd=original).stdout.strip():
            raise ValueError("local-repository-must-be-clean")
    git_run("clone", "--no-local", "--no-checkout", "--", source, str(destination))
    # clone 只建立默认分支的本地引用；显式 fetch 才能统一支持其它分支、tag 和 SHA。
    if ref:
        git_run("fetch", "--no-tags", "origin", ref, cwd=destination)
    git_run("checkout", "--detach", "FETCH_HEAD" if ref else "HEAD", cwd=destination)
    commit = git_run("rev-parse", "HEAD", cwd=destination).stdout.strip()
    if local:
        git_run("checkout", "-B", FIXTURE_BRANCH, cwd=destination)
        git_run("config", "--file", env["GIT_CONFIG_GLOBAL"],
                "url." + destination.as_uri() + ".insteadOf", LOCAL_GIT_ALIAS)
        return LOCAL_GIT_ALIAS, FIXTURE_BRANCH, commit
    return source, ref, commit


def commit_update_probe(repository: Path, env: dict[str, str], git: Path) -> str:
    """同版本更新只改验收文案及其 SHA，业务代码、版本和依赖均保持一致。"""
    plugin = repository / PLUGIN_PATH
    validate_plugin(plugin)
    support = plugin / "SUPPORT.md"
    support.write_bytes(support.read_bytes() + UPDATE_MARKER.encode())
    manifest_path = plugin / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["SUPPORT.md"] = hashlib.sha256(support.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run([str(git), "add", "--", str(PLUGIN_PATH / "SUPPORT.md"), str(PLUGIN_PATH / "package-manifest.json")],
        cwd=repository, env=env)
    run([str(git), "-c", "user.name=Community Smoke", "-c", "user.email=community-smoke@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(repository.parent / "empty-hooks"),
         "commit", "-m", "Verify isolated same-version marketplace refresh"], cwd=repository, env=env)
    return run([str(git), "rev-parse", "HEAD"], cwd=repository, env=env).stdout.strip()


def contained_path(value: str, parent: Path) -> Path:
    """CLI 返回的路径必须仍在私有用户目录内，后续复制与删除才能安全执行。"""
    path = Path(value)
    if not path.is_absolute() or not path.resolve().is_relative_to(parent.resolve()) or path.resolve() == parent.resolve():
        raise ValueError("cli-returned-path-outside-isolated-profile")
    return path.resolve()


def remove_tree(directory: Path) -> None:
    """Git 对象常带只读属性；Windows 清理时只解除当前私有目录中的文件属性。"""
    def retry(function, path, error):
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        function(path)
    shutil.rmtree(directory, onexc=retry)


def configure_github_auth(env: dict[str, str], root: Path, git: Path) -> None:
    """私有仓库可使用已登录 gh 的凭据接口；不复制或输出 token，不加载用户 Git 配置。"""
    gh = shutil.which("gh")
    if gh:
        config = os.environ.get("GH_CONFIG_DIR") or str(Path.home() / ".config/gh")
        env["GH_CONFIG_DIR"] = config
        helper = "!" + shlex.quote(Path(gh).absolute().as_posix()) + " auth git-credential"
        run([str(git), "config", "--file", env["GIT_CONFIG_GLOBAL"],
             "credential.https://github.com.helper", helper], cwd=root, env=env)


def smoke(repository: str, *, ref: str | None = None, codex: Path, uv: Path,
          uv_cache_dir: Path | None = None, community_environment: dict[str, str] | None = None) -> dict:
    """验证 Git 安装/更新及缓存迁移；桌面点击和跨版本升级不在此验收中。"""
    checks: list[dict] = []
    report = {"platform": sys.platform, "marketplace": MARKETPLACE,
              "installationMode": "realGitMarketplace", "isolatedUserProfile": True,
              "taskDataRead": False, "syntheticTaskDataRead": True, "hostUiVerified": False,
              "crossVersionUpgradeVerified": False, "checks": checks}
    stage = "validate_inputs"
    def check(operation, passed):
        checks.append({"operation": operation, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(operation)
    try:
        source, local = repository_source(repository, ref)
        options = community_environment or {}
        if options.keys() - UV_OPTIONS:
            raise ValueError("unsupported-community-environment-option")
        git = executable("git")
        with tempfile.TemporaryDirectory(prefix="codex-community-中文 空格-") as temporary:
            root = Path(temporary).resolve()
            home = root / "cli-home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            cache = uv_cache_dir.expanduser().resolve() if uv_cache_dir else root / "uv-cache"
            env = isolated_environment(home, cache)
            env.update(options)
            Path(env["GIT_CONFIG_GLOBAL"]).write_text("", encoding="utf-8")
            if not local:
                configure_github_auth(env, root, git)
            report.update(localSyntheticUpdate=local, uvCacheMode="explicit" if uv_cache_dir else "temporaryColdCache",
                          uvOffline=options.get("UV_OFFLINE") == "1")
            def cli(*args):
                return run([str(codex), *args], cwd=root, env=env, timeout=300)
            report["codexVersion"] = cli("--version").stdout.strip()
            report["uvVersion"] = run([str(uv), "--version"], cwd=root, env=env).stdout.strip()
            stage = "clone_repository"
            cloned = root / "source repository"
            registered, registered_ref, commit = prepare_repository(source, local, ref, cloned, env, git)
            source_plugin = validate_repository(cloned)
            source_manifest = validate_plugin(source_plugin)
            report.update(sourceCommit=commit, version=source_manifest["version"])
            check("committed_repository_inventory_verified", True)
            stage = "real_git_marketplace_add"
            args = ["plugin", "marketplace", "add", registered, "--json"]
            if registered_ref:
                args += ["--ref", registered_ref]
            added = json.loads(cli(*args).stdout)
            snapshot = contained_path(added["installedRoot"], codex_home)
            check("real_git_marketplace_snapshot", added["marketplaceName"] == MARKETPLACE and snapshot != cloned)
            validate_repository(snapshot)
            stage = "plugin_add"
            added_plugin = json.loads(cli("plugin", "add", SELECTOR, "--json").stdout)
            installed = contained_path(added_plugin["installedPath"], codex_home / "plugins/cache")
            check("installed_cache_matches_git_snapshot", fingerprints(installed) == fingerprints(snapshot / PLUGIN_PATH))
            check("installed_expected_source_revision", fingerprints(installed) == fingerprints(source_plugin))
            launch = json.loads((installed / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["codex-trace"]
            stage = "resolved_uv_configuration"
            configured = json.loads(cli("mcp", "list", "--json").stdout)
            server = next((item for item in configured if item.get("name") == "codex-trace"), {})
            transport = server.get("transport", {})
            check("cli_resolves_uv_plugin_cwd", transport.get("command") == "uv"
                  and transport.get("args") == launch["args"]
                  and Path(transport.get("cwd", "")).is_absolute()
                  and Path(transport["cwd"]).resolve() == installed)
            stage = "marketplace_upgrade"
            before_update = fingerprints(installed)
            # 私有缓存污染探针证明 plugin add 真正替换目录；同一远端 commit 也可验收。
            (installed / ".smoke-cache-refresh").write_text("synthetic stale cache file", encoding="utf-8")
            if local:
                report["syntheticUpdateCommit"] = commit_update_probe(cloned, env, git)
            upgraded = json.loads(cli("plugin", "marketplace", "upgrade", MARKETPLACE, "--json").stdout)
            check("marketplace_upgrade_succeeded", not upgraded.get("errors")
                  and MARKETPLACE in upgraded.get("selectedMarketplaces", [])
                  and str(snapshot) in upgraded.get("upgradedRoots", []))
            updated_source = validate_repository(snapshot)
            check("marketplace_snapshot_refresh_verified", not local
                  or (updated_source / "SUPPORT.md").read_text(encoding="utf-8").endswith(UPDATE_MARKER))
            stage = "plugin_reinstall_after_upgrade"
            readded = json.loads(cli("plugin", "add", SELECTOR, "--json").stdout)
            installed = contained_path(readded["installedPath"], codex_home / "plugins/cache")
            after_update = fingerprints(installed)
            check("plugin_reinstall_refreshes_cache", not (installed / ".smoke-cache-refresh").exists()
                  and after_update == fingerprints(updated_source))
            check("same_version_update_propagated", not local
                  or (before_update != after_update and readded["version"] == added_plugin["version"]))
            report.update(sameVersionUpdateVerified=local,
                          updatePayloadChanged=before_update != after_update,
                          updateVerification="syntheticSameVersionCommit" if local else "remoteRefreshAndReinstall")
            stage = "relocate_cache_and_remove_sources"
            relocated = root / "moved 中文 plugin"
            shutil.copytree(installed, relocated)
            remove_tree(cloned)
            remove_tree(snapshot)
            remove_tree(codex_home / "plugins/cache")
            check("source_and_original_cache_removed", not cloned.exists() and not snapshot.exists() and not installed.exists())
            before_runtime = fingerprints(relocated)
            stage = "uv_runtime_and_synthetic_mcp_export"
            report.update(verify_cache(relocated, root / "verification", checks, community=True,
                                      uv_executable=uv, uv_cache_dir=cache, community_environment=options))
            check("relocated_package_files_unchanged", fingerprints(relocated) == before_runtime)
    except Exception as error:
        checks.append({"operation": stage, "passed": False, "errorType": type(error).__name__})
        report["failureStage"] = stage
        # 不记录异常原文：JSON、HTTP 和凭据工具的错误可能包含外部输入。
    report["passed"] = bool(checks) and all(item["passed"] for item in checks)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--uv-cache-dir", type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    try:
        result = smoke(args.repository, ref=args.ref, codex=executable(args.codex), uv=executable(args.uv),
                       uv_cache_dir=args.uv_cache_dir)
    except (OSError, ValueError) as error:
        result = {"passed": False, "failureStage": "resolve_executables", "errorType": type(error).__name__}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
