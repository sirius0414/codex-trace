#!/usr/bin/env python3
"""诊断 Windows CI 的默认 owner 与私有 DACL，所有文件探针使用临时合成数据。

默认只读取本进程 token 并创建一次性样本。显式 --normalize-test-owner 通过
普通 subprocess 创建专用子进程，核验其 TokenId 与父进程不同且用户相同，
再只调整该独立子进程的 TokenOwner；结束时恢复，父 token 前后必须完全一致。
不修改系统策略、既有文件所有权或生产检查；这不是降低权限，因此通过也不能
声称已验证真正的普通用户桌面环境。脚本不属于发布包。
"""
from __future__ import annotations

import argparse
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
                "groups": sorted((security.ConvertSidToStringSid(sid), attributes)
                    for sid, attributes in security.GetTokenInformation(token, security.TokenGroups)),
                # USED_FOR_ACCESS 仅记录是否使用过权限，不表示权限本身变化；比较实际授权位。
                "privileges": sorted((security.LookupPrivilegeName(None, luid), attributes & ~0x80000000)
                    for luid, attributes in security.GetTokenInformation(token, security.TokenPrivileges)),
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


UNCHANGED_TOKEN_FIELDS = ("userSid", "tokenId", "defaultDacl", "elevated", "elevationType", "groups", "privileges")


def require_independent_child_token(parent: dict, child: dict) -> None:
    """TokenId 标识 token 对象实例；必须先证实对象独立，不能只比较进程号。"""
    if child["tokenId"] == parent["tokenId"]:
        raise RuntimeError("independent-child-token-required")
    if child["userSid"] != parent["userSid"]:
        raise RuntimeError("same-token-user-required")


def normalized_child(request_path: Path) -> int:
    """仅修改经核验的独立子进程 token，finally 恢复；测试后代继承规范化 owner。"""
    import win32api
    import win32security as security
    request = json.loads(request_path.read_text(encoding="utf-8"))
    report = {"normalizationMode": "verifiedIndependentChildTokenOwnerOnly", "ordinaryUserValidation": False}
    token = None
    changed = False
    original_owner = None
    stage = "open_child_token"
    code = 1
    try:
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), security.TOKEN_QUERY | security.TOKEN_ADJUST_DEFAULT)
        before = token_snapshot(token)
        report["beforeChildToken"] = before
        stage = "verify_independent_child_token"
        require_independent_child_token(request["parentToken"], before)
        report["independentChildTokenVerified"] = True
        original_owner = security.GetTokenInformation(token, security.TokenOwner)
        stage = "normalize_child_owner"
        security.SetTokenInformation(token, security.TokenOwner, security.GetTokenInformation(token, security.TokenUser)[0])
        changed = True
        normalized = token_snapshot(token)
        report["normalizedChildToken"] = normalized
        report["onlyDefaultOwnerChanged"] = all(before[key] == normalized[key] for key in UNCHANGED_TOKEN_FIELDS)
        if not normalized["ownerEqualsUser"] or not report["onlyDefaultOwnerChanged"]:
            raise RuntimeError("unexpected-token-change")
        stage = "verify_native_files_and_grandchild"
        report["diagnostic"] = diagnostic()
        if not report["diagnostic"]["ordinaryChildToken"]["ownerEqualsUser"]:
            raise RuntimeError("normalized-owner-not-inherited")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        stage = "run_command"
        code = subprocess.run(request["command"]).returncode if request["command"] else 0
        report["commandExitCode"] = code
    except Exception as error:
        report["error"] = {"stage": stage, "errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
    finally:
        try:
            if changed:
                security.SetTokenInformation(token, security.TokenOwner, original_owner)
                report["restoredChildToken"] = token_snapshot(token)
                report["childTokenRestored"] = report["restoredChildToken"] == report["beforeChildToken"]
                if not report["childTokenRestored"]:
                    code = 1
        except Exception as error:
            report["restoreError"] = {"errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
            code = 1
        finally:
            if token is not None:
                token.Close()
        Path(request["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return code


def normalized_run(command: list[str]) -> tuple[int, dict]:
    """父进程只查询 token；普通子进程证明 token 独立后才自行调整默认 owner。"""
    report = {"before": diagnostic(), "ordinaryUserValidation": False}
    stage = "prepare_child_request"
    code = 1
    try:
        with tempfile.TemporaryDirectory(prefix="codex-owner-child-") as temporary:
            root = Path(temporary).resolve()
            request = {"command": command, "report": str(root / "report.json"), "parentToken": report["before"]["token"]}
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            child_command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child-request", str(request_path)]
            stage = "start_independent_child"
            code = subprocess.run(child_command).returncode
            stage = "read_child_report"
            report["child"] = json.loads(Path(request["report"]).read_text(encoding="utf-8"))
            if not report["child"].get("independentChildTokenVerified") or not report["child"].get("childTokenRestored"):
                code = 1
    except Exception as error:
        report["normalizationError"] = {"stage": stage, "errorType": type(error).__name__, "winerror": getattr(error, "winerror", None)}
        code = 1
    finally:
        report["afterOriginalToken"] = token_snapshot()
        report["originalTokenUnchanged"] = report["afterOriginalToken"] == report["before"]["token"]
        if not report["originalTokenUnchanged"]:
            code = 1
    return code, report


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
                  "ordinaryUserValidation": False, "normalizationRequiresIndependentChildToken": True}
        code = 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
