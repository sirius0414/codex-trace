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
        # Windows 字节锁也禁止其他进程读取锁区；不能先 read 再尝试抢锁。
        if os.fstat(stream.fileno()).st_size == 0:
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


def windows_process_exited(pid: int) -> bool:
    """只在内核证明进程已退出或 PID 不存在时返回 True；访问受限仍报错。"""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE，不申请终止权限。
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER：该 PID 已不存在。
            return True
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:  # WAIT_OBJECT_0
            return True
        if state == 258:  # WAIT_TIMEOUT
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


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
    try:
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", timeout=5, check=True)
        value = json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key]
                                             for key in ("started", "command")):
            raise OSError("Cannot verify Windows process identity")
        value.update({"group": pid, "argv": windows_argv(value["command"])})
        return value
    except (OSError, ValueError, subprocess.SubprocessError):
        # 正常停止可在 CIM 读取属性时结束进程；失败本身不能证明进程已退出。
        if windows_process_exited(pid):
            return None
        raise


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


def python_spawn_options(python: Path) -> dict:
    """绕过 Windows venv redirector，使 Popen PID 就是实际服务的 PID。"""
    options = spawn_options()
    if not IS_WINDOWS:
        return options
    probe = subprocess.run([str(python), "-c",
        "import json,sys;print(json.dumps([sys._base_executable,sys.prefix != sys.base_prefix]))"],
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    value = json.loads(probe.stdout)
    if (not isinstance(value, list) or len(value) != 2
            or not isinstance(value[0], str) or not value[0] or type(value[1]) is not bool):
        raise ValueError("Cannot identify the selected Python runtime")
    if value[1]:
        base = Path(value[0])
        if not base.is_absolute() or not base.is_file():
            raise ValueError("Cannot locate the selected virtual environment's base Python")
        # CreateProcess 直接执行基础解释器，命令行仍保留 venv argv[0] 供身份核对。
        # CPython 消费该变量来恢复 sys.executable/sys.prefix 和 venv 依赖搜索路径。
        options.update(executable=str(base), env={**os.environ, "__PYVENV_LAUNCHER__": str(python)})
    return options


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
