#!/usr/bin/env python3
"""诊断 Windows CI 的默认 owner 与私有 DACL，所有文件探针使用临时合成数据。

默认只读取本进程 token 并创建一次性样本。显式 --normalize-test-owner 将
primary token 复制后只调整副本的 TokenOwner，再用副本启动专用验收子进程。
从不修改原 token、系统策略、既有文件所有权或生产检查；这不是降低权限，
因此通过也不能声称已经验证了真正的普通用户桌面环境。脚本不属于发布包。
"""
from __future__ import annotations

import argparse
import codecs
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def token_snapshot(token=None) -> dict:
    """记录 SID、默认 ACL 和提权状态，不读取登录凭据或聊天来源。"""
    import win32api
    import win32security as security
    owned = token is None
    if owned:
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), security.TOKEN_QUERY)
    try:
        user = security.GetTokenInformation(token, security.TokenUser)[0]
        owner = security.GetTokenInformation(token, security.TokenOwner)
        user_sid = security.ConvertSidToStringSid(user)
        owner_sid = security.ConvertSidToStringSid(owner)
        statistics = security.GetTokenInformation(token, security.TokenStatistics)
        return {"userSid": user_sid, "ownerSid": owner_sid,
                "ownerEqualsUser": user_sid == owner_sid,
                "ownerIsAdministrators": owner_sid == "S-1-5-32-544",
                "elevated": bool(security.GetTokenInformation(token, security.TokenElevation)),
                "elevationType": security.GetTokenInformation(token, security.TokenElevationType),
                "tokenId": str(statistics["TokenId"]),
                "defaultDacl": acl_entries(security.GetTokenInformation(token, security.TokenDefaultDacl))}
    finally:
        if owned:
            token.Close()


def acl_entries(acl) -> list[dict] | None:
    import win32security as security
    if acl is None:
        return None
    result = []
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        result.append({"type": ace[0][0], "flags": ace[0][1], "mask": ace[1],
                       "sid": security.ConvertSidToStringSid(ace[-1])})
    return result


def object_snapshot(path: Path, user_sid: str) -> dict:
    import win32security as security
    descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
    owner = security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
    dacl = acl_entries(descriptor.GetSecurityDescriptorDacl())
    return {"ownerSid": owner, "ownerEqualsUser": owner == user_sid, "dacl": dacl,
            "aclOnlyUserAndSystem": dacl is not None and all(
                ace["type"] == security.ACCESS_ALLOWED_ACE_TYPE and ace["sid"] in (user_sid, "S-1-5-18")
                for ace in dacl)}


