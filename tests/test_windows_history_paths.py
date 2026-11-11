"""History RPC → raw → saved ZIP; Windows aliases require native Windows.

The child process serves only synthetic metadata and never starts Codex or reads
real sessions. Passing the portable case does not establish Windows acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4
import zipfile

from codex_trace.adapters.history import HistoryAdapter
from codex_trace.adapters.raw import RawAdapter
from codex_trace.export_store import ExportStore
from codex_trace.exports import ExportManager
from codex_trace.platform import filesystem as fs
from codex_trace.raw_export import RawExportCollector
from codex_trace.storage import DerivedStore


_MAIN = "11111111-1111-4111-8111-111111111111"
_CHILD = "22222222-2222-4222-8222-222222222222"
_PEER = '''import json, sys
from pathlib import Path
fixture = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
for line in sys.stdin.buffer:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        result = {}
    elif method == "thread/read":
        result = {"thread": fixture["metadata"][params["threadId"]]}
    elif method == "thread/list":
        result = {"data": list(fixture["metadata"].values()), "nextCursor": None}
    elif method == "thread/items/list":
        result = {"data": fixture["items"].get(params["threadId"], []), "nextCursor": None}
    else:
        raise RuntimeError("unexpected synthetic read method")
    answer = {"id": request["id"], "result": result}
    sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=False).encode() + b"\\n")
    sys.stdout.buffer.flush()
'''


class HistoryPathRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex history 中文 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root / "中文 会话"
        self.archived = self.root / "归档 会话"
        self.sessions.mkdir()
        self.archived.mkdir()

    def source(self, identifier, directory):
        path = directory / f"rollout-2026-11-10T12-00-00-{identifier}.jsonl"
        rows = [
            {"type": "session_meta", "ordinal": 0,
             "payload": {"id": identifier, "history_mode": "paginated"}},
            {"type": "response_item", "ordinal": 1,
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "中文 原始内容 " + identifier}]}},
        ]
        payload = b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\r\n" for row in rows)
        path.write_bytes(payload)
        return path

    def peer(self, paths, *, child=False):
        script = self.root / f"只读 历史 {uuid4()}.py"
        items = {_MAIN: [{"turnId": "synthetic-turn", "startedAtMs": 0, "completedAtMs": 1,
                          "item": {"type": "subAgentActivity", "id": "synthetic-spawn",
                                   "agentThreadId": _CHILD, "agentPath": "/root/child", "kind": "started"}}]} if child else {}
        script.write_text(_PEER, encoding="utf-8")
        script.with_suffix(".json").write_text(json.dumps({
            "metadata": {identifier: {"id": identifier, "path": path} for identifier, path in paths.items()},
            "items": items,
        }, ensure_ascii=False), encoding="utf-8")
        history = HistoryAdapter(str(script), timeout_seconds=5)
        self.addCleanup(history.close)
        return history

    def raw(self):
        raw = RawAdapter([self.sessions, self.archived])
        self.addCleanup(raw.close)
        return raw

    def exports(self, history, raw):
        case_root = self.root / str(uuid4())
        case_root.mkdir()
        store = DerivedStore(case_root / "派生 状态")
        self.addCleanup(store.close)
        destination = ExportStore(case_root / "保存 ZIP")
        manager = ExportManager(store, RawExportCollector(history, raw), destination)
        self.addCleanup(manager.close)
        binding = {"schemaVersion": "1.0.0", "sourceInstanceId": store.source_instance_id,
                   "viewSessionId": "history-path-runtime", "selectionEpoch": 1, "threadId": _MAIN}
        return manager, destination, binding

    def assert_saved_sources(self, *, verbatim):
        paths = {_MAIN: self.source(_MAIN, self.sessions), _CHILD: self.source(_CHILD, self.archived)}
        originals = {identifier: (path.read_bytes(), path.stat().st_mtime_ns) for identifier, path in paths.items()}
        api_paths = {identifier: ("\\\\?\\" if verbatim else "") + str(path) for identifier, path in paths.items()}
        history = self.peer(api_paths, child=True)
        raw = self.raw()
        self.assertEqual({row["id"]: row["path"] for row in history.list_threads()["data"]},
                         {identifier: str(path) for identifier, path in paths.items()})
        for identifier, path in paths.items():
            metadata = history.read(identifier)
            self.assertEqual(metadata["path"], str(path))
            snapshot = raw.read(identifier, metadata)
            self.assertEqual(snapshot.issues, [])
            self.assertEqual(b"".join(row["raw"] for row in snapshot.records), originals[identifier][0])
            self.assertTrue(all(source["state"] == "readable" for source in snapshot.sources))
        manager, destination, binding = self.exports(history, raw)
        create_key, save_key = str(uuid4()), str(uuid4())
        created = manager.create(binding, create_key)
        self.assertEqual(created["status"], "ready", created)
        saved = manager.commit(binding, created["exportId"], save_key)
        self.assertEqual(saved["save"]["status"], "verified", saved)
        saved_path = Path(saved["save"]["path"])
        zip_bytes = saved_path.read_bytes()
        self.assertEqual(saved["save"]["bytes"], len(zip_bytes))
        self.assertEqual(saved["save"]["sha256"], hashlib.sha256(zip_bytes).hexdigest())
        self.assertEqual(manager.create(binding, create_key)["exportId"], created["exportId"])
        self.assertEqual(manager.result(binding, save_key)["save"]["status"], "verified")
        self.assertEqual(len(list(destination.root.glob("*.zip"))), 1)
        with zipfile.ZipFile(saved_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(manifest["enumerationComplete"], manifest)
            self.assertEqual({source["threadId"] for source in manifest["sources"]}, set(paths))
            self.assertEqual(len(manifest["sources"]), 2)
            self.assertTrue(any(edge["type"] == "spawned-by" and edge["resolution"] == "confirmed"
                                and edge["fromThreadId"] == _CHILD and edge["toThreadId"] == _MAIN
                                for edge in manifest["relationships"]))
            for source in manifest["sources"]:
                content = archive.read(source["entryName"])
                self.assertEqual(content, originals[source["threadId"]][0])
                self.assertEqual(source["sha256"], hashlib.sha256(content).hexdigest())
            self.assertNotIn(str(self.root), json.dumps(manifest))
        for identifier, path in paths.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), originals[identifier])

        if verbatim:
            # Only the trusted history boundary accepts this alias; raw and the
            # generic filesystem interface must keep rejecting device paths.
            direct = self.raw()
            rejected = direct.read(_MAIN, {"id": _MAIN, "path": api_paths[_MAIN]})
            self.assertEqual(rejected.records, [])
            self.assertIn("outside-session-roots", rejected.issues)
            self.assertEqual(direct.metrics["readBytes"], 0)
            with self.assertRaises(ValueError):
                fs.open_read(api_paths[_MAIN])

    def assert_rejected_export(self, path):
        history = self.peer({_MAIN: path})
        raw = self.raw()
        metadata = history.read(_MAIN)
        self.assertTrue(metadata["path"], "rejected paths must not become missing-path fallbacks")
        snapshot = raw.read(_MAIN, metadata)
        self.assertEqual(snapshot.records, [])
        self.assertEqual(snapshot.segments, [])
        self.assertTrue(snapshot.issues)
        self.assertTrue(all(source["state"] == "unreadable" for source in snapshot.sources))
        self.assertEqual(raw.metrics["readBytes"], 0)
        manager, destination, binding = self.exports(history, raw)
        request_id = str(uuid4())
        created = manager.create(binding, request_id)
        self.assertEqual(created["status"], "failed", created)
        self.assertEqual(created["save"]["status"], "notRequested")
        self.assertEqual(manager.status(binding, created["exportId"])["status"], "failed")
        self.assertEqual(manager.create(binding, request_id)["exportId"], created["exportId"])
        self.assertEqual(raw.metrics["readBytes"], 0)
        self.assertFalse(list(destination.root.glob("*.zip")))
        self.assertFalse(list(manager.root.glob("*.zip")))

    def test_history_rpc_to_raw_and_saved_zip_with_ordinary_paths(self):
        self.assert_saved_sources(verbatim=False)

    def test_history_outside_ordinary_root_never_reads_or_saves(self):
        self.source(_MAIN, self.sessions)
        outside = self.source(_MAIN, self.root)
        self.assert_rejected_export(str(outside))

    @unittest.skipUnless(os.name == "nt", "requires native Windows path and handle semantics")
    def test_history_verbatim_main_and_child_paths_export_exact_bytes(self):
        self.assert_saved_sources(verbatim=True)

    @unittest.skipUnless(os.name == "nt", "requires native Windows path and handle semantics")
    def test_history_outside_and_invalid_aliases_never_read_or_save(self):
        source = self.source(_MAIN, self.sessions)
        outside = self.source(_MAIN, self.root)
        prefix = "\\\\?\\"
        cases = [
            prefix + str(outside),
            prefix + str(source.parent) + "\\.\\" + source.name,
            prefix + str(source.parent) + "\\..\\" + source.name,
            prefix + str(source.parent) + "\\\\" + source.name,
            prefix + str(source).replace("\\", "/"),
            prefix + str(source) + ":stream",
            prefix + str(source) + ".",
            prefix + str(source) + " ",
            prefix + str(source.parent / "NUL"),
            "\\\\?\\UNC\\server\\share\\" + source.name,
            "\\\\.\\" + str(source),
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assert_rejected_export(path)

    @unittest.skipUnless(os.name == "nt", "requires native Windows directory junctions")
    def test_history_alias_through_junction_remains_unreadable(self):
        source = self.source(_MAIN, self.root)
        # A valid same-owner source under the allowed root makes an accidental
        # conversion to a missing path observable as a failed rejection.
        self.source(_MAIN, self.sessions)
        junction = self.sessions / "目录 联接"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(self.root)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, "native junction fixture could not be created")
        self.addCleanup(junction.rmdir)
        self.assert_rejected_export("\\\\?\\" + str(junction / source.name))


if __name__ == "__main__":
    unittest.main()
