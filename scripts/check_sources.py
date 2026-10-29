#!/usr/bin/env python3
"""M0: inspect explicitly selected thread identities and physical header metadata.

Reports contain no task text, file paths, commands, or instruction bodies.
The independent app-server may update its own runtime cache; no task is resumed.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from codex_display.server import verify_thread
from platform_runtime import executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--thread-id", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.codex = executable(args.codex)
    os.environ["CODEX_DISPLAY_CODEX_EXECUTABLE"] = str(args.codex.absolute())
    checks = []
    for thread_id in args.thread_id:
        try:
            result = verify_thread(thread_id)
            # 即使身份验证成功，诊断报告也不收集可能含任务正文的标题。
            checks.append({key: value for key, value in result.items() if key != "title"})
        except Exception as error:
            checks.append({"threadId": thread_id, "identityMatches": False, "errorClass": type(error).__name__})
    report = {"generatedAt": datetime.now(timezone.utc).isoformat(),
              "codexVersion": subprocess.check_output([str(args.codex), "--version"], text=True, encoding="utf-8").strip(),
              "taskMutations": [], "sourceScope": "explicit IDs; physical segment header only", "checks": checks}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(check.get("identityMatches") for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
