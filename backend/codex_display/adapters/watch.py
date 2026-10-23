"""Owned read-only vnode notifications with a mandatory short polling fallback."""
from __future__ import annotations

from collections import OrderedDict
import math
import os
import select
import threading


class SourceWatcher:
    """Only accepts descriptors already safely opened by RawAdapter.

    wait_for_changes always returns after at most 250 ms, even where kqueue is
    unavailable, registration fails, or a source has moved. A notification is
    only a wake-up hint; every read still validates file identity and bounds.
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
                owned = os.dup(descriptor)
            except OSError:
                # Notification capacity is optional; the caller's validated
                # read continues and the 250 ms poll still observes changes.
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
            # A concurrent close or an invalidated descriptor is never evidence
            # that a task finished; the caller still performs its fallback read.
            return False

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            if self._queue is not None:
                self._queue.close()
                self._queue = None
            for descriptor, _ in self._files.values():
                os.close(descriptor)
            self._files.clear()
