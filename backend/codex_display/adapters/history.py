"""Bounded, read-only access to the Codex app-server history API.

The child app-server may maintain its own runtime cache. This adapter never
resumes, starts, interrupts or changes a Codex task. M0 exposes metadata only;
raw response bodies are neither logged nor retained as diagnostics. Historical
items are returned only to the projection service, never as diagnostic text.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any


READ_METHODS = frozenset({
    "initialize", "thread/list", "thread/read", "thread/turns/list", "thread/items/list",
})
_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_MESSAGES = 256
_SOURCE_TYPES = frozenset({"cli", "vscode", "exec", "appServer", "subAgent", "unknown"})
_RUNTIME_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})


class HistoryError(RuntimeError):
    """An error whose public message never contains app-server response text."""

    def __init__(self, code: str, *, rpc_code: int | None = None):
        self.code = code
        self.rpc_code = rpc_code
        super().__init__(code)


def _thread_id(value: Any) -> str:
    if not isinstance(value, str) or not _THREAD_ID.fullmatch(value):
        raise HistoryError("invalid-thread-id")
    return value


def _bounded_text(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def _cursor(value: Any, *, response: bool = False) -> str | None:
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 8192):
        raise HistoryError("invalid-response-cursor" if response else "invalid-cursor")
    return value


def _page_params(thread_id: str, cursor: str | None, limit: int, sort_direction: str,
                 *, maximum: int) -> dict[str, Any]:
    identifier = _thread_id(thread_id)
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise HistoryError("invalid-page-limit")
    if sort_direction not in ("asc", "desc"):
        raise HistoryError("invalid-sort-direction")
    params: dict[str, Any] = {"threadId": identifier, "limit": limit, "sortDirection": sort_direction}
    if _cursor(cursor) is not None:
        params["cursor"] = cursor
    return params


def _history_page(result: dict[str, Any], limit: int, *, thread_id: str,
                  turn_id: str | None = None, items: bool) -> dict[str, Any]:
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) > limit:
        raise HistoryError("invalid-history-page")
    for row in rows:
        if not isinstance(row, dict):
            raise HistoryError("invalid-history-entry")
        if "threadId" in row and row["threadId"] != thread_id:
            raise HistoryError("thread-identity-mismatch")
        identifier = row.get("turnId" if items else "id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 512:
            raise HistoryError("invalid-turn-id")
        if items:
            if turn_id is not None and identifier != turn_id:
                raise HistoryError("turn-identity-mismatch")
            item = row.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not 1 <= len(item["id"]) <= 512:
                raise HistoryError("invalid-history-item")
            for field in ("startedAtMs", "completedAtMs"):
                value = row.get(field)
                if value is not None and (type(value) is not int or abs(value) > 2**53 - 1):
                    raise HistoryError("invalid-item-timestamp")
    return {
        "data": rows,
        "nextCursor": _cursor(result.get("nextCursor"), response=True),
        "backwardsCursor": _cursor(result.get("backwardsCursor"), response=True),
    }


def _metadata(thread: Any) -> dict[str, Any]:
    if not isinstance(thread, dict):
        raise HistoryError("invalid-thread-metadata")
    identifier = _thread_id(thread.get("id"))
    source = thread.get("source")
    source_type = source if isinstance(source, str) else None
    if isinstance(source, dict):
        source_type = source.get("type")
        if "subAgent" in source or "subagent" in source:
            source_type = "subAgent"
    status = thread.get("status")
    if isinstance(status, dict):
        status = status.get("type")
    # In particular, never return preview, turns, instructions, or arbitrary
    # nested source/status objects: those may contain message bodies.
    result: dict[str, Any] = {
        "id": identifier,
        "name": _bounded_text(thread.get("name"), 1024),
        "path": _bounded_text(thread.get("path"), 8192),
        "sourceType": source_type if isinstance(source_type, str) and source_type in _SOURCE_TYPES else "unknown",
        # Cold-reader runtime state is not evidence of the task lifecycle.
        "runtimeStatus": status if isinstance(status, str) and status in _RUNTIME_STATUSES else "unknown",
    }
    for field in ("createdAt", "updatedAt"):
        value = thread.get(field)
        result[field] = value if type(value) in (int, float) and abs(value) <= 2**53 - 1 and math.isfinite(value) else None
    return result


class HistoryAdapter:
    """One serialized stdio connection owned exclusively by this adapter.

    Use as a context manager or call close(). On a protocol, I/O, or timeout
    failure the connection is closed, so a delayed response cannot satisfy a
    later request. process_factory is only a dependency-injection seam for
    synthetic transport tests; production defaults to subprocess.Popen.
    """

    def __init__(
        self,
        executable: str,
        *,
        timeout_seconds: float = 10,
        max_response_bytes: int = 8 * 1024 * 1024,
        process_factory: Callable[..., Any] = subprocess.Popen,
    ):
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise HistoryError("absolute-executable-required")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be in (0, 120]")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 32 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 KiB and 32 MiB")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._lock = threading.RLock()
        self._process: Any = None
        self._selector: selectors.BaseSelector | None = None
        self._pending = b""
        self._sequence = 0
        self._closed = False
        self._initialized = False
        try:
            self._process = process_factory(
                [executable, "app-server"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            if self._process.stdin is None or self._process.stdout is None:
                raise HistoryError("missing-process-pipes")
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
            self._selector = selectors.DefaultSelector()
            self._selector.register(self._process.stdout, selectors.EVENT_READ)
            self._call("initialize", {
                "clientInfo": {"name": "codex_display", "version": "0.0.1"},
            })
            with self._lock:
                self._write({"method": "initialized", "params": {}}, time.monotonic() + self.timeout_seconds)
                self._initialized = True
        except Exception as error:
            self.close()
            if isinstance(error, (HistoryError, ValueError)):
                raise
            raise HistoryError("app-server-start-failed") from None

    def __enter__(self) -> HistoryAdapter:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _write(self, message: dict[str, Any], deadline: float) -> None:
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(data) > _MAX_REQUEST_BYTES:
            raise HistoryError("request-too-large")
        offset = 0
        with selectors.DefaultSelector() as writable:
            writable.register(self._process.stdin, selectors.EVENT_WRITE)
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise HistoryError("request-timeout")
                try:
                    count = os.write(self._process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
                if count == 0:
                    raise HistoryError("app-server-disconnected")
                offset += count

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in READ_METHODS:
            raise HistoryError("method-not-allowed")
        deadline = time.monotonic() + self.timeout_seconds
        if not self._lock.acquire(timeout=self.timeout_seconds):
            raise HistoryError("connection-busy-timeout")
        try:
            if self._closed:
                raise HistoryError("connection-closed")
            if method == "initialize" and self._sequence != 0:
                raise HistoryError("already-initialized")
            if method != "initialize" and not self._initialized:
                raise HistoryError("not-initialized")
            self._sequence += 1
            request_id = self._sequence
            self._write({"id": request_id, "method": method, "params": params}, deadline)
            consumed = messages = 0
            while True:
                while b"\n" in self._pending:
                    line, self._pending = self._pending.split(b"\n", 1)
                    consumed += len(line) + 1
                    messages += 1
                    if consumed > self.max_response_bytes or messages > _MAX_MESSAGES:
                        raise HistoryError("response-limit-exceeded")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        raise HistoryError("invalid-rpc-json") from None
                    if not isinstance(message, dict):
                        raise HistoryError("invalid-rpc-message")
                    if "method" in message:
                        if "id" in message:
                            identifier = message["id"]
                            if type(identifier) not in (int, str) or (isinstance(identifier, str) and len(identifier) > 256):
                                raise HistoryError("invalid-server-request-id")
                            self._write({"id": identifier, "error": {
                                "code": -32601, "message": "Read-only history observer",
                            }}, deadline)
                            raise HistoryError("unsolicited-server-request")
                        # Notifications do not establish execution state and are
                        # ignored within the same time/byte/message budget.
                        continue
                    if type(message.get("id")) is not int or message["id"] != request_id:
                        raise HistoryError("unexpected-response-id")
                    if ("error" in message) == ("result" in message):
                        raise HistoryError("invalid-rpc-response")
                    if "error" in message:
                        error = message["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        raise HistoryError("rpc-error", rpc_code=code if type(code) is int else None)
                    if not isinstance(message["result"], dict):
                        raise HistoryError("invalid-rpc-result")
                    return message["result"]
                if consumed + len(self._pending) > self.max_response_bytes:
                    raise HistoryError("response-limit-exceeded")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._selector is None or not self._selector.select(remaining):
                    raise HistoryError("request-timeout")
                try:
                    chunk = os.read(self._process.stdout.fileno(), min(65536, self.max_response_bytes + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    raise HistoryError("app-server-disconnected")
                self._pending += chunk
        except (OSError, ValueError, HistoryError) as error:
            self._close_unlocked()
            if isinstance(error, HistoryError):
                raise
            raise HistoryError("app-server-io-error") from None
        finally:
            self._lock.release()

    def list_threads(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HistoryError("invalid-page-limit")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 8192):
            raise HistoryError("invalid-cursor")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        result = self._call("thread/list", params)
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) > limit:
            raise HistoryError("invalid-thread-list")
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 8192):
            raise HistoryError("invalid-response-cursor")
        return {"data": [_metadata(row) for row in rows], "nextCursor": next_cursor}

    def read(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        identifier = _thread_id(thread_id)
        if include_turns is not False:
            raise HistoryError("metadata-only-read")
        result = self._call("thread/read", {"threadId": identifier, "includeTurns": False})
        thread = result.get("thread")
        # Identity is verified before projecting even one metadata field.
        if not isinstance(thread, dict) or thread.get("id") != identifier:
            raise HistoryError("thread-identity-mismatch")
        return _metadata(thread)

    def list_turns(self, thread_id: str, *, cursor: str | None = None, limit: int = 50,
                   sort_direction: str = "desc", items_view: str = "summary") -> dict[str, Any]:
        """Return official turn rows and opaque cursors in their original order."""
        params = _page_params(thread_id, cursor, limit, sort_direction, maximum=100)
        if items_view not in ("notLoaded", "summary", "full"):
            raise HistoryError("invalid-items-view")
        params["itemsView"] = items_view
        return _history_page(self._call("thread/turns/list", params), limit,
                             thread_id=thread_id, items=False)

    def list_items(self, thread_id: str, *, turn_id: str | None = None,
                   cursor: str | None = None, limit: int = 200,
                   sort_direction: str = "desc") -> dict[str, Any]:
        """Keep ThreadItemEntry's turnId/item/startedAtMs/completedAtMs wrapper.

        Unknown item types are retained here for the versioned projector to
        ignore. Cursors are opaque strings returned by the official API; this
        observer does not synthesize cursor offsets or item anchors.
        """
        params = _page_params(thread_id, cursor, limit, sort_direction, maximum=500)
        if turn_id is not None:
            if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 512:
                raise HistoryError("invalid-turn-id")
            params["turnId"] = turn_id
        return _history_page(self._call("thread/items/list", params), limit,
                             thread_id=thread_id, turn_id=turn_id, items=True)

    def read_history(self, thread_id: str, max_items: int = 200,
                     cursor: str | None = None) -> dict[str, Any]:
        """Read one bounded item page plus the corresponding recorded turns.

        Metadata identity is checked before body reads. The item continuation
        and reverse cursors are the exact upstream strings. Turn paging stops
        once all returned items' turns are found, or after ten bounded pages;
        turnsComplete exposes that bound without pretending missing metadata
        means there are no earlier turns. This is a historical read, not an
        atomic source snapshot or a live execution-status subscription.
        """
        _page_params(thread_id, cursor, max_items, "desc", maximum=500)
        thread = self.read(thread_id)
        page = self.list_items(thread_id, cursor=cursor, limit=max_items)
        wanted = {entry["turnId"] for entry in page["data"]}
        turns: list[dict[str, Any]] = []
        turn_cursor = None
        seen: set[str] = set()
        complete = False
        for _ in range(10):
            turn_page = self.list_turns(thread_id, cursor=turn_cursor, limit=100)
            turns.extend(turn_page["data"])
            wanted.difference_update(turn["id"] for turn in turn_page["data"])
            turn_cursor = turn_page["nextCursor"]
            complete = not wanted
            if complete or turn_cursor is None:
                break
            if turn_cursor in seen:
                raise HistoryError("repeated-history-cursor")
            seen.add(turn_cursor)
        return {"thread": thread, "items": page["data"], "turns": turns,
                "nextCursor": page["nextCursor"], "backwardsCursor": page["backwardsCursor"],
                "turnsNextCursor": turn_cursor, "turnsComplete": complete}

    def _close_unlocked(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending = b""
        if self._selector is not None:
            self._selector.close()
        process = self._process
        if process is None:
            return
        # Only this Popen handle is signalled; never process groups or a PID
        # obtained from Codex's files, desktop process, or a different client.
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()
