"""自有只读 vnode 监听；始终保留短轮询兜底，通知仅用于提前唤醒。"""
from __future__ import annotations

from collections import OrderedDict
import math
from ..platform import filesystem as os
import select
import threading


class SourceWatcher:
    """只接受 RawAdapter 已安全打开的描述符，复制后由本类独立回收。

    kqueue 不可用、注册失败或来源移动时仍最多等待 250 ms。通知不能替代身份和边界
    校验，删除/重命名通知也不能被解释成任务已经完成。
    """
    def __init__(self, *, max_sources: int = 128):
        if type(max_sources) is not int or max_sources < 1:
            raise ValueError("positive-watch-limit-required")
        self.max_sources = max_sources
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._files = OrderedDict()
        try:
            self._queue = select.kqueue() if hasattr(select, "kqueue") else None
        except OSError:
            self._queue = None
        self.mode = "kqueue" if self._queue is not None else "poll"

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def watch(self, key: str, descriptor: int) -> None:
        """监听已验证来源；同路径换 inode 时替换旧监听，条目超限按 LRU 释放。"""
        with self._lock:
            if self._closed.is_set() or self._queue is None:
                return
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            previous = self._files.get(key)
            if previous and previous[1] == identity:
                self._files.move_to_end(key)
                return
            if previous:
                os.close(self._files.pop(key)[0])
            try:
                # 复制描述符，避免读取结束关闭原 fd 后让监听失效或误关闭调用方资源。
                owned = os.dup(descriptor)
            except OSError:
                # 通知容量不足不影响已校验的读取，250 ms 轮询仍会发现后续变化。
                return
            try:
                flags = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB |
                         select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)
                event = select.kevent(owned, filter=select.KQ_FILTER_VNODE,
                                     flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=flags)
                self._queue.control([event], 0, 0)
            except OSError:
                os.close(owned)
                return
            self._files[key] = (owned, identity)
            while len(self._files) > self.max_sources:
                os.close(self._files.popitem(last=False)[1][0])

    def wait_for_changes(self, timeout: float = .25) -> bool:
        """在关闭、平台不支持和并发失效时安全退回轮询，不把异常当成状态证据。"""
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid-watch-timeout")
        timeout = min(timeout, .25)
        with self._lock:
            queue = None if self._closed.is_set() else self._queue
        if self._closed.is_set():
            return False
        if queue is None:
            self._closed.wait(timeout)
            return False
        try:
            return bool(queue.control(None, self.max_sources, timeout))
        except (OSError, ValueError):
            # 并发关闭或 fd 失效不是任务终态证据，调用方仍须执行兜底读取。
            return False

    def close(self) -> None:
        """幂等关闭事件队列及所有自有 fd；同时唤醒 poll 模式中的等待者。"""
        with self._lock:
            self._closed.set()
            if self._queue is not None:
                self._queue.close()
                self._queue = None
            for descriptor, _ in self._files.values():
                os.close(descriptor)
            self._files.clear()
