"""All persistence tests use isolated synthetic plugin directories."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import UUID

from backend.codex_trace import storage
from backend.codex_trace.storage import DerivedStore, StorageError
from platform_fixtures import assert_private_mode, symlink


STATE = json.loads((Path(__file__).parent / "fixtures/contracts/view-state.json").read_text(encoding="utf-8"))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "plugin" / "index"
        self.store = DerivedStore(self.root)
        self.addCleanup(self.store.close)

    def reopen(self):
        self.store.close()
        self.store = DerivedStore(self.root)
        self.addCleanup(self.store.close)
        return self.store

    def test_private_permissions_and_stable_identity_after_restart(self):
        identifier = self.store.source_instance_id
        UUID(identifier)
        assert_private_mode(self, self.root, 0o700)
        assert_private_mode(self, self.store.path, 0o600)
        self.assertEqual(self.reopen().source_instance_id, identifier)
        self.assertEqual(self.store._connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_separate_instances_share_identity_and_cross_process_cas(self):
        with DerivedStore(self.root) as other:
            self.assertEqual(other.source_instance_id, self.store.source_instance_id)
            results = list(ThreadPoolExecutor(2).map(lambda store: store.save_view("A", 0, STATE), (self.store, other)))
            self.assertEqual(sorted(result["saved"] for result in results), [False, True])
            self.assertEqual(self.store.load_view("A")["preferenceRevision"], 1)

    def test_projection_requires_exact_thread_generation_parser_version(self):
        self.store.save_projection("A", 3, "parser-1", {"events": [{"id": "synthetic"}]})
        self.assertEqual(self.store.load_projection("A", 3, "parser-1")["events"][0]["id"], "synthetic")
        for key in (("B", 3, "parser-1"), ("A", 4, "parser-1"), ("A", 3, "parser-2")):
            self.assertIsNone(self.store.load_projection(*key))
        self.assertIsNotNone(self.reopen().load_projection("A", 3, "parser-1"))

    def test_view_state_conflict_returns_current_state_and_task_isolation(self):
        self.assertEqual(self.store.load_view("A"), {"preferenceRevision": 0, "state": None})
        first = self.store.save_view("A", 0, STATE)
        self.assertEqual(first, {"saved": True, "preferenceRevision": 1, "state": STATE})
        other = {**STATE, "follow": "following", "detailTab": "raw"}
        self.assertEqual(self.store.save_view("A", 0, other), {**first, "saved": False})
        self.assertEqual(self.store.save_view("A", 1, other)["preferenceRevision"], 2)
        self.assertEqual(self.store.load_view("B"), {"preferenceRevision": 0, "state": None})
        self.assertEqual(self.reopen().load_view("A")["state"], other)

    def test_view_rejects_body_paths_unknown_fields_and_nonfinite_values(self):
        values = [{**STATE, "body": "synthetic"}, {**STATE, "sourcePath": "/synthetic"},
                  {**STATE, "anchor": {"eventId": "x", "offsetPx": float("nan")}},
                  {**STATE, "filters": {**STATE["filters"], "secret": "synthetic"}},
                  {**STATE, "detailTab": "invalid"}]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(StorageError):
                    self.store.save_view("A", 0, value)
        self.assertEqual(self.store.load_view("A")["preferenceRevision"], 0)

    def test_selected_agent_preferences_survive_restart_including_no_agents(self):
        for revision, group_ids in enumerate((["main", "nested"], [])):
            state = {**STATE, "agentScope": {"kind": "selected", "groupIds": group_ids}}
            self.assertTrue(self.store.save_view("A", revision, state)["saved"])
            self.assertEqual(self.reopen().load_view("A")["state"]["agentScope"], state["agentScope"])

    def test_absolute_turn_selection_and_both_clear_forms_survive_restart(self):
        # 顶层偏好完整替换，旧状态缺失字段和显式 null 都不能复活上次的轮次选区。
        absolute_range = {"startMs": 1790000000000, "endMs": 1790000001000}
        for revision, extra in enumerate(({}, {"turnRange": absolute_range}, {"turnRange": None}, {})):
            state = {**STATE, **extra}
            self.assertTrue(self.store.save_view("A", revision, state)["saved"])
            self.assertEqual(self.reopen().load_view("A")["state"], state)

    def test_timeline_spacing_survives_restart_without_changing_absolute_turn_selection(self):
        absolute_range = {"startMs": 1790000000000, "endMs": 1790000001000}
        for revision, extra in enumerate(({}, {"timelineSpacing": "runtime"}, {"timelineSpacing": "compact"}, {})):
            state = {**STATE, "turnRange": absolute_range, **extra}
            self.assertTrue(self.store.save_view("A", revision, state)["saved"])
            self.assertEqual(self.reopen().load_view("A")["state"], state)

    def test_returned_payloads_do_not_alias_caller_mutations(self):
        state = copy.deepcopy(STATE)
        returned = self.store.save_view("A", 0, state)
        state["filters"]["text"] = "changed"
        returned["state"]["filters"]["text"] = "also changed"
        self.assertEqual(self.store.load_view("A")["state"], STATE)

    def test_durable_receipts_insert_and_update_are_compare_and_swap(self):
        self.assertIsNone(self.store.get_receipt("request"))
        pending = {"status": "pending", "task": "A", "exportId": "synthetic"}
        result = self.store.put_receipt("request", pending)
        self.assertEqual(result, {"saved": True, "revision": 1, "value": pending})
        duplicate = self.store.put_receipt("request", {"status": "different"})
        self.assertEqual(duplicate, {**result, "saved": False})
        self.assertEqual(self.reopen().get_receipt("request"), {"revision": 1, "value": pending})
        verified = {**pending, "status": "verified"}
        self.assertEqual(self.store.put_receipt("request", verified, expected_revision=1)["revision"], 2)
        self.assertEqual(self.store.get_receipt("request")["value"], verified)

    def test_concurrent_receipt_insert_keeps_one_durable_value(self):
        with ThreadPoolExecutor(8) as executor:
            results = list(executor.map(lambda i: self.store.put_receipt("same", {"attempt": i}), range(32)))
        self.assertEqual(sum(result["saved"] for result in results), 1)
        self.assertTrue(all(result["value"] == results[0]["value"] for result in results))

    def test_symlink_directory_or_database_and_hardlinks_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.base / "ancestor-link"
        symlink(self, link, outside, target_is_directory=True)
        with self.assertRaises(StorageError):
            DerivedStore(link / "index")
        self.assertFalse((outside / "index").exists())
        for mode in ("symlink", "hardlink"):
            root = self.base / mode
            root.mkdir()
            target = outside / (mode + ".sqlite3")
            target.write_bytes(b"synthetic")
            if mode == "symlink":
                symlink(self, root / "derived.sqlite3", target)
            else:
                os.link(target, root / "derived.sqlite3")
            with self.assertRaises(StorageError):
                DerivedStore(root)
            self.assertEqual(target.read_bytes(), b"synthetic")

    def test_sidecar_symlink_is_rejected_before_sqlite_can_open_it(self):
        self.store.close()
        target = self.base / "untouched"
        target.write_bytes(b"synthetic")
        symlink(self, self.root / "derived.sqlite3-journal", target)
        with self.assertRaises(StorageError):
            DerivedStore(self.root)
        self.assertEqual(target.read_bytes(), b"synthetic")

    def sidecar_stat_sequence(self, values, *, name="derived.sqlite3-journal"):
        """Inject the OS unlink observation deterministically; SQLite still runs normally."""
        original = storage.os.stat
        observations = iter(values)
        calls = []

        def observe(path, *args, **kwargs):
            if path == name:
                calls.append((path, kwargs))
                value = next(observations)
                if isinstance(value, Exception):
                    raise value
                return value
            return original(path, *args, **kwargs)

        return mock.patch.object(storage.os, "stat", side_effect=observe), calls

    def sidecar_metadata(self, **changes):
        return SimpleNamespace(**({"st_mode": stat.S_IFREG | 0o600,
                                   "st_uid": storage.os.getuid(), "st_nlink": 0} | changes))

    def test_removed_private_sidecar_is_rechecked_and_missing_or_valid_replacement_is_accepted(self):
        for suffix in ("-journal", "-wal", "-shm"):
            for replacement in (FileNotFoundError(), self.sidecar_metadata(st_nlink=1)):
                patcher, calls = self.sidecar_stat_sequence(
                    [self.sidecar_metadata(), replacement], name="derived.sqlite3" + suffix)
                with self.subTest(suffix=suffix, replacement=type(replacement).__name__), patcher:
                    self.assertIsNone(self.store.get_receipt("synthetic-unlink-race"))
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertFalse(calls[0][1]["follow_symlinks"])

    def test_unsafe_sidecar_is_not_retried_even_if_it_would_disappear(self):
        for changes in ({"st_mode": stat.S_IFLNK | 0o600}, {"st_mode": stat.S_IFDIR | 0o700},
                        {"st_uid": storage.os.getuid() + 1}, {"st_mode": stat.S_IFREG | 0o640},
                        {"st_nlink": 2}):
            patcher, calls = self.sidecar_stat_sequence([self.sidecar_metadata(**changes), FileNotFoundError()])
            with self.subTest(changes=changes), patcher, self.assertRaises(StorageError):
                self.store.get_receipt("synthetic-unsafe-sidecar")
            self.assertEqual(len(calls), 1)

    def test_sidecar_recheck_still_rejects_unsafe_or_unlinked_replacement(self):
        for changes in ({}, {"st_nlink": 2}, {"st_mode": stat.S_IFLNK | 0o600, "st_nlink": 1},
                        {"st_uid": storage.os.getuid() + 1, "st_nlink": 1},
                        {"st_mode": stat.S_IFREG | 0o640, "st_nlink": 1}):
            patcher, calls = self.sidecar_stat_sequence([self.sidecar_metadata(), self.sidecar_metadata(**changes)])
            with self.subTest(changes=changes), patcher, self.assertRaises(StorageError):
                self.store.get_receipt("synthetic-replaced-sidecar")
            self.assertEqual(len(calls), 2)

    def test_sidecar_recheck_does_not_ignore_other_io_errors(self):
        patcher, calls = self.sidecar_stat_sequence([self.sidecar_metadata(), PermissionError("synthetic")])
        with patcher, self.assertRaises(PermissionError):
            self.store.get_receipt("synthetic-inaccessible-sidecar")
        self.assertEqual(len(calls), 2)

    def test_main_database_with_zero_links_remains_an_error(self):
        patcher, calls = self.sidecar_stat_sequence([self.sidecar_metadata(), FileNotFoundError()], name="derived.sqlite3")
        with patcher, self.assertRaisesRegex(StorageError, "unsafe-storage-file"):
            self.store.get_receipt("synthetic-unlinked-database")
        self.assertEqual(len(calls), 1)

    def test_directory_replacement_or_database_replacement_stops_writes(self):
        if os.name == "nt":
            # Windows 在整个使用期 pin 住目录；攻击在 rename 处就必须失败。
            with self.assertRaises(PermissionError):
                self.root.rename(self.root.with_name("old"))
            self.assertTrue(self.store.put_receipt("x", {"value": "synthetic"})["saved"])
            return
        self.root.rename(self.root.with_name("old"))
        self.root.mkdir()
        with self.assertRaises(StorageError):
            self.store.put_receipt("x", {"value": "synthetic"})
        self.assertFalse((self.root / "derived.sqlite3").exists())

    def test_unsupported_database_version_does_not_clear_existing_data(self):
        self.store.put_receipt("x", {"synthetic": True})
        path = self.store.path
        self.store.close()
        # Connection 上下文只提交事务，不关闭句柄；Windows 清理目录前必须显式关闭。
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(StorageError, "unsupported-storage-version"):
            DerivedStore(self.root)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1)

    def test_invalid_keys_revisions_and_nonfinite_json_are_rejected(self):
        for revision in (-1, True, 2**53):
            with self.assertRaises(StorageError):
                self.store.put_receipt("x", {}, expected_revision=revision)
        for key in ("", "bad\0key", None):
            with self.assertRaises(StorageError):
                self.store.get_receipt(key)
        with self.assertRaises(StorageError):
            self.store.save_projection("A", 0, "p", {"value": float("inf")})

    def test_close_is_idempotent_and_future_operations_fail(self):
        self.store.close()
        self.store.close()
        with self.assertRaisesRegex(StorageError, "storage-closed"):
            self.store.load_view("A")

    def test_sqlite_error_does_not_expose_message_and_transaction_rolls_back(self):
        self.store._connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'synthetic secret'); END")
        with self.assertRaisesRegex(StorageError, "storage-transaction-failed") as caught:
            self.store.put_receipt("failure", {"synthetic": True})
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(self.store.get_receipt("failure"))
        self.store._connection.execute("DROP TRIGGER synthetic_failure")
        self.assertTrue(self.store.put_receipt("failure", {"synthetic": True})["saved"])

    def test_corrupt_json_cannot_leak_persisted_payload_in_error(self):
        self.store._connection.execute("INSERT INTO receipts VALUES(?,?,?)", ("corrupt", 1, "synthetic secret"))
        with self.assertRaisesRegex(StorageError, "invalid-stored-json") as caught:
            self.store.get_receipt("corrupt")
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