def file_probes(user_sid: str) -> dict:
    """分别观察默认创建、显式 owner 创建及 SQLite 继承 DACL 的真实 NTFS 行为。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from codex_trace.platform import filesystem as fs
    from codex_trace.storage import DerivedStore
    result = {}
    with tempfile.TemporaryDirectory(prefix="codex-owner-probe-") as temporary:
        root = Path(temporary).resolve()
        ordinary = root / "stdlib-file"
        ordinary.write_bytes(b"synthetic")
        result["stdlibFile"] = object_snapshot(ordinary, user_sid)
        ordinary_directory = root / "stdlib-directory"
        ordinary_directory.mkdir()
        result["stdlibDirectory"] = object_snapshot(ordinary_directory, user_sid)
        private = root / "explicit-private-directory"
        handle = fs.open_directory(private, create=True, private=True)
        try:
            result["explicitPrivateDirectory"] = object_snapshot(private, user_sid)
            descriptor = fs.open("database.sqlite3", fs.O_RDWR | fs.O_CREAT | fs.O_EXCL, 0o600, dir_fd=handle)
            fs.close(descriptor)
            result["explicitPrivateFile"] = object_snapshot(private / "database.sqlite3", user_sid)
            inherited = private / "native-inherited-file"
            inherited.write_bytes(b"synthetic inherited ACL")
            result["nativeInheritedFile"] = object_snapshot(inherited, user_sid)
            connection = sqlite3.connect(private / "database.sqlite3", isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("CREATE TABLE probe(value TEXT)")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO probe VALUES ('synthetic')")
                result["sqliteJournal"] = object_snapshot(private / "database.sqlite3-journal", user_sid)
                connection.execute("ROLLBACK")
            finally:
                connection.close()
        finally:
            fs.close(handle)
        try:
            with DerivedStore(root / "derived-store") as store:
                store.get_receipt("synthetic-missing")
            result["productionDerivedStore"] = {"passed": True}
        except Exception as error:
            result["productionDerivedStore"] = {"passed": False, "errorType": type(error).__name__,
                                                 "errorCode": str(error)}
    return result


def diagnostic() -> dict:
    snapshot = token_snapshot()
    result = {"platform": "win32", "token": snapshot, "ordinaryUserValidation": False}
    try:
        result["files"] = file_probes(snapshot["userSid"])
    except Exception as error:
        result["fileProbeError"] = {"errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
    child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--token-only"],
                           capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
    result["ordinaryChildToken"] = json.loads(child.stdout)
    return result


def create_process_with_token(token, command: list[str], cwd: Path):
    """pywin32 未暴露此 API；仅进程创建做薄封装，token/ACL 操作仍使用 pywin32。

    参数中显式给出可执行文件，避免空格路径歧义。子进程自行打开日志文件，
    不依赖 CreateProcessWithTokenW 的标准句柄继承；环境来自当前 CI 进程。
    """
    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                    ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                    ("x", wintypes.DWORD), ("y", wintypes.DWORD), ("xsize", wintypes.DWORD),
                    ("ysize", wintypes.DWORD), ("xchars", wintypes.DWORD), ("ychars", wintypes.DWORD),
                    ("fill", wintypes.DWORD), ("flags", wintypes.DWORD), ("show", wintypes.WORD),
                    ("reservedSize", wintypes.WORD), ("reservedBytes", wintypes.LPVOID),
                    ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                    ("processId", wintypes.DWORD), ("threadId", wintypes.DWORD)]
    function = ctypes.WinDLL("advapi32", use_last_error=True).CreateProcessWithTokenW
    function.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
                         wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
                         ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
    function.restype = wintypes.BOOL
    startup, process = StartupInfo(), ProcessInfo()
    startup.cb = ctypes.sizeof(startup)
    line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    environment = ctypes.create_unicode_buffer("\0".join(
        name + "=" + value for name, value in sorted(os.environ.items(), key=lambda pair: pair[0].upper())) + "\0\0")
    if not function(int(token), 0, command[0], line, 0x08000400, environment, str(cwd),
                    ctypes.byref(startup), ctypes.byref(process)):
        raise ctypes.WinError(ctypes.get_last_error())
    return process.process, process.thread


def normalized_child(request_path: Path) -> int:
    """只接受副本 token 的默认 owner；正常 subprocess 后代也必须保留该设置。"""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    report = {}
    code = 1
    with Path(request["log"]).open("w", encoding="utf-8", buffering=1) as log:
        try:
            report = diagnostic()
            report["normalizationMode"] = "duplicatedPrimaryTokenOwnerOnly"
            log.write(json.dumps(report, ensure_ascii=False) + "\n")
            if not report["token"]["ownerEqualsUser"] or not report["ordinaryChildToken"]["ownerEqualsUser"]:
                raise RuntimeError("normalized-owner-not-inherited")
            log.flush()
            code = subprocess.run(request["command"], stdout=log, stderr=subprocess.STDOUT).returncode if request["command"] else 0
            report["commandExitCode"] = code
        except Exception as error:
            report["error"] = {"errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
            log.write(json.dumps(report["error"]) + "\n")
        finally:
            Path(request["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def normalized_run(command: list[str]) -> tuple[int, dict]:
    """只修改复制的 token；原进程与 runner 的 owner、DACL、权限均不调整。"""
    import win32api
    import win32event
    import win32process
    import win32security as security
    original = security.OpenProcessToken(win32api.GetCurrentProcess(), security.TOKEN_QUERY | security.TOKEN_DUPLICATE)
    duplicate = None
    report = {"before": diagnostic(), "ordinaryUserValidation": False}
    try:
        access = security.TOKEN_QUERY | security.TOKEN_DUPLICATE | security.TOKEN_ASSIGN_PRIMARY | security.TOKEN_ADJUST_DEFAULT
        duplicate = security.DuplicateTokenEx(original, security.SecurityImpersonation, access, security.TokenPrimary)
        before_duplicate = token_snapshot(duplicate)
        if before_duplicate["tokenId"] == report["before"]["token"]["tokenId"]:
            raise RuntimeError("independent-token-copy-required")
        security.SetTokenInformation(duplicate, security.TokenOwner, security.GetTokenInformation(original, security.TokenUser)[0])
        report["duplicate"] = token_snapshot(duplicate)
        report["onlyDefaultOwnerChanged"] = all(before_duplicate[key] == report["duplicate"][key]
            for key in ("userSid", "tokenId", "defaultDacl", "elevated", "elevationType"))
        if not report["onlyDefaultOwnerChanged"]:
            raise RuntimeError("unexpected-token-change")
        with tempfile.TemporaryDirectory(prefix="codex-owner-child-") as temporary:
            root = Path(temporary).resolve()
            request = {"command": command, "report": str(root / "report.json"), "log": str(root / "output.log")}
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            Path(request["log"]).touch()
            child_command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child-request", str(request_path)]
            process, thread = create_process_with_token(duplicate, child_command, Path.cwd())
            try:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                with Path(request["log"]).open("rb") as log:
                    while True:
                        finished = win32event.WaitForSingleObject(process, 200) == win32event.WAIT_OBJECT_0
                        text = decoder.decode(log.read(), final=finished)
                        if text:
                            print(text, end="", flush=True)
                        if finished:
                            break
                code = win32process.GetExitCodeProcess(process)
                report["child"] = json.loads(Path(request["report"]).read_text(encoding="utf-8"))
                return code, report
            finally:
                win32api.CloseHandle(thread)
                win32api.CloseHandle(process)
    except Exception as error:
        report["normalizationError"] = {"errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
        return 1, report
    finally:
        report["afterOriginalToken"] = token_snapshot(original)
        report["originalTokenUnchanged"] = report["afterOriginalToken"] == report["before"]["token"]
        if duplicate is not None:
            duplicate.Close()
        original.Close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalize-test-owner", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--token-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if sys.platform != "win32":
        parser.error("This diagnostic requires native Windows")
    if args.token_only:
        print(json.dumps(token_snapshot()))
        return 0
    if args.child_request:
        return normalized_child(args.child_request)
    try:
        if args.normalize_test_owner:
            code, report = normalized_run(command)
            if not report["originalTokenUnchanged"]:
                code = 1
        else:
            report = diagnostic()
            code = subprocess.run(command).returncode if command else 0
    except Exception as error:
        report = {"errorType": type(error).__name__, "winerror": getattr(error, "winerror", None),
                  "ordinaryUserValidation": False, "normalizationUsesIndependentTokenOnly": True}
        code = 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
