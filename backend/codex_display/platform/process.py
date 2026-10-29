"""历史只读连接的进程 argv 边界；不经过命令解释器解析本地路径。"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def executable_command(executable: str, *arguments: str) -> list[str]:
    """支持原生程序及合成 Python peer；拒绝隐式调用 cmd.exe 的批处理入口。"""
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise ValueError("absolute-executable-required")
    suffix = Path(executable).suffix.lower()
    if os.name == "nt" and suffix in (".bat", ".cmd"):
        raise ValueError("native-executable-required")
    return ([sys.executable, executable] if suffix == ".py" else [executable]) + list(arguments)
