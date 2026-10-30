"""Synthetic stdio tests: never connect to Codex or read real sessions."""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.codex_trace.adapters.history import HistoryAdapter, HistoryError


THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"
EXECUTABLE = str(Path(tempfile.gettempdir()).resolve() / "synthetic-codex.exe")


class FakeProcess:
    """Popen-compatible pipes with a synthetic, non-Codex peer."""

    def __init__(self, handler):
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()
        self.stdin = os.fdopen(write_in, "wb", buffering=0)
        self.stdout = os.fdopen(read_out, "rb", buffering=0)
        self._read_in, self._write_out = read_in, write_out
        self.handler = handler
        self.requests = []
        self.terminated = self.killed = 0
        self.stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        pending = b""
        try:
            # 阻塞读取与被测实现无关，在 Windows 上也无需用 select 轮询匿名管道。
            while not self.stop.is_set():
                chunk = os.read(self._read_in, 65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    request = json.loads(line)
                    self.requests.append(request)
                    if request.get("method") == "initialize":
                        answer = {"id": request["id"], "result": {"userAgent": "synthetic"}}
                    else:
                        answer = self.handler(request)
                    if answer is None:
                        continue
                    payload = answer if isinstance(answer, bytes) else json.dumps(answer).encode() + b"\n"
                    os.write(self._write_out, payload)
        except (OSError, ValueError):
            pass
        finally:
            os.close(self._read_in)
            os.close(self._write_out)

    def poll(self):
        return 0 if not self._thread.is_alive() else None

    def terminate(self):
        self.terminated += 1
        self.stop.set()
        self.stdin.close()  # EOF 唤醒合成对端的阻塞读取，等价于终止自有子进程。

    def kill(self):
        self.killed += 1
        self.stop.set()
        self.stdin.close()

    def wait(self, timeout=None):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired("synthetic-peer", timeout)
        return 0


class HistoryTests(unittest.TestCase):
    def adapter(self, handler=None, **kwargs):
        if handler is None:
            handler = lambda message: {"id": message["id"], "result": {"thread": {
                "id": message["params"]["threadId"], "name": "Synthetic task",
            }}} if "id" in message else None
        self.process = FakeProcess(handler)
        self.addCleanup(self.process.terminate)
        self.addCleanup(self.process.wait, 1)
        self.calls = []

        def create(*args, **options):
            self.calls.append((args, options))
            return self.process

        client = HistoryAdapter(EXECUTABLE, process_factory=create, **kwargs)
        self.addCleanup(client.close)
        return client

    def assert_error(self, code, operation):
        with self.assertRaises(HistoryError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("secret", str(caught.exception))
        return caught.exception

    def test_read_verifies_identity_and_projects_only_minimal_metadata(self):
        def handler(message):
            if "id" not in message:
                return None
            self.assertEqual(message["method"], "thread/read")
            self.assertEqual(message["params"], {"threadId": THREAD_A, "includeTurns": False})
            return {"id": message["id"], "result": {"thread": {
                "id": THREAD_A, "name": "Synthetic name", "path": "/synthetic/sessions/task.jsonl",
                "source": {"type": "cli", "hidden": "secret"},
                "status": {"type": "notLoaded", "hidden": "secret"},
                "createdAt": 0, "updatedAt": 123.5, "preview": "secret",
                "turns": [{"text": "secret"}], "baseInstructions": "secret",
            }}}
        client = self.adapter(handler)
        result = client.read(THREAD_A)
        self.assertEqual(result, {
            "id": THREAD_A, "name": "Synthetic name", "path": "/synthetic/sessions/task.jsonl",
            "sourceType": "cli", "runtimeStatus": "notLoaded", "createdAt": 0, "updatedAt": 123.5,
        })
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.calls[0][0][0], [EXECUTABLE, "app-server"])
        self.assertEqual(self.calls[0][1]["stderr"], subprocess.DEVNULL)
        self.assertEqual([r["method"] for r in self.process.requests], ["initialize", "initialized", "thread/read"])

    def test_list_paging_is_bounded_and_does_not_return_preview_or_unknown_source(self):
        def handler(message):
            if "id" not in message:
                return None
            self.assertEqual(message["params"], {"limit": 1, "cursor": "opaque"})
            return {"id": message["id"], "result": {"data": [{
                "id": THREAD_A, "preview": "secret", "source": "secret", "status": "secret",
            }], "nextCursor": "next-opaque"}}
        result = self.adapter(handler).list_threads(limit=1, cursor="opaque")
        self.assertEqual(result["nextCursor"], "next-opaque")
        self.assertEqual(result["data"][0]["sourceType"], "unknown")
        self.assertEqual(result["data"][0]["runtimeStatus"], "unknown")
        self.assertNotIn("secret", json.dumps(result))

    def test_mismatched_thread_never_returns_another_tasks_metadata(self):
        client = self.adapter(lambda m: {"id": m["id"], "result": {"thread": {
            "id": THREAD_B, "name": "secret",
        }}} if "id" in m else None)
        self.assert_error("thread-identity-mismatch", lambda: client.read(THREAD_A))

    def test_invalid_ids_and_body_reads_never_reach_peer(self):
        client = self.adapter()
        for value in ("../../secret", "", None, 0, THREAD_A + " "):
            self.assert_error("invalid-thread-id", lambda: client.read(value))
        self.assert_error("metadata-only-read", lambda: client.read(THREAD_A, include_turns=True))
        for value in (0, 101, True):
            self.assert_error("invalid-page-limit", lambda: client.list_threads(limit=value))
        self.assert_error("invalid-cursor", lambda: client.list_threads(cursor=""))
        self.assertFalse(any(r.get("method", "").startswith("thread/") for r in self.process.requests))

    def test_mutation_methods_and_raw_initialized_request_are_rejected(self):
        client = self.adapter()
        for method in ("thread/resume", "thread/start", "thread/interrupt", "thread/compact", "initialized", "turn/start"):
            self.assert_error("method-not-allowed", lambda: client._call(method, {}))
        self.assertFalse(any(r.get("method") == "thread/resume" for r in self.process.requests))
        self.assertEqual(client.read(THREAD_A)["id"], THREAD_A)

    def test_unsolicited_execution_request_is_denied_and_connection_closed(self):
        client = self.adapter(lambda m: {"id": "approval-1", "method": "item/commandExecution/requestApproval",
                                         "params": {"command": "secret"}} if "id" in m else None)
        with patch.object(client, "_write", wraps=client._write) as writes:
            self.assert_error("unsolicited-server-request", lambda: client.read(THREAD_A))
            self.assertEqual(writes.call_args_list[-1].args[0], {
                "id": "approval-1", "error": {"code": -32601, "message": "Read-only history observer"},
            })
        self.assertEqual(self.process.terminated, 1)
        self.assert_error("connection-closed", lambda: client.read(THREAD_A))
        # The denying response is written before the owned process is closed.
        self.assertTrue(client._closed)

    def test_rpc_error_text_is_never_exposed(self):
        client = self.adapter(lambda m: {"id": m["id"], "error": {
            "code": -32000, "message": "secret", "data": {"text": "secret"},
        }} if "id" in m else None)
        error = self.assert_error("rpc-error", lambda: client.read(THREAD_A))
        self.assertEqual(error.rpc_code, -32000)
        self.assertEqual(self.process.terminated, 1)

    def test_timeout_closes_owned_process_and_cannot_reuse_late_response(self):
        client = self.adapter(lambda _: None, timeout_seconds=0.03)
        started = time.monotonic()
        self.assert_error("request-timeout", lambda: client.read(THREAD_A))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.process.terminated, 1)
        self.assert_error("connection-closed", lambda: client.read(THREAD_A))

    def test_unterminated_oversized_response_is_bounded(self):
        client = self.adapter(lambda m: b"x" * 2048 if "id" in m else None, max_response_bytes=1024)
        self.assert_error("response-limit-exceeded", lambda: client.read(THREAD_A))

    def test_complete_oversized_response_is_bounded(self):
        client = self.adapter(lambda m: {"id": m["id"], "result": {"body": "x" * 2048}}
                              if "id" in m else None, max_response_bytes=1024)
        self.assert_error("response-limit-exceeded", lambda: client.read(THREAD_A))

    def test_partial_utf8_response_waits_until_complete(self):
        def handler(message):
            if "id" not in message:
                return None
            payload = json.dumps({"id": message["id"], "result": {"thread": {
                "id": THREAD_A, "name": "合成任务",
            }}}, ensure_ascii=False).encode() + b"\n"
            split = payload.index("合".encode()) + 1
            os.write(self.process._write_out, payload[:split])
            time.sleep(0.01)
            return payload[split:]
        self.assertEqual(self.adapter(handler).read(THREAD_A)["name"], "合成任务")

    def test_timestamp_boolean_and_nonfinite_values_are_missing(self):
        client = self.adapter(lambda m: {"id": m["id"], "result": {"thread": {
            "id": THREAD_A, "createdAt": True, "updatedAt": 10**400,
        }}} if "id" in m else None)
        result = client.read(THREAD_A)
        self.assertIsNone(result["createdAt"])
        self.assertIsNone(result["updatedAt"])

    def test_notification_flood_is_bounded(self):
        client = self.adapter(lambda m: b'{"method":"note"}\n' * 257 if "id" in m else None)
        self.assert_error("response-limit-exceeded", lambda: client.read(THREAD_A))

    def test_malformed_json_and_wrong_response_id_are_rejected(self):
        for payload, expected in ((b"secret\n", "invalid-rpc-json"),
                                  (b'{"id":true,"result":{}}\n', "unexpected-response-id"),
                                  (b'[]\n', "invalid-rpc-message")):
            with self.subTest(expected=expected):
                client = self.adapter(lambda m: payload if "id" in m else None)
                self.assert_error(expected, lambda: client.read(THREAD_A))

    def test_concurrent_reads_are_serialized_and_remain_bound_to_identity(self):
        def handler(message):
            if "id" not in message:
                return None
            time.sleep(0.01)
            return {"id": message["id"], "result": {"thread": {"id": message["params"]["threadId"]}}}
        client = self.adapter(handler)
        identifiers = [THREAD_A, THREAD_B] * 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(client.read, identifiers))
        self.assertEqual([r["id"] for r in results], identifiers)
        ids = [r["id"] for r in self.process.requests if "id" in r]
        self.assertEqual(ids, list(range(1, 22)))

    def test_close_is_idempotent_and_closes_only_owned_handle(self):
        client = self.adapter()
        client.close()
        client.close()
        self.assertEqual(self.process.terminated, 1)
        self.assertEqual(self.process.killed, 0)
        self.assertTrue(self.process.stdin.closed)
        self.assertTrue(self.process.stdout.closed)

    def test_transport_cleanup_failure_keeps_protocol_error_and_closes_both_streams(self):
        client = self.adapter(lambda message: b"invalid-rpc-json\n" if "id" in message else None)
        original_close = client._transport.close
        def close_then_fail():
            original_close()
            raise OSError("PRIVATE_CLEANUP_ERROR")
        with patch.object(client._transport, "close", side_effect=close_then_fail):
            self.assert_error("invalid-rpc-json", lambda: client.read(THREAD_A))
        self.assertTrue(self.process.stdin.closed)
        self.assertTrue(self.process.stdout.closed)
        self.assertIsNotNone(self.process.poll())

    def test_executable_must_be_absolute(self):
        self.assert_error("absolute-executable-required", lambda: HistoryAdapter("codex"))

    def test_item_paging_preserves_opaque_cursors_wrapper_and_unknown_types(self):
        entry = {"turnId": "turn-7", "item": {"id": "item-4", "type": "futureType", "synthetic": True},
                 "startedAtMs": 0, "completedAtMs": 1234}
        def handler(message):
            if "id" not in message:
                return None
            self.assertEqual(message["method"], "thread/items/list")
            self.assertEqual(message["params"], {"threadId": THREAD_A, "turnId": "turn-7",
                "limit": 1, "sortDirection": "desc", "cursor": "opaque:not-an-index"})
            return {"id": message["id"], "result": {"data": [entry], "nextCursor": "next/from/api", "backwardsCursor": "reverse/api"}}
        page = self.adapter(handler).list_items(THREAD_A, turn_id="turn-7", limit=1, cursor="opaque:not-an-index")
        self.assertEqual(page, {"data": [entry], "nextCursor": "next/from/api", "backwardsCursor": "reverse/api"})

    def test_turn_paging_forwards_direction_items_view_and_missing_reverse_cursor(self):
        turn = {"id": "turn-3", "status": "completed", "startedAt": 1, "completedAt": 2, "durationMs": 1000}
        def handler(message):
            if "id" not in message:
                return None
            self.assertEqual(message["method"], "thread/turns/list")
            self.assertEqual(message["params"], {"threadId": THREAD_A, "limit": 1,
                "sortDirection": "asc", "itemsView": "summary"})
            return {"id": message["id"], "result": {"data": [turn], "nextCursor": None}}
        self.assertEqual(self.adapter(handler).list_turns(THREAD_A, limit=1, sort_direction="asc"),
                         {"data": [turn], "nextCursor": None, "backwardsCursor": None})

    def test_history_checks_identity_first_and_follows_actual_turn_cursor(self):
        def handler(message):
            if "id" not in message:
                return None
            method, params = message["method"], message["params"]
            if method == "thread/read":
                result = {"thread": {"id": THREAD_A}}
            elif method == "thread/items/list":
                result = {"data": [{"turnId": "older", "item": {"id": "item-1", "type": "agentMessage"}}],
                          "nextCursor": "next-items", "backwardsCursor": "reverse-items"}
            elif "cursor" not in params:
                result = {"data": [{"id": "newer"}], "nextCursor": "next-turn-from-server"}
            else:
                self.assertEqual(params["cursor"], "next-turn-from-server")
                result = {"data": [{"id": "older"}], "nextCursor": "older-turns"}
            return {"id": message["id"], "result": result}
        result = self.adapter(handler).read_history(THREAD_A, max_items=2)
        self.assertEqual(result["nextCursor"], "next-items")
        self.assertEqual(result["backwardsCursor"], "reverse-items")
        self.assertEqual(result["turnsNextCursor"], "older-turns")
        self.assertTrue(result["turnsComplete"])
        self.assertEqual([turn["id"] for turn in result["turns"]], ["newer", "older"])
        self.assertEqual([r["method"] for r in self.process.requests][2:],
                         ["thread/read", "thread/items/list", "thread/turns/list", "thread/turns/list"])

    def test_history_body_is_not_requested_after_identity_mismatch(self):
        client = self.adapter(lambda message: {"id": message["id"], "result": {"thread": {"id": THREAD_B}}}
                              if "id" in message else None)
        self.assert_error("thread-identity-mismatch", lambda: client.read_history(THREAD_A))
        self.assertFalse(any(r.get("method") == "thread/items/list" for r in self.process.requests))

    def test_history_does_not_infer_complete_from_empty_page_or_missing_turn(self):
        def handler(message):
            if "id" not in message:
                return None
            if message["method"] == "thread/read":
                result = {"thread": {"id": THREAD_A}}
            elif message["method"] == "thread/items/list":
                result = {"data": [{"turnId": "not-recorded", "item": {"id": "x", "type": "future"}}], "nextCursor": "opaque"}
            else:
                result = {"data": [], "nextCursor": None}
            return {"id": message["id"], "result": result}
        result = self.adapter(handler).read_history(THREAD_A)
        self.assertEqual(result["nextCursor"], "opaque")
        self.assertFalse(result["turnsComplete"])

    def test_history_validation_rejects_invalid_page_parameters_before_rpc(self):
        client = self.adapter()
        for operation in (lambda: client.list_items(THREAD_A, limit=501),
                          lambda: client.list_items(THREAD_A, sort_direction="newest"),
                          lambda: client.list_items(THREAD_A, cursor={"index": 4}),
                          lambda: client.list_items(THREAD_A, turn_id=""),
                          lambda: client.list_turns(THREAD_A, items_view="all"),
                          lambda: client.read_history(THREAD_A, max_items=True)):
            with self.assertRaises(HistoryError):
                operation()
        self.assertFalse(any(r.get("method", "").startswith("thread/") for r in self.process.requests))

    def test_wrong_thread_or_turn_and_invalid_item_timestamps_are_rejected(self):
        good = {"turnId": "t", "item": {"id": "i", "type": "future"}, "startedAtMs": None}
        cases = (({**good, "threadId": THREAD_B}, "thread-identity-mismatch"),
                 ({**good, "turnId": "other"}, "turn-identity-mismatch"),
                 ({**good, "startedAtMs": True}, "invalid-item-timestamp"),
                 ({**good, "item": {}}, "invalid-history-item"))
        for row, error in cases:
            with self.subTest(error=error):
                client = self.adapter(lambda m: {"id": m["id"], "result": {"data": [row]}} if "id" in m else None)
                self.assert_error(error, lambda: client.list_items(THREAD_A, turn_id="t"))

    def test_repeated_turn_cursor_cannot_spin_history_reader(self):
        def handler(message):
            if "id" not in message:
                return None
            if message["method"] == "thread/read":
                result = {"thread": {"id": THREAD_A}}
            elif message["method"] == "thread/items/list":
                result = {"data": [{"turnId": "missing", "item": {"id": "x"}}]}
            else:
                result = {"data": [], "nextCursor": "same"}
            return {"id": message["id"], "result": result}
        self.assert_error("repeated-history-cursor", lambda: self.adapter(handler).read_history(THREAD_A))


if __name__ == "__main__":
    unittest.main()
