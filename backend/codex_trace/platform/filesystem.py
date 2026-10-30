"""按宿主选择安全文件系统实现；目录标识是不透明资源，只能交回本模块。

普通文件仍返回 Python 文件描述符，可用于 read/write/fdopen；目录操作、权限、
所有权及 pread 必须经过本模块，避免在 Windows 上误用 Unix 目录 fd 或 chmod。
"""
import os

if os.name == "nt":
    from ._windows import *  # noqa: F403
else:
    from ._posix import *  # noqa: F403


def __getattr__(name):
    """仅保留无平台安全语义的 os 工具；文件操作由上面的显式实现覆盖。"""
    return getattr(os, name)
