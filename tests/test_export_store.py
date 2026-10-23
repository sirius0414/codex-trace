"""Local export boundaries; every save uses a disposable injected directory."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from codex_display import export_store
from codex_display.export_store import ExportStore, ExportUnconfirmedError

FIXED_UUID = UUID("11111111-2222-4333-8444-555555555555")
PAYLOAD = b'{"unknown":true}\r\n\xffbad\n{"partial":'


def _publish_in_process(root, source, candidate, ready, start, results):
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        ready.put(True)
        if not start.wait(5):
            raise RuntimeError("synthetic process gate timeout")
        results.put(ExportStore(Path(root)).publish_file(descriptor, candidate))
    except Exception as error:
        results.put({"error": type(error).__name__, "reason": str(error)})
    finally:
        os.close(descriptor)


class ExportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-display-export-store-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "exports"
        self.store = ExportStore(self.root)

    def source(self, payload=PAYLOAD):
        name = self.base / ("source-" + str(len(list(self.base.iterdir()))) + ".zip")
        name.write_bytes(payload)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, descriptor)
        return name, descriptor

    def candidate(self, payload=PAYLOAD):
        return self.store.reserve(len(payload), hashlib.sha256(payload).hexdigest())

    def test_exact_bytes_hash_permissions_and_no_temporary_files(self):
        result = self.store.save_bytes(PAYLOAD, "codex-display-test")
        saved = Path(result["path"])
        self.assertEqual(saved.parent, self.root)
        self.assertEqual(saved.name, result["filename"])
        self.assertEqual(saved.read_bytes(), PAYLOAD)
        self.assertEqual(result["bytes"], len(PAYLOAD))
        self.assertEqual(result["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertTrue(result["verified"])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.iterdir()), [saved])

    def test_cached_result_is_verified_from_disk_without_writing(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        before = (saved.stat().st_mtime_ns, saved.read_bytes(), sorted(self.root.iterdir()))
        with mock.patch.object(export_store.os, "write", side_effect=AssertionError("verification must not write")):
            verified = self.store.verify_saved(result)
        self.assertEqual(verified, result)
        self.assertIsNot(verified, result)
        self.assertEqual(before, (saved.stat().st_mtime_ns, saved.read_bytes(), sorted(self.root.iterdir())))

    def test_concurrent_publisher_temp_unlink_rehashes_the_same_inode(self):
        payload = b"synthetic published bytes\n" * 10000
        result = self.store.save_bytes(payload)
        saved = Path(result["path"])
        temporary = self.root / ".codex-display-tmp-concurrent-publisher"
        os.link(saved, temporary)
        original = saved.stat()
        real_read = os.read
        totals, current_total = [], 0
        removed = False

        def read_and_cleanup(fd, size):
            nonlocal removed, current_total
            chunk = real_read(fd, size)
            if os.fstat(fd).st_ino == original.st_ino:
                current_total += len(chunk)
                if not removed:
                    temporary.unlink()
                    removed = True
                if not chunk:
                    totals.append(current_total)
                    current_total = 0
            return chunk

        with mock.patch.object(export_store.os, "read", new=read_and_cleanup):
            self.assertEqual(self.store.verify_saved(result), result)
        self.assertEqual(totals, [len(payload), len(payload)])
        self.assertEqual(saved.stat().st_ino, original.st_ino)
        self.assertEqual(saved.stat().st_nlink, 1)
        self.assertEqual(list(self.root.iterdir()), [saved])

    def test_link_cleanup_retry_rejects_changed_bytes_even_with_restored_mtime(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        temporary = self.root / ".codex-display-tmp-concurrent-publisher"
        os.link(saved, temporary)
        original = saved.stat()
        real_read = os.read
        changed = False

        def rewrite_after_first_complete_read(fd, size):
            nonlocal changed
            chunk = real_read(fd, size)
            if not chunk and not changed and os.fstat(fd).st_ino == original.st_ino:
                changed = True
                temporary.unlink()
                saved.write_bytes(b"x" * len(PAYLOAD))
                os.utime(saved, ns=(original.st_atime_ns, original.st_mtime_ns))
            return chunk

        with mock.patch.object(export_store.os, "read", new=rewrite_after_first_complete_read), \
             self.assertRaisesRegex(ValueError, "checksum-mismatch"):
            self.store.verify_saved(result)
        self.assertEqual(saved.read_bytes(), b"x" * len(PAYLOAD))

    def test_ctime_only_rewrite_without_publication_cleanup_is_still_rejected(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        original = saved.stat()
        real_read = os.read
        changed = False

        def rewrite_identical_after_complete_read(fd, size):
            nonlocal changed
            chunk = real_read(fd, size)
            if not chunk and not changed and os.fstat(fd).st_ino == original.st_ino:
                changed = True
                saved.write_bytes(PAYLOAD)
                os.utime(saved, ns=(original.st_atime_ns, original.st_mtime_ns))
            return chunk

        with mock.patch.object(export_store.os, "read", new=rewrite_identical_after_complete_read), \
             self.assertRaisesRegex(ValueError, "changed-during-verification"):
            self.store.verify_saved(result)

    def test_link_cleanup_retry_is_bounded_and_second_mutation_is_rejected(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        temporary = self.root / ".codex-display-tmp-concurrent-publisher"
        os.link(saved, temporary)
        original = saved.stat()
        real_read = os.read
        passes = 0

        def change_after_each_complete_read(fd, size):
            nonlocal passes
            chunk = real_read(fd, size)
            if not chunk and os.fstat(fd).st_ino == original.st_ino:
                passes += 1
                if passes == 1:
                    temporary.unlink()
                else:
                    saved.write_bytes(PAYLOAD)
                    os.utime(saved, ns=(original.st_atime_ns, original.st_mtime_ns))
            return chunk

        with mock.patch.object(export_store.os, "read", new=change_after_each_complete_read), \
             self.assertRaisesRegex(ValueError, "changed-during-verification"):
            self.store.verify_saved(result)
        self.assertEqual(passes, 2)

    def test_missing_or_changed_cached_file_is_not_recreated(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        changed = b"x" * len(PAYLOAD)
        saved.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "checksum-mismatch"):
            self.store.verify_saved(result)
        self.assertEqual(saved.read_bytes(), changed)
        saved.unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.verify_saved(result)
        self.assertEqual(list(self.root.iterdir()), [])
        self.root.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.store.verify_saved(result)
        self.assertFalse(self.root.exists())

    def test_cached_result_cannot_escape_root_or_accept_invalid_envelope(self):
        result = self.store.save_bytes(PAYLOAD)
        for replacement in ({"filename": "../outside.zip"}, {"path": str(self.base / result["filename"])},
                            {"bytes": True}, {"bytes": -1}, {"sha256": "invalid"},
                            {"verified": None}, {"verified": 1}, {"verified": "true"}):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, "invalid-export-result"):
                self.store.verify_saved({**result, **replacement})
        self.assertEqual(Path(result["path"]).read_bytes(), PAYLOAD)

    def test_cached_symlink_and_special_file_are_rejected_without_following(self):
        result = self.store.save_bytes(PAYLOAD)
        saved = Path(result["path"])
        saved.unlink()
        protected = self.base / "protected"
        protected.write_bytes(PAYLOAD)
        saved.symlink_to(protected)
        with self.assertRaises(OSError):
            self.store.verify_saved(result)
        self.assertTrue(saved.is_symlink())
        self.assertEqual(protected.read_bytes(), PAYLOAD)
        saved.unlink()
        os.mkfifo(saved)
        with self.assertRaisesRegex(ValueError, "not-regular-file"):
            self.store.verify_saved(result)
        self.assertTrue(stat.S_ISFIFO(saved.stat().st_mode))

    def test_existing_export_directory_is_restricted_without_changing_parent(self):
        parent_mode = stat.S_IMODE(self.base.stat().st_mode)
        self.root.mkdir(mode=0o777)
        self.root.chmod(0o777)
        self.store.save_bytes(b"")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), parent_mode)

    def test_default_directory_is_fixed_but_construction_does_not_write_it(self):
        with mock.patch.object(Path, "home", return_value=self.base):
            store = ExportStore()
        self.assertEqual(store.root, self.base / "Downloads/Codex Display")
        self.assertFalse((self.base / "Downloads").exists())

    def test_final_collision_never_overwrites_existing_file(self):
        self.root.mkdir()
        occupied = self.root / f"codex-display-{FIXED_UUID.hex}.zip"
        occupied.write_bytes(b"preserve-existing")
        with mock.patch.object(export_store, "uuid4", return_value=FIXED_UUID):
            with self.assertRaises(FileExistsError):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(occupied.read_bytes(), b"preserve-existing")
        self.assertEqual(list(self.root.iterdir()), [occupied])

    def test_existing_temporary_name_is_not_owned_or_deleted(self):
        self.root.mkdir()
        occupied = self.root / f".codex-display-tmp-{FIXED_UUID.hex}"
        occupied.write_bytes(b"another-owner")
        with mock.patch.object(export_store, "uuid4", return_value=FIXED_UUID):
            with self.assertRaises(FileExistsError):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(occupied.read_bytes(), b"another-owner")
        self.assertEqual(list(self.root.iterdir()), [occupied])

    def test_final_symlink_collision_does_not_touch_target(self):
        self.root.mkdir()
        protected = self.base / "protected"
        protected.write_bytes(b"preserve-target")
        link = self.root / f"codex-display-{FIXED_UUID.hex}.zip"
        link.symlink_to(protected)
        with mock.patch.object(export_store, "uuid4", return_value=FIXED_UUID):
            with self.assertRaises(FileExistsError):
                self.store.save_bytes(PAYLOAD)
        self.assertTrue(link.is_symlink())
        self.assertEqual(protected.read_bytes(), b"preserve-target")
        self.assertEqual(list(self.root.iterdir()), [link])

    def test_symlink_directory_and_parent_chain_are_rejected(self):
        actual = self.base / "actual"
        actual.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.save_bytes(PAYLOAD)
        self.assertEqual(list(actual.iterdir()), [])
        with self.assertRaises(OSError):
            ExportStore(self.root / "nested").save_bytes(PAYLOAD)
        self.assertFalse((actual / "nested").exists())

    def test_bad_prefixes_and_payloads_have_no_filesystem_side_effects(self):
        for prefix in ("", ".", "..", "../outside", "/absolute", "a/b", "a\\b", "hello world", "x\x00y", "x" * 81, None):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.store.save_bytes(PAYLOAD, prefix)
        for payload in (None, "text", bytearray(b"bytes"), []):
            with self.subTest(payload=payload), self.assertRaises(TypeError):
                self.store.save_bytes(payload)
        self.assertFalse(self.root.exists())
        for root in (Path("relative"), self.base / ".." / "outside", Path("/")):
            with self.subTest(root=root), self.assertRaises(ValueError):
                ExportStore(root)

    def test_disk_write_failure_removes_only_owned_temporary(self):
        with mock.patch.object(export_store.os, "write", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_writes_are_completed_before_publication(self):
        real_write = os.write
        with mock.patch.object(export_store.os, "write", side_effect=lambda fd, data: real_write(fd, data[:3])):
            result = self.store.save_bytes(PAYLOAD)
        self.assertEqual(Path(result["path"]).read_bytes(), PAYLOAD)

    def test_fsync_failure_cleans_temp_without_publishing(self):
        with mock.patch.object(export_store.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaises(OSError):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_directory_fsync_failure_recovers_by_verifying_same_published_file(self):
        real_fsync = os.fsync

        def fail_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("private synthetic directory fsync failure")
            return real_fsync(fd)

        with mock.patch.object(export_store.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(ExportUnconfirmedError) as raised:
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(str(raised.exception), "export-unconfirmed")
        candidate = raised.exception.candidate_result
        self.assertIs(candidate["verified"], False)
        saved = Path(candidate["path"])
        identity = saved.stat().st_ino
        with mock.patch.object(export_store.os, "write", side_effect=AssertionError("retry must not write")), \
             mock.patch.object(export_store.os, "link", side_effect=AssertionError("retry must not publish")):
            result = self.store.verify_saved(candidate)
        self.assertEqual(result, {**candidate, "verified": True})
        self.assertIs(candidate["verified"], False)
        self.assertEqual(saved.stat().st_ino, identity)
        self.assertEqual(list(self.root.iterdir()), [saved])

    def test_unconfirmed_candidate_must_pass_readback_before_becoming_verified(self):
        candidate = {**self.store.save_bytes(PAYLOAD), "verified": False}
        saved = Path(candidate["path"])
        saved.write_bytes(b"x" * len(PAYLOAD))
        with self.assertRaisesRegex(ValueError, "checksum-mismatch"):
            self.store.verify_saved(candidate)
        self.assertIs(candidate["verified"], False)
        self.assertEqual(list(self.root.iterdir()), [saved])
        saved.unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.verify_saved(candidate)
        self.assertIs(candidate["verified"], False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_checksum_failure_never_publishes(self):
        with mock.patch.object(export_store.os, "read", return_value=b""):
            with self.assertRaisesRegex(ValueError, "checksum-mismatch"):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_publish_failure_cleans_temp_and_preserves_other_files(self):
        self.root.mkdir()
        other = self.root / "other.zip"
        other.write_bytes(b"unrelated")
        with mock.patch.object(export_store.os, "link", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(OSError):
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(other.read_bytes(), b"unrelated")
        self.assertEqual(list(self.root.iterdir()), [other])

    def test_replaced_temporary_is_not_removed_on_failure(self):
        replacement = None

        def replace_then_fail(source, destination, **kwargs):
            nonlocal replacement
            replacement = self.root / source
            replacement.unlink()
            replacement.write_bytes(b"replacement-not-owned")
            raise OSError("synthetic publication failure")

        with mock.patch.object(export_store.os, "link", side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                self.store.save_bytes(PAYLOAD)
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.read_bytes(), b"replacement-not-owned")
        self.assertEqual(list(self.root.iterdir()), [replacement])

    def test_final_readback_failure_does_not_delete_published_or_foreign_file(self):
        real_verify = export_store._verify_bytes
        calls = 0

        def fail_after_publication(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic readback failure")
            return real_verify(*args)

        with mock.patch.object(export_store, "_verify_bytes", side_effect=fail_after_publication):
            with self.assertRaises(ExportUnconfirmedError) as raised:
                self.store.save_bytes(PAYLOAD)
        self.assertEqual(str(raised.exception), "export-unconfirmed")
        candidate = raised.exception.candidate_result
        self.assertIs(candidate["verified"], False)
        entries = list(self.root.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].suffix, ".zip")
        self.assertEqual(entries[0].read_bytes(), PAYLOAD)
        identity = entries[0].stat().st_ino
        recovered = self.store.verify_saved(candidate)
        self.assertEqual(recovered, {**candidate, "verified": True})
        self.assertIs(candidate["verified"], False)
        self.assertEqual(entries[0].stat().st_ino, identity)
        self.assertEqual(list(self.root.iterdir()), entries)

    def test_reservation_is_serializable_fixed_and_has_no_filesystem_side_effects(self):
        candidate = self.candidate()
        self.assertEqual(set(candidate), {"filename", "path", "bytes", "sha256", "verified"})
        self.assertFalse(candidate["verified"])
        self.assertEqual(candidate, json.loads(json.dumps(candidate)))
        self.assertFalse(self.root.exists())
        _, source = self.source()
        result = ExportStore(self.root).publish_file(source, json.loads(json.dumps(candidate)))
        self.assertEqual(result, {**candidate, "verified": True})
        self.assertFalse(candidate["verified"])
        self.assertEqual(Path(result["path"]).read_bytes(), PAYLOAD)

    def test_invalid_reservations_and_candidates_never_create_directory(self):
        for size in (-1, True, 1.5, 2**53, None):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.store.reserve(size, hashlib.sha256(PAYLOAD).hexdigest())
        for digest in (None, "", "x" * 64, "A" * 64, "0" * 65):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.store.reserve(1, digest)
        for prefix in ("../x", "", "x/y", None):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.store.reserve(1, "0" * 64, prefix)
        candidate = self.candidate()
        for fields in ({"path": str(self.base / candidate["filename"])}, {"filename": "../outside.zip"},
                       {"bytes": True}, {"verified": 1}, {"sha256": "invalid"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.publish_file(-1, {**candidate, **fields})
        self.assertFalse(self.root.exists())

    def test_file_publication_is_bounded_streaming_preserves_source_offset_and_permissions(self):
        payload = (b"synthetic-zip-content\x00\xff\r\n" * 900000)[:20 * 1024 * 1024]
        path, source = self.source(payload)
        candidate = self.candidate(payload)
        os.lseek(source, 11, os.SEEK_SET)
        before = path.stat()
        sizes = []
        real_pread, real_write = os.pread, os.write
        def read(fd, size, offset):
            sizes.append(size)
            return real_pread(fd, size, offset)
        def write(fd, data):
            self.assertLessEqual(len(data), 65536)
            return real_write(fd, data)
        tracemalloc.start()
        try:
            # Plain replacements do not retain every write buffer as Mock's
            # call history would, which would distort this memory assertion.
            with mock.patch.object(export_store.os, "pread", new=read), \
                 mock.patch.object(export_store.os, "write", new=write):
                result = self.store.publish_file(source, candidate)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 65536)
        self.assertEqual(os.lseek(source, 0, os.SEEK_CUR), 11)
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertEqual(Path(result["path"]).read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(Path(result["path"]).stat().st_mode), 0o600)

    def test_existing_same_candidate_is_only_verified_even_when_original_source_is_gone(self):
        _, source = self.source()
        candidate = self.candidate()
        result = self.store.publish_file(source, candidate)
        saved = Path(result["path"])
        identity = saved.stat().st_ino
        with mock.patch.object(export_store.os, "pread", side_effect=AssertionError("must not read original source")), \
             mock.patch.object(export_store.os, "write", side_effect=AssertionError("must not write")), \
             mock.patch.object(export_store.os, "link", side_effect=AssertionError("must not publish")), \
             mock.patch.object(export_store, "uuid4", side_effect=AssertionError("must not choose another name")):
            recovered = ExportStore(self.root).publish_file(-1, json.loads(json.dumps(candidate)))
        self.assertEqual(recovered, result)
        self.assertEqual(saved.stat().st_ino, identity)
        self.assertEqual(list(self.root.iterdir()), [saved])

    def test_streaming_collision_preserves_file_and_never_chooses_another_name(self):
        _, source = self.source()
        candidate = self.candidate()
        self.root.mkdir()
        occupied = Path(candidate["path"])
        occupied.write_bytes(b"X" * len(PAYLOAD))
        with mock.patch.object(export_store, "uuid4", side_effect=AssertionError("must not re-reserve")), \
             self.assertRaisesRegex(ValueError, "checksum-mismatch"):
            self.store.publish_file(source, candidate)
        self.assertEqual(occupied.read_bytes(), b"X" * len(PAYLOAD))
        self.assertEqual(list(self.root.iterdir()), [occupied])

    def test_verified_candidate_never_recreates_a_user_deleted_file(self):
        _, source = self.source()
        result = self.store.publish_file(source, self.candidate())
        Path(result["path"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.publish_file(source, result)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_streaming_rejects_size_hash_and_special_source_without_publication(self):
        _, source = self.source()
        for candidate in ({**self.candidate(), "bytes": len(PAYLOAD) + 1},
                          {**self.candidate(), "sha256": "0" * 64}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.store.publish_file(source, candidate)
        reader, writer = os.pipe()
        try:
            with self.assertRaisesRegex(ValueError, "not-regular-file"):
                self.store.publish_file(reader, self.candidate(b""))
        finally:
            os.close(reader)
            os.close(writer)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_streaming_final_symlink_and_ancestor_symlink_are_never_followed(self):
        protected, source = self.source()
        candidate = self.candidate()
        self.root.mkdir()
        Path(candidate["path"]).symlink_to(protected)
        with self.assertRaises(OSError):
            self.store.publish_file(source, candidate)
        self.assertEqual(protected.read_bytes(), PAYLOAD)
        Path(candidate["path"]).unlink()
        self.root.rmdir()
        actual = self.base / "actual"
        actual.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.publish_file(source, candidate)
        self.assertEqual(list(actual.iterdir()), [])

    def test_source_append_truncate_or_same_size_rewrite_during_copy_prevents_publication(self):
        payload = b"a" * 150000
        for mutation in ("append", "truncate", "rewrite"):
            with self.subTest(mutation=mutation):
                path, source = self.source(payload)
                candidate = self.candidate(payload)
                original = path.stat()
                real_read = os.pread
                changed = False
                def mutate(fd, size, offset):
                    nonlocal changed
                    data = real_read(fd, size, offset)
                    if fd == source and not changed:
                        changed = True
                        if mutation == "append":
                            with path.open("ab") as stream:
                                stream.write(b"more")
                        elif mutation == "truncate":
                            path.write_bytes(b"a")
                        else:
                            path.write_bytes(b"z" * len(payload))
                            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                    return data
                with mock.patch.object(export_store.os, "pread", side_effect=mutate), self.assertRaises(ValueError):
                    self.store.publish_file(source, candidate)
                self.assertFalse(Path(candidate["path"]).exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_source_descriptor_replacement_during_copy_prevents_publication(self):
        payload = b"a" * 150000
        _, source = self.source(payload)
        _, replacement = self.source(payload)
        candidate = self.candidate(payload)
        real_read = os.pread
        replaced = False
        def replace(fd, size, offset):
            nonlocal replaced
            data = real_read(fd, size, offset)
            if fd == source and not replaced:
                replaced = True
                os.dup2(replacement, source)
            return data
        with mock.patch.object(export_store.os, "pread", side_effect=replace), \
             self.assertRaisesRegex(ValueError, "source-changed-during-copy"):
            self.store.publish_file(source, candidate)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_prepublication_failure_retries_same_persisted_candidate_after_restart(self):
        _, source = self.source()
        for operation in ("write", "link"):
            candidate = self.candidate()
            persisted = json.dumps(candidate)
            with self.subTest(operation=operation), \
                 mock.patch.object(export_store.os, operation, side_effect=OSError("synthetic failure")), \
                 self.assertRaises(OSError):
                self.store.publish_file(source, candidate)
            self.assertFalse(Path(candidate["path"]).exists())
            with mock.patch.object(export_store, "uuid4", return_value=FIXED_UUID):
                saved = ExportStore(self.root).publish_file(source, json.loads(persisted))
            self.assertEqual(saved, {**candidate, "verified": True})
            self.assertEqual(Path(saved["path"]).read_bytes(), PAYLOAD)

    def test_postpublication_fsync_failure_retries_fixed_candidate_without_source_copy(self):
        _, source = self.source()
        candidate = self.candidate()
        persisted = json.dumps(candidate)
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic directory fsync failure")
            return real_fsync(fd)
        with mock.patch.object(export_store.os, "fsync", side_effect=fail_directory), \
             self.assertRaises(ExportUnconfirmedError) as error:
            self.store.publish_file(source, candidate)
        self.assertEqual(error.exception.candidate_result, candidate)
        with mock.patch.object(export_store.os, "write", side_effect=AssertionError("must not duplicate")):
            recovered = ExportStore(self.root).publish_file(-1, json.loads(persisted))
        self.assertEqual(recovered, {**candidate, "verified": True})
        self.assertEqual(list(self.root.iterdir()), [Path(candidate["path"])])

    def test_crash_immediately_after_link_recovers_using_candidate_recorded_before_link(self):
        _, source = self.source()
        candidate = self.candidate()
        persisted = json.dumps(candidate)
        real_link = os.link
        def crash_after_link(*args, **kwargs):
            real_link(*args, **kwargs)
            raise SystemExit("synthetic crash")
        with mock.patch.object(export_store.os, "link", side_effect=crash_after_link), self.assertRaises(SystemExit):
            self.store.publish_file(source, candidate)
        recovered = ExportStore(self.root).publish_file(-1, json.loads(persisted))
        self.assertEqual(recovered, {**candidate, "verified": True})
        self.assertEqual(list(self.root.iterdir()), [Path(candidate["path"])])

    def test_link_success_then_directory_move_and_exception_is_unconfirmed_not_prepublication_failure(self):
        _, source = self.source()
        candidate = self.candidate()
        moved = self.base / "moved-exports"
        real_link = os.link
        def moved_after_link(*args, **kwargs):
            real_link(*args, **kwargs)
            self.root.rename(moved)
            raise OSError("synthetic post-link exception")
        with mock.patch.object(export_store.os, "link", side_effect=moved_after_link), \
             self.assertRaises(ExportUnconfirmedError) as caught:
            self.store.publish_file(source, candidate)
        self.assertEqual(caught.exception.candidate_result, candidate)
        self.assertFalse(self.root.exists())
        self.assertEqual((moved / candidate["filename"]).read_bytes(), PAYLOAD)
        self.assertEqual(list(moved.iterdir()), [moved / candidate["filename"]])
        with self.assertRaises(FileNotFoundError):
            self.store.verify_saved(candidate)

    def test_two_processes_publish_the_same_candidate_only_once(self):
        self.maxDiff = None
        payload = b"synthetic concurrent publication\n" * 200000
        source, _ = self.source(payload)
        candidate = self.candidate(payload)
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        workers = [context.Process(target=_publish_in_process,
                                   args=(str(self.root), str(source), candidate, ready, start, results)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                self.assertTrue(ready.get(timeout=5))
            start.set()
            outcomes = [results.get(timeout=5) for _ in workers]
            for worker in workers:
                worker.join(5)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(outcomes, [{**candidate, "verified": True}] * 2)
            self.assertEqual(Path(candidate["path"]).read_bytes(), payload)
            self.assertEqual(list(self.root.iterdir()), [Path(candidate["path"])])
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(2)
            ready.close()
            results.close()


if __name__ == "__main__":
    unittest.main()
