#!/usr/bin/env python3
"""使用已有运行环境准备、验证并安装本地插件更新。

prepare 只生成独立产物和内容指纹；install 才修改市场注册及插件缓存。
二者以 update-plan.json 衔接，安装前重新核对目标 CODEX_HOME 和产物内容。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from package_plugin import executable, locked_versions, verify_runtime
from platform_runtime import IS_WINDOWS, npm_command, state_root, venv_python

ROOT = Path(__file__).resolve().parents[1]
MARKET = "codex-trace-local"
PLUGIN = "codex-trace"
SELECTOR = f"{PLUGIN}@{MARKET}"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().absolute()


def run(command: list[str], *, log_path: Path | None = None) -> str:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=300, encoding="utf-8")
    if log_path:
        log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        # Do not dump JSON configuration on errors. Successful stderr warnings
        # are not installation failures.
        detail = f"完整日志：{log_path}" if log_path else result.stderr.strip()
        raise RuntimeError(f"命令失败（{result.returncode}）：{command[0]} {' '.join(command[1:3])}\n{detail}")
    return result.stdout


def cli_json(codex: Path, *arguments: str):
    return json.loads(run([str(codex), *arguments]))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprints(directory: Path) -> dict[str, str]:
    """按相对路径核对包内容，忽略运行产生的字节码并拒绝符号链接。"""
    result = {}
    for path in [directory, *sorted(directory.rglob("*"))]:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT 包括 junction
            raise ValueError(f"安装包不应包含符号链接或重解析点：{path}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError(f"安装包为空：{directory}")
    return result


def runtime_paths(python_arg: str | None, codex_arg: str | None) -> tuple[Path, Path]:
    """显式参数优先，其次复用安装回执，最后检查约定路径中的已有环境。"""
    installed = codex_home() / "plugins/cache" / MARKET / PLUGIN
    records = []
    for path in sorted(installed.glob("*/runtime.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    version = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]

    def choose(explicit, candidates, label):
        if explicit:
            return executable(Path(explicit).expanduser() if any(mark in explicit for mark in ("/", "\\", "~")) else explicit)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return executable(candidate)
            except ValueError:
                continue
        raise ValueError(f"找不到{label}，请使用对应的 --python / --codex 参数指定已有环境。")

    python = choose(python_arg, [*(record.get("runtimePython") for record in records),
        venv_python(state_root().parent / f"runtime/{version}"), venv_python(ROOT / ".venv")], "Python 运行环境")
    desktop_candidates = [] if IS_WINDOWS else [
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "/Applications/Codex.app/Contents/Resources/codex"]
    codex = choose(codex_arg, [*(record.get("appServerExecutable") for record in records),
        *desktop_candidates, shutil.which("codex")], "Codex 可执行文件")
    return python, codex


def prepare(python: Path, codex: Path) -> Path:
    """生成可供审阅的包、验证报告和安装计划，不改当前安装。"""
    verify_runtime(python, locked_versions(ROOT / "requirements.lock"))
    output_parent = ROOT / "build"
    output_parent.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix=f"update-{datetime.now():%Y%m%d-%H%M%S}-", dir=output_parent))
    print(f"更新目录：{output}", flush=True)
    print("[1/5] 构建前端与检查契约", flush=True)
    run(npm_command("run", "build"), log_path=output / "build.log")
    run([str(python), str(ROOT / "contracts/generate_types.py"), "--check"])
    print("[2/5] 打包并验证安装包", flush=True)
    package = output / "plugin"
    run([str(python), str(ROOT / "scripts/package_plugin.py"), "--output", str(package),
         "--python", str(python), "--codex", str(codex)])
    run([str(python), str(ROOT / "scripts/verify_install.py"), "--package", str(package),
         "--cache-copy-only", "--out", str(output / "verification.json")])
    verification = json.loads((output / "verification.json").read_text(encoding="utf-8"))
    if verification.get("passed") is not True:
        raise ValueError("安装包验证未通过。")
    print("[3/5] 生成本地市场", flush=True)
    run([str(python), str(ROOT / "scripts/prepare_marketplace.py"), "--package", str(package),
         "--output", str(output / "marketplace"), "--codex", str(codex)])
    # 将包、市场目录和目标配置一起冻结；分开执行安装时不能悄悄换用另一份产物。
    write_json(output / "update-plan.json", {"schemaVersion": 1, "platform": sys.platform, "codexHome": str(codex_home()),
        "python": str(python), "codex": str(codex), "files": fingerprints(package),
        "catalogSha256": hashlib.sha256((output / "marketplace/.agents/plugins/marketplace.json").read_bytes()).hexdigest()})
    return output


def install(output: Path) -> dict:
    """仅安装内容仍与计划一致的已验证产物；失败时尝试恢复市场注册。"""
    output = output.resolve()
    plan = json.loads((output / "update-plan.json").read_text(encoding="utf-8"))
    if plan.get("schemaVersion") != 1 or Path(plan["codexHome"]).resolve() != codex_home().resolve():
        raise ValueError("更新计划版本或 CODEX_HOME 不一致，请重新准备安装包。")
    if plan.get("platform", sys.platform) != sys.platform:
        raise ValueError("更新计划绑定其他操作系统，请在本机重新准备安装包。")
    python, codex = executable(plan["python"]), executable(plan["codex"])
    package, marketplace = output / "plugin", output / "marketplace"
    expected = plan["files"]
    if fingerprints(package) != expected or fingerprints(marketplace / "plugins" / PLUGIN) != expected:
        raise ValueError("已验证的安装包或市场副本发生变化，请重新构建。")
    if hashlib.sha256((marketplace / ".agents/plugins/marketplace.json").read_bytes()).hexdigest() != plan["catalogSha256"]:
        raise ValueError("已准备的市场目录发生变化，请重新构建。")
    if json.loads((output / "verification.json").read_text(encoding="utf-8")).get("passed") is not True:
        raise ValueError("缺少已通过的安装包验证结果。")
    verify_runtime(python, locked_versions(package / "requirements.lock"))
    manifest = json.loads((package / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN:
        raise ValueError("只能更新 codex-trace。")
    # 使用已验证包中的编号，分步安装时当前源码或 dist 可能已是另一份构建。
    runtime_path = package / "runtime.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.is_file() else {}
    build_id = (runtime.get("frontendBuild") or {}).get("buildId")
    previous = next((entry for entry in cli_json(codex, "plugin", "marketplace", "list", "--json")["marketplaces"]
                     if entry["name"] == MARKET), None)
    if previous and previous.get("marketplaceSource", {}).get("sourceType") != "local":
        raise ValueError("同名市场不是本地来源，不能自动替换。")
    old_root = Path(previous["root"]) if previous else None
    replacing = old_root is None or old_root.resolve() != marketplace.resolve()
    removed = added = False
    write_json(output / "previous-marketplace.json", previous)
    print("[4/5] 注册新市场并安装插件", flush=True)
    try:
        if replacing:
            # CLI 不允许同名市场直接覆盖；先记录旧地址，出错后才能恢复注册。
            if old_root:
                run([str(codex), "plugin", "marketplace", "remove", MARKET])
                removed = True
            run([str(codex), "plugin", "marketplace", "add", str(marketplace)])
            added = True
        installed = cli_json(codex, "plugin", "add", SELECTOR, "--json")
        cache = Path(installed["installedPath"]).resolve()
        expected_cache = (codex_home() / "plugins/cache" / MARKET / PLUGIN / manifest["version"]).resolve()
        if installed.get("pluginId") != SELECTOR or installed.get("version") != manifest["version"] or cache != expected_cache:
            raise ValueError("安装回执与目标插件不一致。")
        print("[5/5] 核对缓存文件与 MCP 启动配置", flush=True)
        # CLI 返回成功尚不足以确认更新：缓存字节和实际 MCP 启动配置都必须匹配。
        if fingerprints(cache) != expected:
            raise ValueError("安装缓存与本次构建不一致。")
        server = cli_json(codex, "mcp", "get", PLUGIN, "--json")
        transport = server.get("transport", {})
        launch = json.loads((package / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][PLUGIN]
        if (server.get("name") != PLUGIN or server.get("enabled") is not True or Path(transport.get("cwd") or "").resolve() != cache / "backend"
                or transport.get("command") != launch["command"] or transport.get("args") != launch["args"]
                or any(transport.get("env", {}).get(key) != value for key, value in launch["env"].items())):
            raise ValueError("MCP 未启用或启动配置未指向本次安装。")
        report = {"passed": True, "pluginId": SELECTOR, "version": manifest["version"], "buildId": build_id,
            "installedPath": str(cache), "marketplace": str(marketplace), "verifiedFileCount": len(expected),
            "uiSha256": expected["ui/trajectory-v1.html"], "mcpCwdVerified": True,
            "hostUiReloaded": False}
        write_json(output / "installed.json", report)
        return report
    except (Exception, KeyboardInterrupt):
        # 市场注册可以补偿，但缓存可能已被 CLI 改写，报告不能声称完整回滚。
        restored, rollback_error = False, None
        if replacing and (removed or added):
            try:
                if added:
                    run([str(codex), "plugin", "marketplace", "remove", MARKET])
                if removed and old_root:
                    run([str(codex), "plugin", "marketplace", "add", str(old_root)])
                restored = True
            except Exception as error:
                rollback_error = str(error)
        write_json(output / "install-failed.json", {"passed": False, "marketplaceRegistrationRestored": restored,
            "rollbackError": rollback_error, "cacheRestored": False})
        print("安装未确认成功；" + ("原市场注册已恢复。" if restored else "请查看 install-failed.json。")
              + "插件缓存未自动回滚，修复后可重新运行更新。", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help="已准备的 Python 环境，默认复用已安装插件的环境")
    parser.add_argument("--codex", help="Codex 可执行文件，默认复用已安装插件的路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="只构建、验证和生成市场，不修改安装")
    mode.add_argument("--install-prepared", type=Path, metavar="DIR", help="安装此前准备且内容未变的更新目录")
    args = parser.parse_args()
    if args.install_prepared and (args.python or args.codex):
        parser.error("--install-prepared 使用已验证计划中的可执行文件，不能同时覆盖 --python / --codex")
    try:
        output = args.install_prepared or prepare(*runtime_paths(args.python, args.codex))
        if args.prepare_only:
            print(f"准备完成；安装命令：python3 scripts/update_plugin.py --install-prepared {output}")
            return 0
        result = install(output)
        print(f"更新完成：{result['installedPath']}\n构建编号：{result['buildId'] or '未知（安装包未记录）'}\n校验通过：{result['verifiedFileCount']} 个文件\n安装报告：{output / 'installed.json'}")
        print("请重新加载插件资源；若仍显示旧界面，请在方便时重启 Codex。")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"更新失败：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("更新已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
