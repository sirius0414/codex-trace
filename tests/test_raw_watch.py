import os
from pathlib import Path
import select
import tempfile
import threading
import time
import unittest
from unittest import mock

from codex_display.adapters.watch import SourceWatcher
from codex_display.adapters.raw import RawAdapter


class SourceWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'source.jsonl'
        self.path.write_bytes(b'initial\n')
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)
        self.watcher = SourceWatcher()
        self.addCleanup(self.watcher.close)

    def test_watch_owns_only_duplicated_descriptor_and_closes_idempotently(self):
        self.watcher.watch('source', self.fd)
        self.watcher.watch('source', self.fd)
        owned = [value[0] for value in self.watcher._files.values()]
        self.watcher.close()
        self.watcher.close()
        self.assertEqual(os.pread(self.fd, 8, 0), b'initial\n')
        for descriptor in owned:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertFalse(self.watcher.wait_for_changes(0))

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_actual_kqueue_append_and_rename_notifications(self):
        self.assertEqual(self.watcher.mode, 'kqueue')
        self.watcher.watch('source', self.fd)
        self.assertFalse(self.watcher.wait_for_changes(0))
        with self.path.open('ab') as stream:
            stream.write(b'append\n')
        self.assertTrue(self.watcher.wait_for_changes(.1))
        self.assertFalse(self.watcher.wait_for_changes(0))
        self.path.rename(self.root / 'archived.jsonl')
        self.assertTrue(self.watcher.wait_for_changes(.1))

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_replace_updates_watched_inode_without_closing_caller_fd(self):
        self.watcher.watch('source', self.fd)
        old = self.watcher._files['source'][0]
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'new\n')
        replacement.replace(self.path)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.watcher.watch('source', fd)
            self.assertEqual(self.watcher._files['source'][1], (os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            self.assertEqual(os.pread(self.fd, 8, 0), b'initial\n')
            with self.path.open('ab') as stream:
                stream.write(b'next\n')
            self.assertTrue(self.watcher.wait_for_changes(.1))
        finally:
            os.close(fd)

    def test_poll_fallback_timeout_is_bounded_and_can_close(self):
        with mock.patch.object(select, 'kqueue', side_effect=OSError('synthetic unsupported'), create=True):
            watcher = SourceWatcher()
        self.addCleanup(watcher.close)
        self.assertEqual(watcher.mode, 'poll')
        started = time.monotonic()
        self.assertFalse(watcher.wait_for_changes(10))
        self.assertLess(time.monotonic() - started, .6)
        worker = threading.Thread(target=watcher.wait_for_changes)
        worker.start()
        watcher.close()
        worker.join(.4)
        self.assertFalse(worker.is_alive())

    def test_invalid_timeout_and_resource_bounds(self):
        for value in (-1, float('inf'), float('nan'), True, '1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.watcher.wait_for_changes(value)
        with self.assertRaises(ValueError):
            SourceWatcher(max_sources=0)

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_descriptor_limit_does_not_fail_a_valid_source_read(self):
        with mock.patch('codex_display.adapters.watch.os.dup', side_effect=OSError('synthetic fd limit')):
            self.watcher.watch('source', self.fd)
        self.assertEqual(len(self.watcher._files), 0)
        self.assertEqual(os.pread(self.fd, 8, 0), b'initial\n')
        self.assertFalse(self.watcher.wait_for_changes(0))

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_watch_descriptor_count_is_bounded(self):
        watcher = SourceWatcher(max_sources=2)
        self.addCleanup(watcher.close)
        for index in range(5):
            watcher.watch(str(index), self.fd)
        self.assertEqual(len(watcher._files), 2)
        self.assertEqual(list(watcher._files), ['3', '4'])

    @unittest.skipUnless(hasattr(select, 'kqueue'), 'kqueue requires macOS/BSD')
    def test_raw_adapter_registers_validated_sources_and_waits_for_changes(self):
        identifier = '11111111-1111-4111-8111-111111111111'
        self.path.write_text('{"type":"session_meta","payload":{"id":"' + identifier + '"}}\n')
        adapter = RawAdapter([self.root])
        self.addCleanup(adapter.close)
        result = adapter.read(identifier, {'id': identifier, 'path': str(self.path)})
        self.assertEqual(result.issues, [])
        self.assertFalse(adapter.wait_for_changes(0))
        with self.path.open('ab') as stream:
            stream.write(b'{"type":"future"}\n')
        self.assertTrue(adapter.wait_for_changes(.1))
        next_snapshot = adapter.read(identifier, {'id': identifier, 'path': str(self.path)})
        self.assertNotEqual(result.fingerprint, next_snapshot.fingerprint)
        self.assertEqual(len(result.records), len(next_snapshot.records))


if __name__ == '__main__':
    unittest.main()
