"""Durable export intents and local receipts, separate from observation state.

Locks cover only this plugin's private export files. No lock or write touches a
Codex source. Long collection/publication work never holds a SQLite transaction.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import threading
import time
from uuid import UUID, uuid4

from .export_store import ExportStore, ExportUnconfirmedError, _open_chain, _verify_bytes
from .storage import _open_directory

CHUNK_BYTES = 1024 * 1024
TTL_MS = 30 * 60 * 1000


def now_ms():
    return int(time.time() * 1000)


class ExportError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(message)


def _operation_key(request_id):
    return 'export-operation:' + hashlib.sha256(request_id.encode()).hexdigest()


def _state_key(export_id):
    return 'export-snapshot:' + export_id


def _identity(info):
    return [info.st_dev, info.st_ino]


class ExportManager:
    def __init__(self, store, collector, destination=None, *, ttl_ms=TTL_MS, max_snapshots=8):
        self.store, self.collector = store, collector
        self.destination = destination or ExportStore()
        self.root = store.root / 'exports'
        self.directory = _open_directory(self.root)
        self.directory_identity = _identity(os.fstat(self.directory))
        self.ttl_ms, self.max_snapshots = ttl_ms, max_snapshots
        self.stop = threading.Event()
        self.janitor = threading.Thread(target=self._janitor, name='codex-display-export-cleanup', daemon=True)
        self.janitor.start()

    def _janitor(self):
        while not self.stop.wait(60):
            try:
                self.cleanup()
            except Exception:
                # Unavailable private storage does not authorize broad cleanup.
                pass

    def close(self):
        self.stop.set()
        self.janitor.join(timeout=2)
        if self.directory >= 0:
            os.close(self.directory)
            self.directory = -1

    def _check_directory(self):
        fd = _open_chain(self.root)
        try:
            if _identity(os.fstat(fd)) != self.directory_identity:
                raise ExportError('unreadable', '临时导出目录已变化。')
        finally:
            os.close(fd)

    @contextmanager
    def _lock(self, name, *, blocking=True):
        self._check_directory()
        fd = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=self.directory)
        acquired = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ExportError('unreadable', '临时导出锁不可用。')
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, key):
        result = self.store.get_receipt(key)
        return result['value'] if result else None

    def _write(self, key, value):
        previous = self.store.get_receipt(key)
        result = self.store.put_receipt(key, value, previous['revision'] if previous else 0)
        if not result['saved']:
            raise ExportError('revisionConflict', '导出记录正在更新，请用同一请求重试。', True)

    @staticmethod
    def _check_owner(value, binding, operation=None, export_id=None):
        if (value['sourceInstanceId'] != binding['sourceInstanceId']
                or value['rootThreadId'] != binding['threadId']
                or (operation and value.get('operationType') != operation)
                or (export_id and value.get('exportId') != export_id)):
            raise ExportError('idempotencyConflict', '此请求标识已用于其他导出操作。')

    def _state(self, export_id, binding):
        try:
            if str(UUID(export_id)) != export_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ExportError('notFound', '导出记录不存在。') from None
        state = self._read(_state_key(export_id))
        if not state:
            raise ExportError('notFound', '导出记录不存在。')
        self._check_owner(state, binding)
        return state

    @staticmethod
    def _unknown(binding, request_id, reason='requestNotRecorded'):
        return {'binding': dict(binding), 'exportId': None, 'status': 'unknown', 'filename': None,
                'totalBytes': None, 'expiresAt': None, 'manifest': None, 'chunk': None,
                'explanation': '保存结果尚未确认；请核对同一次请求，不要据此判断文件未保存。',
                'createRequestId': None,
                'save': {'status': 'unknown', 'requestId': request_id, 'reason': reason}}

    def _response(self, state, binding, *, operation=None, chunk=None):
        if operation is None and state.get('commitRequestId'):
            operation = self._read(_operation_key(state['commitRequestId']))
        save = deepcopy(operation['save']) if operation else {'status': 'notRequested', 'requestId': None}
        return {'binding': dict(binding), 'exportId': state['exportId'], 'status': state['status'],
                'filename': state.get('candidate', {}).get('filename'), 'totalBytes': state.get('bytes'),
                'expiresAt': state['expiresAt'], 'manifest': deepcopy(state.get('manifest')), 'chunk': chunk,
                'explanation': state.get('explanation', '采集各来源的限定范围；不代表单一时刻的原子快照。'),
                'createRequestId': state['createRequestId'], 'save': save}

    def _unlink_snapshot(self, state):
        name = state['exportId'] + '.zip'
        try:
            info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and _identity(info) == state.get('fileIdentity')):
                os.unlink(name, dir_fd=self.directory)
                os.fsync(self.directory)
        except FileNotFoundError:
            pass

    def _expire(self, state):
        # An accepted save pins its snapshot until its result is definitive.
        request_id = state.get('commitRequestId')
        op = self._read(_operation_key(request_id)) if request_id else None
        if op and op['save']['status'] in ('pending', 'unknown'):
            return state
        if state['status'] != 'released' and now_ms() >= state['expiresAt']:
            self._unlink_snapshot(state)
            state = {**state, 'status': 'released', 'explanation': '临时导出已过期；保存回执仍可核对。'}
            self._write(_state_key(state['exportId']), state)
        return state

    def cleanup(self):
        # UUID filenames only. Lock files and receipts are durable coordination
        # metadata; they are never unlinked while another process may hold them.
        self._check_directory()
        for name in os.listdir(self.directory):
            if not name.endswith('.zip'):
                continue
            identifier = name[:-4]
            try:
                if str(UUID(identifier)) != identifier:
                    continue
            except ValueError:
                continue
            with self._lock(identifier, blocking=False) as acquired:
                if acquired:
                    state = self._read(_state_key(identifier))
                    if state:
                        self._expire(state)
                    else:
                        # The only crash window before the first receipt leaves
                        # an empty reservation. Nonempty/unowned/linked entries
                        # are never inferred to be ours and never removed.
                        info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                                and info.st_size == 0 and stat.S_IMODE(info.st_mode) == 0o600
                                and now_ms() - int(info.st_mtime * 1000) >= self.ttl_ms):
                            os.unlink(name, dir_fd=self.directory)
                            os.fsync(self.directory)

    def _open_snapshot(self, state, *, verify=False):
        self._check_directory()
        fd = os.open(state['exportId'] + '.zip', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=self.directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or _identity(info) != state['fileIdentity'] or info.st_size != state['bytes']
                    or info.st_mtime_ns != state['mtimeNs'] or info.st_ctime_ns != state['ctimeNs']):
                raise ValueError('snapshot-changed')
            if verify:
                _verify_bytes(fd, state['bytes'], state['sha256'])
            return fd
        except BaseException:
            os.close(fd)
            raise

    def create(self, binding, request_id):
        key = _operation_key(request_id)
        # A quota lock coordinates private snapshot reservations across processes.
        with self._lock('quota'):
            self.cleanup()
            with self._lock(key.replace(':', '-')):
                op = self._read(key)
                if op:
                    self._check_owner(op, binding, 'create')
                    return self.status(binding, op['exportId'])
                if sum(name.endswith('.zip') for name in os.listdir(self.directory)) >= self.max_snapshots:
                    raise ExportError('resourceLimit', '临时导出数量已达上限，请释放或等待已有导出到期。', True)
                export_id = str(uuid4())
                lock = self._lock(export_id)
                lock.__enter__()
                state = {'sourceInstanceId': binding['sourceInstanceId'], 'rootThreadId': binding['threadId'],
                         'exportId': export_id, 'createRequestId': request_id, 'status': 'collecting',
                         'expiresAt': now_ms() + self.ttl_ms, 'createdAt': now_ms(), 'cursorSecret': uuid4().hex}
                fd = None
                try:
                    # Persist the identity and operation before collection can write
                    # content. A crashed collecting attempt is never re-enumerated.
                    fd = os.open(export_id + '.zip', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self.directory)
                    os.fchmod(fd, 0o600)
                    state['fileIdentity'] = _identity(os.fstat(fd))
                    self._write(_state_key(export_id), state)
                    self._write(key, {**{k: state[k] for k in ('sourceInstanceId', 'rootThreadId', 'exportId')},
                                      'operationType': 'create', 'requestId': request_id})
                    os.fsync(self.directory)
                except BaseException:
                    if fd is not None:
                        self._unlink_snapshot(state)
                        os.close(fd)
                    lock.__exit__(None, None, None)
                    raise
        try:
            result = self.collector.collect(binding['threadId'], self.root / (export_id + '.zip'), destination_fd=fd)
            info = os.fstat(fd)
            if _identity(info) != state['fileIdentity']:
                raise ValueError('snapshot-replaced')
            _verify_bytes(fd, result['bytes'], result['sha256'])
            candidate = self.destination.reserve(result['bytes'], result['sha256'],
                prefix='codex-display-' + binding['threadId'] + '-' + str(state['createdAt']))
            state.update(status='ready', manifest=result['manifest'], bytes=result['bytes'], sha256=result['sha256'],
                         candidate=candidate, mtimeNs=info.st_mtime_ns, ctimeNs=info.st_ctime_ns)
            if self._read('export-release:' + export_id):
                self._unlink_snapshot(state)
                state.update(status='released', explanation='已停止此导出，未提交保存。')
            self._write(_state_key(export_id), state)
        except Exception as error:
            message = ('全部原始来源均不可读取；未提交保存。' if getattr(error, 'code', None) == 'allSourcesUnreadable'
                       else '原始记录采集未完成；未提交保存。')
            state.update(status='failed', explanation=message)
            if getattr(error, 'manifest', None) is not None:
                state['manifest'] = error.manifest
            self._write(_state_key(export_id), state)
            self._unlink_snapshot(state)
        finally:
            os.close(fd)
            lock.__exit__(None, None, None)
        return self._response(state, binding)

    def status(self, binding, export_id):
        state = self._state(export_id, binding)
        with self._lock(export_id, blocking=False) as acquired:
            if acquired:
                state = self._state(export_id, binding)
                if state['status'] == 'collecting':
                    state.update(status='failed', explanation='采集已中断；未重新枚举来源，也未提交保存。')
                    self._write(_state_key(export_id), state)
                    self._unlink_snapshot(state)
                state = self._expire(state)
        return self._response(state, binding)

    def commit(self, binding, export_id, request_id):
        state = self._state(export_id, binding)
        key = _operation_key(request_id)
        with self._lock(key.replace(':', '-')):
            old = self._read(key)
            if old:
                self._check_owner(old, binding, 'commit', export_id)
                if old.get('phase') != 'accepted':
                    return self.result(binding, request_id)
            lock = self._lock(export_id, blocking=False)
            acquired = lock.__enter__()
            if not acquired:
                lock.__exit__(None, None, None)
                if old:
                    return self._response(state, binding, operation=old)
                raise ExportError('exportNotReady', '此导出正在处理，请用同一请求重试。', True)
            if old:
                try:
                    old = self._read(key)
                except BaseException:
                    lock.__exit__(None, None, None)
                    raise
                # The original publisher does not retain the operation lock
                # during I/O. Its phase may have advanced before this export
                # lock became available; never recover a stale accepted copy.
                if old.get('phase') != 'accepted':
                    lock.__exit__(None, None, None)
                    return self.result(binding, request_id)
            try:
                state = self._expire(self._state(export_id, binding))
                recorded = self._read(_operation_key(state['commitRequestId'])) if state.get('commitRequestId') else None
                if not recorded and self._read('export-release:' + export_id):
                    self._unlink_snapshot(state)
                    state.update(status='released', explanation='此导出已释放，未提交保存。')
                    self._write(_state_key(export_id), state)
                if state['status'] != 'ready':
                    raise ExportError('exportExpired' if state['status'] == 'released' else 'exportNotReady', '导出尚未就绪或已释放。')
                if state.get('commitRequestId') and state['commitRequestId'] != request_id:
                    previous = self._read(_operation_key(state['commitRequestId']))
                    if previous:
                        raise ExportError('idempotencyConflict', '此导出已提交；请核对原保存请求。')
                if old:
                    # Only an accepted intent with no publication attempt can
                    # resume. Unknown publishing attempts are verify-only.
                    op = old
                else:
                    # Persist the pin and fixed candidate before accepting a save.
                    state['commitRequestId'] = request_id
                    self._write(_state_key(export_id), state)
                    op = {**{k: state[k] for k in ('sourceInstanceId', 'rootThreadId', 'exportId')},
                          'operationType': 'commit', 'requestId': request_id, 'candidate': state['candidate'],
                          'phase': 'accepted', 'save': {'status': 'pending', 'requestId': request_id}}
                    self._write(key, op)
            except BaseException:
                lock.__exit__(None, None, None)
                raise
        try:
            fd = self._open_snapshot(state, verify=True)
            try:
                op['phase'] = 'publishing'
                self._write(key, op)
                saved = self.destination.publish_file(fd, op['candidate'])
                op.update(phase='verified', save=self._verified(saved, request_id))
            finally:
                os.close(fd)
        except ExportUnconfirmedError:
            op.update(phase='uncertain', save={'status': 'unknown', 'requestId': request_id, 'reason': 'verificationUnavailable'})
        except Exception as error:
            # A preexisting candidate or a failed receipt write is not proof of
            # nonpublication. Only a known first publication's ordinary failure
            # is definitive; ExportStore reports post-publication uncertainty.
            try:
                saved = self.destination.verify_saved(op['candidate'])
                op.update(phase='verified', save=self._verified(saved, request_id))
            except FileNotFoundError:
                code = ('permissionDenied' if isinstance(error, PermissionError) else
                        'resourceLimit' if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT) else
                        'destinationUnavailable' if isinstance(error, FileNotFoundError) else 'writeFailed')
                op.update(phase='failed', save={'status': 'failed', 'requestId': request_id, 'code': code})
            except Exception:
                op.update(phase='uncertain', save={'status': 'unknown', 'requestId': request_id, 'reason': 'verificationUnavailable'})
        finally:
            try:
                self._write(key, op)
            finally:
                lock.__exit__(None, None, None)
        return self._response(state, binding, operation=op)

    @staticmethod
    def _verified(saved, request_id):
        return {'status': 'verified', 'requestId': request_id, 'filename': saved['filename'], 'path': saved['path'],
                'bytes': saved['bytes'], 'sha256': saved['sha256'], 'verifiedAt': now_ms()}

    def result(self, binding, request_id):
        key = _operation_key(request_id)
        op = self._read(key)
        if not op:
            return self._unknown(binding, request_id)
        self._check_owner(op, binding, 'commit')
        state = self._state(op['exportId'], binding)
        with self._lock(op['exportId'], blocking=False) as acquired:
            if acquired:
                op = self._read(key)
                if op['save']['status'] != 'failed':
                    try:
                        saved = self.destination.verify_saved(op['candidate'])
                        op.update(phase='verified', save=self._verified(saved, request_id))
                    except Exception:
                        # Preserve accepted: only an explicit same-key commit may
                        # resume that known pre-publication stage after restart.
                        op.update(save={'status': 'unknown', 'requestId': request_id, 'reason': 'verificationUnavailable'})
                    self._write(key, op)
        return self._response(state, binding, operation=op)

    def release(self, binding, export_id):
        state = self._state(export_id, binding)
        # This flag is independent of the long collection lock. It cannot undo
        # an accepted publication; collection checks it before offering ready.
        self.store.put_receipt('export-release:' + export_id, {'requested': True})
        with self._lock(export_id, blocking=False) as acquired:
            if not acquired:
                return self._response(state, binding)
            state = self._state(export_id, binding)
            op = self._read(_operation_key(state['commitRequestId'])) if state.get('commitRequestId') else None
            if op and op['save']['status'] in ('pending', 'unknown'):
                return self._response(state, binding, operation=op)
            self._unlink_snapshot(state)
            state.update(status='released', explanation='临时导出已释放；已保存文件与保存回执不受影响。')
            self._write(_state_key(export_id), state)
        return self._response(state, binding, operation=op)

    def read(self, binding, export_id, cursor):
        state = self._state(export_id, binding)
        with self._lock(export_id, blocking=False) as acquired:
            if not acquired or state['status'] != 'ready':
                raise ExportError('exportNotReady', '导出字节尚不可读。', True)
            state = self._expire(self._state(export_id, binding))
            if state['status'] == 'released':
                raise ExportError('exportExpired', '临时导出已过期。')
            scope = json.dumps([binding, export_id], sort_keys=True, separators=(',', ':'))
            def token(offset):
                value = str(offset)
                signature = hmac.new(state['cursorSecret'].encode(), (scope + ':' + value).encode(), hashlib.sha256).hexdigest()
                return value + ':' + signature
            offset = 0
            if cursor is not None:
                try:
                    offset = int(cursor.split(':')[0])
                    if offset < 0 or offset >= state['bytes'] or offset % CHUNK_BYTES or not hmac.compare_digest(cursor, token(offset)):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise ExportError('invalidCursor', '导出游标无效或不属于当前选择。') from None
            try:
                fd = self._open_snapshot(state)
                try:
                    chunk = os.pread(fd, min(CHUNK_BYTES, state['bytes'] - offset), offset)
                    if len(chunk) != min(CHUNK_BYTES, state['bytes'] - offset):
                        raise ValueError('short-snapshot-read')
                    after = os.fstat(fd)
                    named = os.stat(export_id + '.zip', dir_fd=self.directory, follow_symlinks=False)
                    if (after.st_mtime_ns != state['mtimeNs'] or after.st_ctime_ns != state['ctimeNs']
                            or after.st_size != state['bytes'] or _identity(named) != state['fileIdentity']):
                        raise ValueError('snapshot-changed-during-read')
                finally:
                    os.close(fd)
            except Exception:
                raise ExportError('unreadable', '冻结导出文件无法核验。') from None
            chunk_result = {'offset': offset, 'base64': base64.b64encode(chunk).decode(), 'byteLength': len(chunk),
                            'sha256': hashlib.sha256(chunk).hexdigest(),
                            'nextCursor': token(offset + len(chunk)) if offset + len(chunk) < state['bytes'] else None}
            return self._response(state, binding, chunk=chunk_result)

    def handle(self, request):
        binding, action = request['binding'], request['action']
        if action == 'create':
            return self.create(binding, request['requestId'])
        if action == 'commit':
            return self.commit(binding, request['exportId'], request['requestId'])
        if action == 'result':
            return self.result(binding, request['requestId'])
        if action == 'read':
            return self.read(binding, request['exportId'], request['cursor'])
        return getattr(self, action)(binding, request['exportId'])
