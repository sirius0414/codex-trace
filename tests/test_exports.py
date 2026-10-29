"""Durable export lifecycle; all bytes and destinations are synthetic/temp-only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from codex_display.platform import filesystem as os
from codex_display.exports import ExportManager, ExportError, _operation_key, _state_key
from codex_display.export_store import ExportStore, ExportUnconfirmedError
from codex_display.storage import DerivedStore
from contracts.test_contracts import validate

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'


class Collector:
    def __init__(self, payload=b'synthetic ZIP bytes only', started=None, proceed=None):
        self.payload, self.started, self.proceed, self.calls = payload, started, proceed, 0
    def collect(self, identifier, destination, *, destination_fd):
        self.calls += 1
        if self.started:
            self.started.set()
            if not self.proceed.wait(3): raise RuntimeError('test timeout')
        remaining = memoryview(self.payload)
        while remaining:
            count = os.write(destination_fd, remaining)
            remaining = remaining[count:]
        os.fsync(destination_fd)
        return {'bytes': len(self.payload), 'sha256': hashlib.sha256(self.payload).hexdigest(),
                'manifest': {'schemaVersion': '1.0.0', 'rootThreadId': identifier, 'enumerationCutoff': 1,
                             'collectionStartedAt': 1, 'collectionEndedAt': 2, 'atomicSnapshot': False,
                             'relationships': [], 'sources': [], 'enumerationComplete': True, 'unresolvedRanges': []}}


def commit_process(root, destination, binding, export_id, request_id, queue):
    store = DerivedStore(Path(root))
    manager = ExportManager(store, Collector(), ExportStore(Path(destination)))
    try:
        result = manager.commit(binding, export_id, request_id)
        queue.put(result['save']['status'])
    finally:
        manager.close(); store.close()


class ExportManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'state'
        self.store = DerivedStore(self.root)
        (self.base / 'Downloads').mkdir()
        self.destination = ExportStore(self.base / 'Downloads/Codex Display')
        self.collector = Collector()
        self.manager = ExportManager(self.store, self.collector, self.destination)
        self.addCleanup(lambda: self.store.close())
        self.addCleanup(lambda: self.manager.close())
        self.binding = {'schemaVersion': '1.0.0', 'sourceInstanceId': self.store.source_instance_id,
                        'viewSessionId': 'synthetic-view', 'selectionEpoch': 1, 'threadId': A}
        self.create_key, self.commit_key = str(uuid4()), str(uuid4())
    def create(self):
        result = self.manager.create(self.binding, self.create_key)
        validate('DownloadResult', result)
        return result
    def commit(self, created):
        result = self.manager.commit(self.binding, created['exportId'], self.commit_key)
        validate('DownloadResult', result)
        return result
    def test_create_commit_and_result_are_stable_and_exact(self):
        created = self.create()
        self.assertEqual(created['status'], 'ready')
        self.assertEqual(created, self.create())
        self.assertEqual(self.collector.calls, 1)
        committed = self.commit(created)
        self.assertEqual(committed['save']['status'], 'verified')
        self.assertEqual(committed['filename'], committed['save']['filename'])
        path = Path(committed['save']['path'])
        self.assertEqual(path.read_bytes(), self.collector.payload)
        self.commit(created)
        result = self.manager.result(self.binding, self.commit_key)
        self.assertEqual(result['save']['sha256'], hashlib.sha256(self.collector.payload).hexdigest())
        self.assertEqual(list(self.destination.root.iterdir()), [path])
        self.assertEqual(self.collector.calls, 1)
    def test_conflicts_span_operation_thread_and_export(self):
        created = self.create()
        for binding in ({**self.binding, 'threadId': B}, {**self.binding, 'sourceInstanceId': 'foreign'}):
            with self.assertRaises(ExportError) as caught:
                self.manager.create(binding, self.create_key)
            self.assertEqual(caught.exception.code, 'idempotencyConflict')
        with self.assertRaises(ExportError): self.manager.commit(self.binding, created['exportId'], self.create_key)
        with self.assertRaises(ExportError): self.manager.result(self.binding, self.create_key)
        self.commit(created)
        with self.assertRaises(ExportError): self.manager.commit(self.binding, created['exportId'], str(uuid4()))
        other = self.manager.create(self.binding, str(uuid4()))
        with self.assertRaises(ExportError): self.manager.commit(self.binding, other['exportId'], self.commit_key)
    def test_chunk_retries_are_exact_and_cursor_bound_to_view(self):
        self.collector.payload = b'\xffpartial\n' * 150_000
        created = self.create()
        first = self.manager.read(self.binding, created['exportId'], None)
        validate('DownloadResult', first)
        self.assertEqual(first, self.manager.read(self.binding, created['exportId'], None))
        second = self.manager.read(self.binding, created['exportId'], first['chunk']['nextCursor'])
        self.assertIsNone(second['chunk']['nextCursor'])
        data = base64.b64decode(first['chunk']['base64']) + base64.b64decode(second['chunk']['base64'])
        self.assertEqual(data, self.collector.payload)
        with self.assertRaises(ExportError):
            self.manager.read({**self.binding, 'selectionEpoch': 2}, created['exportId'], first['chunk']['nextCursor'])
        with self.assertRaises(ExportError): self.manager.read(self.binding, created['exportId'], '1:forged')
    def test_result_missing_record_is_unknown_and_does_not_save(self):
        result = self.manager.result(self.binding, self.commit_key)
        validate('DownloadResult', result)
        self.assertEqual(result['save']['reason'], 'requestNotRecorded')
        self.assertFalse(self.destination.root.exists())
    def test_restart_reuses_receipts_and_deleted_file_is_not_recreated(self):
        created = self.create(); saved = self.commit(created)
        path = Path(saved['save']['path'])
        self.manager.close(); self.store.close()
        self.store = DerivedStore(self.root)
        self.manager = ExportManager(self.store, Collector(), self.destination)
        self.assertEqual(self.create()['exportId'], created['exportId'])
        recovered = self.manager.result({**self.binding, 'viewSessionId': 'new-view'}, self.commit_key)
        self.assertEqual(recovered['save']['status'], 'verified')
        path.unlink()
        unknown = self.manager.result(self.binding, self.commit_key)
        validate('DownloadResult', unknown)
        self.assertEqual(unknown['save']['status'], 'unknown')
        self.assertEqual(self.commit(created)['save']['status'], 'unknown')
        self.assertEqual(list(self.destination.root.iterdir()), [])
    def test_publication_then_receipt_loss_recovers_same_candidate(self):
        created = self.create()
        real = self.destination.publish_file
        def uncertain(fd, candidate):
            real(fd, candidate)
            raise ExportUnconfirmedError(candidate)
        with mock.patch.object(self.destination, 'publish_file', side_effect=uncertain):
            result = self.commit(created)
        self.assertEqual(result['save']['status'], 'unknown')
        self.assertEqual(self.manager.result(self.binding, self.commit_key)['save']['status'], 'verified')
        self.assertEqual(len(list(self.destination.root.glob('*.zip'))), 1)
    def test_restart_with_accepted_intent_never_publishes_from_result(self):
        created = self.create()
        state = self.manager._state(created['exportId'], self.binding)
        state['commitRequestId'] = self.commit_key
        self.manager._write(_state_key(created['exportId']), state)
        self.manager._write(_operation_key(self.commit_key), {
            **{k: state[k] for k in ('sourceInstanceId','rootThreadId','exportId')},
            'operationType':'commit','requestId':self.commit_key,'candidate':state['candidate'],
            'phase':'accepted','save':{'status':'pending','requestId':self.commit_key}})
        with mock.patch.object(self.destination, 'publish_file', side_effect=AssertionError('result must not write')):
            result = self.manager.result(self.binding, self.commit_key)
        self.assertEqual(result['save']['status'], 'unknown')
        self.assertFalse(self.destination.root.exists())
        self.manager.release(self.binding, created['exportId'])
        self.assertTrue((self.manager.root / (created['exportId'] + '.zip')).exists())
        resumed = self.commit(created)
        self.assertEqual(resumed['save']['status'], 'verified')
        self.assertEqual(resumed['save']['filename'], state['candidate']['filename'])
    def test_concurrent_create_status_and_release_during_collection(self):
        started, proceed = threading.Event(), threading.Event()
        self.manager.collector = Collector(started=started, proceed=proceed)
        with ThreadPoolExecutor() as executor:
            future = executor.submit(self.create)
            self.assertTrue(started.wait(2))
            result = self.create()
            self.assertEqual(result['status'], 'collecting')
            self.assertEqual(self.manager.release(self.binding, result['exportId'])['status'], 'collecting')
            proceed.set()
            self.assertEqual(future.result(3)['status'], 'released')
        self.assertEqual(self.manager.collector.calls, 1)
        self.assertEqual(list(self.manager.root.glob('*.zip')), [])
    def test_pending_save_release_and_poll_do_not_cancel_publication(self):
        created = self.create()
        started, proceed = threading.Event(), threading.Event()
        real = self.destination.publish_file
        def slow(fd, candidate):
            started.set()
            if not proceed.wait(3): raise RuntimeError('test timeout')
            return real(fd, candidate)
        with mock.patch.object(self.destination, 'publish_file', side_effect=slow), ThreadPoolExecutor() as executor:
            future = executor.submit(self.commit, created)
            self.assertTrue(started.wait(2))
            self.assertEqual(self.manager.result(self.binding, self.commit_key)['save']['status'], 'pending')
            self.assertEqual(self.manager.release(self.binding, created['exportId'])['save']['status'], 'pending')
            proceed.set()
            self.assertEqual(future.result(3)['save']['status'], 'verified')
        self.manager.release(self.binding, created['exportId'])
        self.assertEqual(len(list(self.destination.root.glob('*.zip'))), 1)
        self.assertEqual(self.manager.result(self.binding, self.commit_key)['save']['status'], 'verified')
    def test_expiry_cleans_only_private_snapshot_preserves_receipt_and_saved_file(self):
        created = self.create(); saved = self.commit(created)
        with mock.patch('codex_display.exports.now_ms', return_value=created['expiresAt'] + 1):
            self.manager.cleanup()
            result = self.manager.status(self.binding, created['exportId'])
        validate('DownloadResult', result)
        self.assertEqual(result['status'], 'released')
        self.assertFalse((self.manager.root / (created['exportId'] + '.zip')).exists())
        self.assertTrue(Path(saved['save']['path']).exists())
        self.assertEqual(self.manager.result(self.binding, self.commit_key)['save']['status'], 'verified')
    def test_collection_failure_does_not_reenumerate_on_retry(self):
        with mock.patch.object(self.collector, 'collect', side_effect=OSError('synthetic failure')):
            failed = self.create()
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.create()['status'], 'failed')
        self.assertEqual(self.collector.calls, 0)
    def test_cross_process_same_commit_publishes_one_file(self):
        created = self.create()
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        args = (str(self.root), str(self.destination.root), self.binding, created['exportId'], self.commit_key, queue)
        processes = [context.Process(target=commit_process, args=args) for _ in range(2)]
        for process in processes: process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        states = [queue.get(timeout=1) for _ in processes]
        self.assertTrue(all(s in ('pending', 'verified') for s in states))
        self.assertEqual(self.manager.result(self.binding, self.commit_key)['save']['status'], 'verified')
        self.assertEqual(len(list(self.destination.root.glob('*.zip'))), 1)
    def test_private_snapshot_mutation_fails_and_never_writes_source_back(self):
        created = self.create()
        path = self.manager.root / (created['exportId'] + '.zip')
        path.write_bytes(b'x' * created['totalBytes'])
        with self.assertRaises(ExportError): self.manager.read(self.binding, created['exportId'], None)
        result = self.commit(created)
        self.assertEqual(result['save']['status'], 'failed')
        self.assertEqual(path.read_bytes(), b'x' * created['totalBytes'])

    def test_chunk_rejects_same_size_rewrite_with_restored_mtime(self):
        created = self.create()
        path = self.manager.root / (created['exportId'] + '.zip')
        before = path.stat()
        path.write_bytes(b'x' * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ExportError):
            self.manager.read(self.binding, created['exportId'], None)
    def test_snapshot_quota_is_explicit(self):
        self.manager.max_snapshots = 1
        created = self.create()
        with self.assertRaises(ExportError) as caught: self.manager.create(self.binding, str(uuid4()))
        self.assertEqual(caught.exception.code, 'resourceLimit')
        self.manager.release(self.binding, created['exportId'])
        self.assertEqual(self.manager.create(self.binding, str(uuid4()))['status'], 'ready')

    def test_reservation_receipt_failure_cleans_only_owned_empty_file(self):
        with mock.patch.object(self.manager, '_write', side_effect=OSError('synthetic persistence failure')):
            with self.assertRaises(OSError): self.create()
        self.assertEqual(list(self.manager.root.glob('*.zip')), [])
        self.assertEqual(self.collector.calls, 0)

    def test_expired_empty_orphan_is_reclaimed_but_nonempty_unknown_is_retained(self):
        orphan = self.manager.root / (str(uuid4()) + '.zip')
        os.close(os.open(orphan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        unknown = self.manager.root / (str(uuid4()) + '.zip')
        descriptor = os.open(unknown, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b'unknown owner')
        finally:
            os.close(descriptor)
        for path in (orphan, unknown): os.utime(path, ns=(1, 1))
        self.manager.cleanup()
        self.assertFalse(orphan.exists())
        self.assertEqual(unknown.read_bytes(), b'unknown owner')

    def test_retry_rechecks_phase_after_taking_export_lock(self):
        created = self.create()
        state = self.manager._state(created['exportId'], self.binding)
        state['commitRequestId'] = self.commit_key
        self.manager._write(_state_key(created['exportId']), state)
        op = {**{k: state[k] for k in ('sourceInstanceId','rootThreadId','exportId')},
              'operationType':'commit','requestId':self.commit_key,'candidate':state['candidate'],
              'phase':'accepted','save':{'status':'pending','requestId':self.commit_key}}
        self.manager._write(_operation_key(self.commit_key), op)
        original_lock = self.manager._lock
        advanced = False
        @contextmanager
        def raced(name, **kwargs):
            nonlocal advanced
            if name == created['exportId'] and not advanced:
                advanced = True
                fd = self.manager._open_snapshot(state)
                try: saved = self.destination.publish_file(fd, state['candidate'])
                finally: os.close(fd)
                op.update(phase='verified', save=self.manager._verified(saved, self.commit_key))
                self.manager._write(_operation_key(self.commit_key), op)
                Path(saved['path']).unlink()  # User moved the already saved file.
            with original_lock(name, **kwargs) as acquired:
                yield acquired
        with mock.patch.object(self.manager, '_lock', side_effect=raced):
            result = self.commit(created)
        self.assertEqual(result['save']['status'], 'unknown')
        self.assertEqual(list(self.destination.root.iterdir()), [])

    def test_publishing_crash_without_file_is_unknown_and_never_republished(self):
        created = self.create()
        state = self.manager._state(created['exportId'], self.binding)
        state['commitRequestId'] = self.commit_key
        self.manager._write(_state_key(created['exportId']), state)
        self.manager._write(_operation_key(self.commit_key), {
            **{k: state[k] for k in ('sourceInstanceId','rootThreadId','exportId')},
            'operationType':'commit','requestId':self.commit_key,'candidate':state['candidate'],
            'phase':'publishing','save':{'status':'pending','requestId':self.commit_key}})
        with mock.patch.object(self.destination, 'publish_file', side_effect=AssertionError('must not republish')):
            self.assertEqual(self.commit(created)['save']['status'], 'unknown')
        self.assertFalse(self.destination.root.exists())


if __name__ == '__main__': unittest.main()
