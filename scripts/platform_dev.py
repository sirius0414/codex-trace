"""仅供本地开发服务的进程归属与锁；生产轨迹服务不依赖本模块。

POSIX 保留 flock/进程组行为。Windows 使用字节范围锁、进程创建时间和
CommandLineToArgvW 核对身份，通过 HTTP 请求正常退出；服务持有 Job Object，
异常退出时由内核回收它启动的 MCP 子进程，不按磁盘 PID 强杀进程树。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


@contextmanager
def exclusive_lock(stream):
    """同一管理文件上的非阻塞进程锁；离开上下文才释放。"""
    if IS_WINDOWS:
        import msvcrt
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError("development-manager-busy") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def windows_process_info(pid: int) -> dict | None:
    """只读 CIM 获取创建时间和完整命令行；不确定时抛错并保留管理回执。"""
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid-process-id")
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise OSError("PowerShell is required to verify Windows development process identity")
    # 唯一插入值是经过类型/范围验证的 PID；目录与 UUID 不进入 PowerShell 语法。
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
              f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
              "if($null -ne $p){[pscustomobject]@{started=$p.CreationDate.ToUniversalTime().ToString('o');"
              "command=$p.CommandLine}|ConvertTo-Json -Compress}")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", timeout=5, check=True)
    if not result.stdout.strip():
        return None
    value = json.loads(result.stdout.lstrip("\ufeff"))
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key]
                                         for key in ("started", "command")):
        raise OSError("Cannot verify Windows process identity")
    value.update({"group": pid, "argv": windows_argv(value["command"])})
    return value


def windows_argv(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def spawn_options() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def protect_windows_children():
    """服务自己持有 Job 句柄；服务异常退出也会回收 MCP，不依赖管理器存活。"""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # 故意保留句柄直到进程退出；手动关闭包含当前服务的 Job 会立即终止服务本身。
    return job
