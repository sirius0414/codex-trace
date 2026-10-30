"""只协调插件私有文件的跨进程锁；平台模块按需导入，来源永不加写锁。"""
from __future__ import annotations

import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        """锁定第一个字节（允许超过 EOF）；不靠改写锁文件表达持有状态。"""
        operation = _Overlapped()
        if not _kernel.LockFileEx(msvcrt.get_osfhandle(fd), 2 | (0 if blocking else 1),
                                  0, 1, 0, ctypes.byref(operation)):
            code = ctypes.get_last_error()
            if code in (33, 158):
                raise BlockingIOError("export-lock-busy")
            raise ctypes.WinError(code)

    def unlock(fd: int) -> None:
        operation = _Overlapped()
        if not _kernel.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(operation)):
            raise ctypes.WinError(ctypes.get_last_error())
else:
    import fcntl

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
