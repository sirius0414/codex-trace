"""为串行 JSON-RPC 提供有截止时间的字节管道，不解析业务协议。

POSIX 保留非阻塞 selector；Windows 的匿名子进程管道不能注册 socket selector，
因此使用有界队列与专属读写线程。超时后上层必须终止自有子进程，再 close 回收，
不重用可能仍有迟到字节的连接。线程既不接触来源文件，也不启动额外进程。
"""
from __future__ import annotations

import os
import queue
import selectors
import threading
import time


class _WindowsCancellation:
    """保留真实线程句柄直到 worker 退出，取消时不按可能复用的 native_id 查线程。"""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name in ("GetCurrentProcess", "GetCurrentThread"):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = [], wintypes.HANDLE
        self.kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                                wintypes.BOOL, wintypes.DWORD]
        self.kernel.DuplicateHandle.restype = wintypes.BOOL
        self.kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        self.kernel.CancelSynchronousIo.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.lock, self.handles = threading.Lock(), set()

    def register(self):
        handle = self.wintypes.HANDLE()
        process = self.kernel.GetCurrentProcess()
        # CancelSynchronousIo 要求 THREAD_TERMINATE。复制当前线程伪句柄得到自有的
        # 实句柄，worker 退出前一直保留；注册、取消和关闭由同一锁保护。
        if not self.kernel.DuplicateHandle(process, self.kernel.GetCurrentThread(), process,
                                            self.ctypes.byref(handle), 0x0001, False, 0):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        with self.lock:
            self.handles.add(handle.value)
        return handle.value

    def unregister(self, handle):
        with self.lock:
            self.handles.remove(handle)
            self.kernel.CloseHandle(handle)

    def cancel(self):
        with self.lock:
            for handle in self.handles:
                # ERROR_NOT_FOUND 表示该线程此刻没有同步 I/O；close 会重复取消，
                # 覆盖 stopped 检查后、真正进入 ReadFile/WriteFile 前的调度窗口。
                if not self.kernel.CancelSynchronousIo(handle):
                    code = self.ctypes.get_last_error()
                    if code != 1168:
                        # 某次取消失败仍继续尝试其他 worker；close 以最终是否退出判定，
                        # 不能因第一个句柄的错误跳过另一个仍阻塞的管道。
                        continue


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("pipe-timeout")
    return remaining


