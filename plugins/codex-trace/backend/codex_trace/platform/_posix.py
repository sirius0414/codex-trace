"""POSIX 原有 dir_fd/O_NOFOLLOW 行为，不改变 macOS 的权限及发布语义。"""
import os
from pathlib import Path

O_RDONLY = os.O_RDONLY
O_WRONLY = os.O_WRONLY
O_RDWR = os.O_RDWR
O_CREAT = os.O_CREAT
O_EXCL = os.O_EXCL
O_APPEND = os.O_APPEND
O_TRUNC = os.O_TRUNC
O_NOFOLLOW = os.O_NOFOLLOW
O_NONBLOCK = os.O_NONBLOCK
O_DIRECTORY = os.O_DIRECTORY

open = os.open
close = os.close
fstat = os.fstat
stat = os.stat
mkdir = os.mkdir
unlink = os.unlink
link = os.link
fsync = os.fsync
fchmod = os.fchmod
getuid = os.getuid
listdir = os.listdir
dup = os.dup
pread = os.pread


def open_directory(path, *, create=False, private=False):
    """逐级打开绝对路径；仅收紧最终私有目录，不修改既有祖先。"""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute-directory-required")
    current = open(path.anchor, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        for name in path.parts[1:]:
            try:
                child = open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    mkdir(name, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=current)
            close(current)
            current = child
        if private:
            if fstat(current).st_uid != getuid():
                raise PermissionError("directory-not-owned")
            fchmod(current, 0o700)
        return current
    except BaseException:
        close(current)
        raise


def open_read(path):
    """以只读方式打开普通文件，任意祖先链接都不能绕过读取边界。"""
    path = Path(path)
    directory = open_directory(path.parent)
    try:
        return open(path.name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=directory)
    finally:
        close(directory)
