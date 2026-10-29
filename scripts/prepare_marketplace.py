#!/usr/bin/env python3
"""Prepare a reviewable local marketplace; print commands but never register it."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys


def display_command(arguments):
    """输出可粘贴的本机 shell 命令；Windows PowerShell 路径需显式调用运算符。"""
    if sys.platform == "win32":
        return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
    return shlex.join(arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    package = args.package.resolve()
    target = args.output.absolute()
    manifest = json.loads((package / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest["name"] != "codex-display" or not (package / ".mcp.json").is_file():
        parser.error("A built codex-display package is required")
    if target.exists():
        parser.error("Output already exists; choose a new generated directory")
    # 市场使用独立副本，便于在注册前审阅；生成过程不依赖已安装缓存。
    destination = target / "plugins/codex-display"
    destination.parent.mkdir(parents=True)
    shutil.copytree(package, destination)
    catalog = target / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"name": "codex-display-local", "interface": {"displayName": "Codex Display Local"},
        "plugins": [{"name": "codex-display", "source": {"source": "local", "path": "./plugins/codex-display"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Developer Tools"}]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"marketplace": str(target), "registered": False, "pluginVersion": manifest["version"],
        "commands": [display_command([args.codex, "plugin", "marketplace", "add", str(target)]),
                     display_command([args.codex, "plugin", "add", "codex-display@codex-display-local", "--json"])]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