class PosixPipeTransport:
    """保持短写、背压及读取共用调用方的单个截止时间。"""
    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.selector = selectors.DefaultSelector()
        try:
            os.set_blocking(stdin.fileno(), False)
            os.set_blocking(stdout.fileno(), False)
            self.selector.register(stdout, selectors.EVENT_READ)
        except BaseException:
            self.selector.close()
            raise

    def write(self, data: bytes, deadline: float) -> None:
        offset = 0
        with selectors.DefaultSelector() as writable:
            writable.register(self.stdin, selectors.EVENT_WRITE)
            while offset < len(data):
                if not writable.select(_remaining(deadline)):
                    raise TimeoutError("pipe-timeout")
                try:
                    count = os.write(self.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
                if not count:
                    raise BrokenPipeError("pipe-disconnected")
                offset += count

    def read(self, maximum: int, deadline: float) -> bytes:
        while True:
            if not self.selector.select(_remaining(deadline)):
                raise TimeoutError("pipe-timeout")
            try:
                return os.read(self.stdout.fileno(), maximum)
            except BlockingIOError:
                continue

    def close(self) -> None:
        self.selector.close()


class ThreadPipeTransport:
    """以最多 16 个 64 KiB 块缓冲匿名管道；每条连接仅有一个写请求在途。"""
    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.stopped = threading.Event()
        self.incoming = queue.Queue(maxsize=16)
        self.outgoing = queue.Queue(maxsize=1)
        self.pending = b""
        self.cancellation = _WindowsCancellation() if os.name == "nt" else None
        if self.cancellation is None:
            # 本机合成验证也可强制选择线程实现；POSIX 用短轮询退出，不能依赖远端
            # 所有继承的管道句柄都已关闭，更不能留下读旧 fd 的后台线程。
            os.set_blocking(stdin.fileno(), False)
            os.set_blocking(stdout.fileno(), False)
        startup = [(target, name, threading.Event()) for target, name in (
            (self._reader, "codex-display-pipe-read"), (self._writer, "codex-display-pipe-write"))]
        errors = []
        self.threads = [threading.Thread(target=self._run, args=(target, ready, errors), name=name, daemon=True)
                        for target, name, ready in startup]
        try:
            for thread in self.threads:
                thread.start()
            for _, _, ready in startup:
                if not ready.wait(2):
                    raise OSError("pipe-worker-start-timeout")
            if errors:
                raise OSError("pipe-worker-start-failed")
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _run(self, target, ready, errors):
        handle = None
        try:
            if self.cancellation is not None:
                handle = self.cancellation.register()
            ready.set()
            if not self.stopped.is_set():
                target()
        except (OSError, ValueError) as error:
            if not ready.is_set():
                errors.append(error)
                ready.set()
            else:
                self._deliver(error)
        finally:
            if handle is not None:
                self.cancellation.unregister(handle)

    def _deliver(self, value) -> bool:
        while not self.stopped.is_set():
            try:
                self.incoming.put(value, timeout=.05)
                return True
            except queue.Full:
                continue
        return False

    def _reader(self) -> None:
        readable = None
        try:
            if self.cancellation is None:
                readable = selectors.DefaultSelector()
                readable.register(self.stdout, selectors.EVENT_READ)
            while not self.stopped.is_set():
                if readable is not None and not readable.select(.05):
                    continue
                try:
                    chunk = os.read(self.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not self._deliver(chunk) or not chunk:
                    return
        except (OSError, ValueError) as error:
            self._deliver(error)
        finally:
            if readable is not None:
                readable.close()

    def _writer(self) -> None:
        writable = None
        try:
            if self.cancellation is None:
                writable = selectors.DefaultSelector()
                writable.register(self.stdin, selectors.EVENT_WRITE)
            self._write_requests(writable)
        finally:
            if writable is not None:
                writable.close()

    def _write_requests(self, writable) -> None:
        while not self.stopped.is_set():
            try:
                data, complete, errors = self.outgoing.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                offset = 0
                while offset < len(data) and not self.stopped.is_set():
                    if writable is not None and not writable.select(.05):
                        continue
                    try:
                        count = os.write(self.stdin.fileno(), data[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    if not count:
                        raise BrokenPipeError("pipe-disconnected")
                    offset += count
                if offset != len(data):
                    raise BrokenPipeError("pipe-closed")
            except (OSError, ValueError) as error:
                errors.append(error)
            finally:
                complete.set()

    def write(self, data: bytes, deadline: float) -> None:
        complete, errors = threading.Event(), []
        try:
            self.outgoing.put((data, complete, errors), timeout=_remaining(deadline))
        except queue.Full:
            raise TimeoutError("pipe-timeout") from None
        if not complete.wait(_remaining(deadline)):
            raise TimeoutError("pipe-timeout")
        if errors:
            raise errors[0]

    def read(self, maximum: int, deadline: float) -> bytes:
        _remaining(deadline)
        if not self.pending:
            try:
                value = self.incoming.get(timeout=_remaining(deadline))
            except queue.Empty:
                raise TimeoutError("pipe-timeout") from None
            if isinstance(value, BaseException):
                raise value
            self.pending = value
        result, self.pending = self.pending[:maximum], self.pending[maximum:]
        return result

    def close(self) -> None:
        """停止并取消自有 worker 的同步 I/O；远端继承管道也不能阻止本连接回收。

        上层先停止直接子进程，再调用这里。取消使用保留的线程句柄并重复至退出；
        只关闭本传输的资源，不杀持有继承管道的其他进程。原生取消行为须在 Windows
        实机验证；无法确认退出时显式报错，由上层仍然关闭每个管道。
        """
        self.stopped.set()
        deadline = time.monotonic() + 1
        while any(thread.is_alive() for thread in self.threads):
            if self.cancellation is not None:
                self.cancellation.cancel()
            for thread in self.threads:
                if thread.ident is not None:
                    thread.join(timeout=min(.025, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline and any(thread.is_alive() for thread in self.threads):
                raise OSError("pipe-worker-stop-timeout")


def make_pipe_transport(stdin, stdout):
    return (ThreadPipeTransport if os.name == "nt" else PosixPipeTransport)(stdin, stdout)
