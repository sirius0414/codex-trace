"""真实本机子进程与平台无关导出回归；在 macOS 运行不构成 Windows 验收。"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4
import zipfile

from codex_trace.adapters.history import HistoryAdapter, HistoryError
from codex_trace.adapters.raw import RawAdapter
from codex_trace.export_store import ExportStore
from codex_trace.exports import ExportManager
from codex_trace.platform import filesystem as fs
from codex_trace.platform.locks import lock_exclusive, unlock
from codex_trace.platform.pipes import ThreadPipeTransport
from codex_trace.storage import DerivedStore
from codex_trace.raw_export import RawExportCollector


_THREAD = "11111111-1111-4111-8111-111111111111"
_PEER = '''import json, sys, time
for line in sys.stdin.buffer:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {}
    elif request["params"]["threadId"] == "22222222-2222-4222-8222-222222222222":
        time.sleep(10)
        continue
    else:
        result = {"thread": {"id": request["params"]["threadId"], "name": "中文任务"}}
    data = json.dumps({"id": request["id"], "result": result}, ensure_ascii=False).encode() + b"\\n"
    sys.stdout.buffer.write(data[:3]); sys.stdout.buffer.flush()
    sys.stdout.buffer.write(data[3:]); sys.stdout.buffer.flush()
'''


def _publish_worker(root, source, candidate, ready, start, output):
    """spawn 子进程各自打开来源，竞争同一已持久化候选名。"""
    fd = fs.open_read(Path(source))
    try:
        ready.put(True)
        start.wait(10)
        result = ExportStore(Path(root)).publish_file(fd, candidate)
        output.put(("ok", result))
    except BaseException as error:
        output.put(("error", type(error).__name__))
    finally:
        fs.close(fd)


def _try_lock(path):
    fd = fs.open(path, fs.O_RDWR | fs.O_NOFOLLOW)
    try:
        try:
            lock_exclusive(fd, blocking=False)
        except BlockingIOError:
            return "busy"
        unlock(fd)
        return "acquired"
    finally:
        fs.close(fd)


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex trace 中文 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def peer(self, **kwargs):
        path = self.root / "只读 peer.py"
        path.write_text(_PEER, encoding="utf-8")
        client = HistoryAdapter(str(path), transport_factory=ThreadPipeTransport, **kwargs)
        self.addCleanup(client.close)
        return client

    def test_threaded_pipe_real_child_handles_unicode_spaces_and_fragmented_utf8(self):
        client = self.peer()
        self.assertEqual(client.read(_THREAD)["name"], "中文任务")
        process, transport = client._process, client._transport
        client.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(any(thread.is_alive() for thread in transport.threads))

    def test_threaded_pipe_timeout_discards_and_reaps_connection(self):
        client = self.peer(timeout_seconds=.3)
        started = time.monotonic()
        with self.assertRaises(HistoryError) as caught:
            client.read("22222222-2222-4222-8222-222222222222")
        self.assertEqual(caught.exception.code, "request-timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(client._process.poll())
        self.assertFalse(any(thread.is_alive() for thread in client._transport.threads))
        with self.assertRaises(HistoryError):
            client.read(_THREAD)

    def test_threaded_pipe_backpressure_has_deadline_and_reaps_writer(self):
        path = self.root / "不读管道.py"
        path.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        transport = ThreadPipeTransport(process.stdin, process.stdout)
        try:
            with self.assertRaises(TimeoutError):
                transport.write(b"x" * (4 * 1024 * 1024), time.monotonic() + .15)
        finally:
            process.kill()
            process.wait(timeout=3)
            transport.close()
            process.stdin.close()
            process.stdout.close()
        self.assertFalse(any(thread.is_alive() for thread in transport.threads))

    def test_native_lock_coordinates_spawn_process_and_releases(self):
        path = self.root / "进程 lock"
        fd = fs.open(path, fs.O_RDWR | fs.O_CREAT | fs.O_EXCL | fs.O_NOFOLLOW, 0o600)
        context = multiprocessing.get_context("spawn")
        try:
            lock_exclusive(fd)
            with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
                self.assertEqual(pool.submit(_try_lock, str(path)).result(timeout=10), "busy")
                unlock(fd)
                self.assertEqual(pool.submit(_try_lock, str(path)).result(timeout=10), "acquired")
        finally:
            fs.close(fd)

    def test_unicode_state_export_concurrency_no_overwrite_and_readback(self):
        state = self.root / "派生 数据"
        with DerivedStore(state) as store:
            original_id = store.source_instance_id
            self.assertTrue(store.put_receipt("中文 receipt", {"state": "pending"})["saved"])
        with DerivedStore(state) as store:
            self.assertEqual(store.source_instance_id, original_id)
            self.assertEqual(store.get_receipt("中文 receipt")["value"], {"state": "pending"})
        source = self.root / "冻结 原始.zip"
        payload = bytes(range(256)) * 2048
        source.write_bytes(payload)
        before = source.stat()
        destination = ExportStore(self.root / "导出 目录")
        # 首次建立私有目录，使竞争仅发生在同一候选的无覆盖发布阶段。
        directory = destination._open_directory()
        fs.close(directory)
        candidate = destination.reserve(len(payload), hashlib.sha256(payload).hexdigest())
        context = multiprocessing.get_context("spawn")
        ready, output, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_publish_worker, args=(str(destination.root), str(source), candidate, ready, start, output)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            results = [output.get(timeout=15) for _ in processes]
            self.assertTrue(all(status == "ok" for status, _ in results), results)
            self.assertEqual(results[0][1], results[1][1])
            saved = Path(candidate["path"])
            self.assertEqual(saved.read_bytes(), payload)
            self.assertEqual(len(list(destination.root.glob("*.zip"))), 1)
            self.assertTrue(destination.verify_saved(candidate)["verified"])
            saved.write_bytes(b"existing changed file")
            fd = fs.open_read(source)
            try:
                with self.assertRaises(ValueError):
                    destination.publish_file(fd, candidate)
            finally:
                fs.close(fd)
            self.assertEqual(saved.read_bytes(), b"existing changed file")
            self.assertEqual(source.read_bytes(), payload)
            after = source.stat()
            self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        finally:
            for process in processes:
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
            for item in (ready, output):
                item.close()
                item.join_thread()

    def test_raw_zip_and_durable_same_request_preserve_chinese_source(self):
        sessions = self.root / "中文 会话"
        sessions.mkdir()
        source = sessions / f"rollout-2026-10-28T22-00-00-{_THREAD}.jsonl"
        header = {"type": "session_meta", "payload": {"id": _THREAD}, "timestamp": "2026-10-28T14:00:00Z"}
        payload = json.dumps(header, ensure_ascii=False).encode() + b"\n" + '未知原始字节\r\n'.encode()
        source.write_bytes(payload)
        before = source.stat()

        class History:
            def read(self, identifier):
                return {"id": identifier, "path": str(source)}

            def list_items(self, identifier, **kwargs):
                return {"data": [], "nextCursor": None}

        raw = RawAdapter([sessions])
        store = DerivedStore(self.root / "状态 空格")
        destination = ExportStore(self.root / "Downloads" / "Codex Trace")
        destination.root.parent.mkdir()
        collector = RawExportCollector(History(), raw)
        manager = ExportManager(store, collector, destination)
        create_key, save_key = str(uuid4()), str(uuid4())
        binding = {"schemaVersion": "1.0.0", "sourceInstanceId": store.source_instance_id,
                   "viewSessionId": "portable-view", "selectionEpoch": 1, "threadId": _THREAD}
        try:
            created = manager.create(binding, create_key)
            self.assertEqual(created["status"], "ready", created)
            saved = manager.commit(binding, created["exportId"], save_key)
            self.assertEqual(saved["save"]["status"], "verified", saved)
            self.assertEqual(manager.create(binding, create_key)["exportId"], created["exportId"])
            retried = manager.commit(binding, created["exportId"], save_key)["save"]
            self.assertEqual({key: value for key, value in retried.items() if key != "verifiedAt"},
                             {key: value for key, value in saved["save"].items() if key != "verifiedAt"})
            manager.close()
            store.close()
            store = DerivedStore(self.root / "状态 空格")
            manager = ExportManager(store, collector, destination)
            recovered = manager.result(binding, save_key)["save"]
            self.assertEqual({key: value for key, value in recovered.items() if key != "verifiedAt"},
                             {key: value for key, value in saved["save"].items() if key != "verifiedAt"})
            self.assertEqual(len(list(destination.root.glob("*.zip"))), 1)
            with zipfile.ZipFile(saved["save"]["path"]) as archive:
                records = [name for name in archive.namelist() if name.startswith("records/")]
                self.assertEqual(len(records), 1)
                self.assertEqual(archive.read(records[0]), payload)
            self.assertEqual(source.read_bytes(), payload)
            self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
        finally:
            manager.close()
            store.close()
            raw.close()


if __name__ == "__main__":
    unittest.main()
