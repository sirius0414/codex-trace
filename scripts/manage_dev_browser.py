#!/usr/bin/env python3
"""仅供开发的本地浏览器服务管理器，不注册系统服务，也不进入插件包。

后台进程脱离调用终端；PID、启动时间和实例 ID 共同确认归属，避免旧 PID
误伤其他进程。所有管理动作串行执行，停止时等待 HTTP/MCP 的正常清理。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

from update_plugin import runtime_paths
from platform_dev import IS_WINDOWS, exclusive_lock, spawn_options, windows_process_info

ROOT = Path(__file__).resolve().parents[1]
DEV_ROOT = ROOT / "build/dev-browser"
SERVER = ROOT / "scripts/dev_browser.py"
SERVICE = "codex-trace-dev-browser"


class ManagerError(RuntimeError):
    pass


def process_info(pid: int) -> dict | None:
    """只读取指定进程的身份；僵尸进程按已退出处理。"""
    try:
        if IS_WINDOWS:
            return windows_process_info(pid)
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
                                env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, timeout=2, encoding="utf-8")
        fields = result.stdout.strip().split(None, 6)
        if not result.stdout.strip() and not result.stderr.strip():
            return None
        if result.returncode or len(fields) != 7:
            raise ManagerError("无法核对进程身份，已保留服务记录；请稍后重试。")
        if "Z" in fields[0]:
            return None
        return {"started": " ".join(fields[1:6]), "command": fields[6], "group": os.getpgid(pid)}
    except ProcessLookupError:
        return None
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagerError("无法读取进程身份，已保留服务记录；请稍后重试。") from error


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def probe_health(port: int) -> dict | None:
    """绕过代理检查本机实例；不创建观察会话或读取用户任务。"""
    try:
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(f"http://127.0.0.1:{port}/_dev/health", timeout=0.5) as response:
            value = json.loads(response.read(4096))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


class ServiceManager:
    def __init__(self, directory: Path = DEV_ROOT, *, startup_timeout: float = 60, stop_timeout: float = 20):
        self.directory = directory
        self.record_path = directory / "server.json"
        self.pid_path = directory / "server.pid"
        self.log_path = directory / "server.log"
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "server.lock").open("a+b") as lock:
            try:
                with exclusive_lock(lock):
                    yield
            except BlockingIOError as error:
                raise ManagerError("另一个服务管理命令正在执行，请稍后重试。") from error

    def _read_record(self) -> dict | None:
        try:
            record = json.loads(self.record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise ManagerError(f"服务记录无法读取：{self.record_path}；未操作任何进程。") from error
        if (not isinstance(record, dict) or record.get("schemaVersion") != 1 or record.get("root") != str(ROOT)
                or type(record.get("pid")) is not int or record["pid"] <= 1
                or type(record.get("port")) is not int or not 1 <= record["port"] <= 65535
                or any(not isinstance(record.get(key), str) or not record[key]
                       for key in ("started", "instanceId", "python", "codex"))
                or type(record.get("skipBuild")) is not bool):
            raise ManagerError(f"服务记录无效：{self.record_path}；未操作任何进程。")
        return record

    def _write_record(self, record: dict):
        temporary = self.directory / f".server-{uuid4().hex}.json"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.write("\n")
            temporary.replace(self.record_path)
            self.pid_path.write_text(str(record["pid"]) + "\n", encoding="utf-8")
        finally:
            temporary.unlink(missing_ok=True)

    def _clear_record(self):
        self.record_path.unlink(missing_ok=True)
        self.pid_path.unlink(missing_ok=True)

    def owns(self, record: dict) -> bool:
        info = process_info(record["pid"])
        if IS_WINDOWS:
            argv = info.get("argv", []) if info else []
            return bool(info and info["started"] == record["started"]
                        and str(SERVER) in argv and "--instance-id" in argv
                        and argv.index("--instance-id") + 1 < len(argv)
                        and argv[argv.index("--instance-id") + 1] == record["instanceId"]
                        and Path(argv[0]).absolute() == Path(record["python"]).absolute())
        # ps 的 command 可能不引用含空格的路径；匹配完整参数边界，拒绝路径/UUID 前缀相同的进程。
        script = r"(?:^|\s)" + re.escape(str(SERVER)) + r"(?=\s|$)"
        marker = r"(?:^|\s)--instance-id\s+" + re.escape(record["instanceId"]) + r"(?=\s|$)"
        return bool(info and info["started"] == record["started"] and info["group"] == record["pid"]
                    and re.search(script, info["command"]) and re.search(marker, info["command"]))

    @staticmethod
    def _healthy(record: dict) -> bool:
        health = probe_health(record["port"])
        return bool(health and health.get("service") == SERVICE and health.get("pid") == record["pid"]
                    and health.get("instanceId") == record["instanceId"])

    def _status(self) -> dict:
        record = self._read_record()
        if record is None:
            return {"state": "stopped", "unmanagedPort": 8765 if port_busy(8765) else None}
        if not self.owns(record):
            return {"state": "stale", "record": record}
        return {"state": "running" if self._healthy(record) else "unhealthy", "record": record}

    def status(self) -> dict:
        """运行中须同时满足进程归属及 HTTP 就绪检查；仅端口有监听不能证明服务身份。"""
        with self.locked():
            return self._status()

    def _stop(self) -> dict:
        state = self._status()
        if state["state"] == "stopped":
            return state
        record = state["record"]
        if state["state"] == "stale":
            self._clear_record()
            return {"state": "stopped", "staleRecord": True}
        # 信号前再次核对，不能只凭磁盘上的 PID 或端口停止进程。
        if self.owns(record):
            if IS_WINDOWS:
                self._request_shutdown(record)
            else:
                try:
                    os.killpg(record["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + self.stop_timeout
        while self.owns(record):
            if time.monotonic() >= deadline:
                # MCP 使用独立进程组；强杀 HTTP 不能保证回收它，超时应保留记录供再次核对。
                raise ManagerError(f"服务尚未完成停止，已保留进程记录，请查看日志：{self.log_path}")
            time.sleep(0.1)
        self._clear_record()
        return {"state": "stopped"}

    def stop(self) -> dict:
        """正常停止自有实例；超时保留记录，重复停止不向其他进程发送信号。"""
        with self.locked():
            return self._stop()

    def _discard_attempt(self, process):
        """清理本次刚创建、尚由 Popen 持有的子进程；即使落盘或身份查询失败也不遗留它。"""
        if process.poll() is None:
            # 未 wait/reap 的自有子进程 PID 不会被重用，此处不依赖可能写失败的管理记录。
            try:
                if IS_WINDOWS:
                    record = self._read_record()
                    if record and self.owns(record) and self._healthy(record):
                        self._request_shutdown(record)
                    else:
                        # Popen 仍持有新建进程句柄；该服务的 Job 同时回收其 MCP。
                        process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired as error:
                raise ManagerError(f"启动失败且 PID {process.pid} 仍在清理，已保留现有记录；请查看日志：{self.log_path}") from error
        self._clear_record()

    @staticmethod
    def _request_shutdown(record):
        """仅请求已核对身份的开发宿主正常退出；不能把 SIGTERM 映射成 Windows 强杀。"""
        token = record.get("shutdownToken")
        if not isinstance(token, str) or not token:
            raise ManagerError("服务缺少安全停止凭据，请在原启动终端使用 Ctrl+C。")
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{record['port']}/_dev/shutdown", data=b"",
            headers={"X-Codex-Trace-Shutdown": token}, method="POST")
        try:
            with client.open(request, timeout=2) as response:
                if response.status != 202:
                    raise OSError("shutdown-not-accepted")
        except (OSError, urllib.error.URLError) as error:
            raise ManagerError("无法请求服务正常停止，已保留进程记录；未强杀进程。") from error

    def _start(self, *, port: int | None = None, python_arg: str | None = None,
               codex_arg: str | None = None, skip_build: bool | None = None) -> dict:
        state = self._status()
        if state["state"] == "running":
            if port is not None and port != state["record"]["port"]:
                raise ManagerError("服务已在其他端口运行；修改配置请使用 restart。")
            return {**state, "alreadyRunning": True}
        if state["state"] == "unhealthy":
            raise ManagerError("服务进程仍在，但未通过就绪检查，请使用 restart 或查看日志。")
        if state["state"] == "stale":
            self._clear_record()
        port = port if port is not None else 8765
        if not 1 <= port <= 65535:
            raise ManagerError("端口必须在 1–65535 之间。")
        if port_busy(port):
            raise ManagerError(f"端口 {port} 已被其他进程占用；未启动或停止任何服务。")
        python, codex = runtime_paths(python_arg, codex_arg)
        instance = str(uuid4())
        shutdown_token = secrets.token_urlsafe(32) if IS_WINDOWS else None
        command = [str(python), str(SERVER), "--port", str(port), "--python", str(python),
                   "--codex", str(codex), "--instance-id", instance]
        if skip_build:
            command.append("--skip-build")
        if shutdown_token:
            command.extend(["--shutdown-token", shutdown_token])
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab", buffering=0) as log:
            log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting local browser service\n".encode())
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, **spawn_options())
        try:
            info = process_info(process.pid)
            if info is None:
                raise ManagerError(f"服务在启动时退出，请查看日志：{self.log_path}")
            record = {"schemaVersion": 1, "root": str(ROOT), "pid": process.pid, "port": port,
                      "instanceId": instance, "started": info["started"], "python": str(python),
                      "codex": str(codex), "skipBuild": bool(skip_build)}
            if shutdown_token:
                record["shutdownToken"] = shutdown_token
            # 先持久化再等待就绪；管理命令被打断后，下次命令仍能识别已启动的进程。
            self._write_record(record)
            deadline = time.monotonic() + self.startup_timeout
            while process.poll() is None:
                if self._healthy(record):
                    return {"state": "running", "record": record}
                if time.monotonic() >= deadline:
                    raise ManagerError(f"服务启动超时，请查看日志：{self.log_path}")
                time.sleep(0.1)
            raise ManagerError(f"服务启动失败（退出码 {process.returncode}），请查看日志：{self.log_path}")
        except Exception:
            self._discard_attempt(process)
            raise

    def start(self, **options) -> dict:
        """服务就绪后才返回成功；已运行时复用实例，启动失败保留日志并清理本次子进程。"""
        with self.locked():
            return self._start(**options)

    def restart(self, *, port: int | None = None, python_arg: str | None = None,
                codex_arg: str | None = None, skip_build: bool | None = None) -> dict:
        with self.locked():
            previous = self._read_record() or {}
            next_port = port if port is not None else previous.get("port", 8765)
            if not 1 <= next_port <= 65535:
                raise ManagerError("端口必须在 1–65535 之间；原服务保持运行。")
            # 先拒绝不存在的显式运行环境，避免输错参数后才发现原服务已经停止。
            python, codex = runtime_paths(python_arg or previous.get("python"), codex_arg or previous.get("codex"))
            self._stop()
            return self._start(port=next_port, python_arg=str(python), codex_arg=str(codex),
                               skip_build=skip_build if skip_build is not None else previous.get("skipBuild"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    parser.add_argument("--port", type=int, help="启动端口（默认 8765；restart 默认复用原值）")
    parser.add_argument("--python", help="已有 Python 运行环境")
    parser.add_argument("--codex", help="已有 Codex 可执行文件")
    parser.add_argument("--skip-build", action="store_true", default=None, help="跳过前端构建")
    args = parser.parse_args()
    if args.action in ("stop", "status") and any(value is not None for value in (args.port, args.python, args.codex, args.skip_build)):
        parser.error("stop/status 使用已记录的服务配置，不接受启动参数")
    manager = ServiceManager()
    try:
        operation = getattr(manager, args.action)
        result = operation(**{"port": args.port, "python_arg": args.python, "codex_arg": args.codex,
                              "skip_build": args.skip_build}) if args.action in ("start", "restart") else operation()
        record = result.get("record", {})
        state = result["state"]
        if state == "running":
            print(f"服务{'已在运行' if result.get('alreadyRunning') else '运行中'}：http://localhost:{record['port']}/（PID {record['pid']}）")
        elif state == "unhealthy":
            print(f"服务进程仍在，但未通过就绪检查（PID {record['pid']}），请重启或查看日志。")
        elif state == "stale":
            print("服务未运行，存在已失效的进程记录；下次 start/stop 会安全清理。")
        elif result.get("staleRecord"):
            print("已清理失效的服务记录；未向身份不符的进程发送信号。")
        else:
            print("受管服务已停止。")
        if result.get("unmanagedPort"):
            print("端口 8765 有其他进程监听，本命令不会停止该进程。")
        print(f"日志：{manager.log_path}")
        return 0 if state in ("running", "stopped", "stale") else 1
    except (ManagerError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"本地调试服务管理失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
