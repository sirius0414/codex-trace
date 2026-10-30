"""持久导出状态机：采集快照和保存回执独立于页面观察状态。

requestId 跨进程幂等；锁仅协调本插件的私有文件，不锁定或写入 Codex 来源。
采集、复制等耗时 I/O 不持有 SQLite 事务；回执先落盘，之后才允许最终文件发布。
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import errno
import hashlib
import hmac
import json
from .platform import filesystem as os
from pathlib import Path
import stat
import threading
import time
from uuid import UUID, uuid4

from .export_store import ExportStore, ExportUnconfirmedError, _open_chain, _verify_bytes
from .storage import _open_directory
from .platform.locks import lock_exclusive, unlock

CHUNK_BYTES = 1024 * 1024
# 仅临时快照自动过期；pending/unknown 保存会固定所需材料，持久回执不自动删除。
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
    """协调冻结 ZIP、同键保存和恢复；ready 后仍须 save.status=verified 才能确认保存。"""

    def __init__(self, store, collector, destination=None, *, ttl_ms=TTL_MS, max_snapshots=8):
        self.store, self.collector = store, collector
        self.destination = destination or ExportStore()
        self.root = store.root / 'exports'
        self.directory = _open_directory(self.root)
        self.directory_identity = _identity(os.fstat(self.directory))
        self.ttl_ms, self.max_snapshots = ttl_ms, max_snapshots
        self.stop = threading.Event()
        self.janitor = threading.Thread(target=self._janitor, name='codex-trace-export-cleanup', daemon=True)
        self.janitor.start()

    def _janitor(self):
        while not self.stop.wait(60):
            try:
                self.cleanup()
            except Exception:
                # 私有目录异常时停止本轮清理，不能扩大删除范围来“修复”状态。
                pass

    def close(self):
        self.stop.set()
        self.janitor.join(timeout=2)
        if self.directory != -1:
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
        """用持久锁文件协调多进程；回收锁文件会破坏仍持锁进程的互斥关系。"""
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
                lock_exclusive(fd, blocking=blocking)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            try:
                if acquired:
                    unlock(fd)
            finally:
                os.close(fd)

    def _read(self, key):
        result = self.store.get_receipt(key)
        return result['value'] if result else None

    def _write(self, key, value):
        """以回执版本作 CAS，防止进程间静默覆盖恢复依据。"""
        previous = self.store.get_receipt(key)
        result = self.store.put_receipt(key, value, previous['revision'] if previous else 0)
        if not result['saved']:
            raise ExportError('revisionConflict', '导出记录正在更新，请用同一请求重试。', True)

    @staticmethod
    def _check_owner(value, binding, operation=None, export_id=None):
        """幂等所有权绑定来源、根任务和操作；页面 epoch 仅用于响应路由。"""
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
        """查不到回执也不能证明未发布；返回 unknown，禁止据此新建替代保存。"""
        return {'binding': dict(binding), 'exportId': None, 'status': 'unknown', 'filename': None,
                'totalBytes': None, 'expiresAt': None, 'manifest': None, 'chunk': None,
                'explanation': '保存结果尚未确认；请核对同一次请求，不要据此判断文件未保存。',
                'createRequestId': None,
                'save': {'status': 'unknown', 'requestId': request_id, 'reason': reason}}

    def _response(self, state, binding, *, operation=None, chunk=None):
        """按本次请求的 Binding 回包，同时保持快照状态与保存状态相互独立。"""
        if operation is None and state.get('commitRequestId'):
            operation = self._read(_operation_key(state['commitRequestId']))
        save = deepcopy(operation['save']) if operation else {'status': 'notRequested', 'requestId': None}
        return {'binding': dict(binding), 'exportId': state['exportId'], 'status': state['status'],
                'filename': state.get('candidate', {}).get('filename'), 'totalBytes': state.get('bytes'),
                'expiresAt': state['expiresAt'], 'manifest': deepcopy(state.get('manifest')), 'chunk': chunk,
                'explanation': state.get('explanation', '采集各来源的限定范围；不代表单一时刻的原子快照。'),
                'createRequestId': state['createRequestId'], 'save': save}

    def _unlink_snapshot(self, state):
        """只删除身份仍匹配的私有临时 ZIP，不触及最终导出或回执。"""
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
        # 已接受的保存必须保留恢复材料，直至结果明确；过期不等于取消发布。
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
        """仅清理可确认归属且未被未决保存固定的临时快照。"""
        # 只识别 UUID ZIP；锁文件和回执是持久协调元数据，不随临时快照删除。
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
                        # 首次回执前的崩溃只会留下空预留文件；非空、非本用户或多链接
                        # 文件均不能凭文件名推定归属并删除。
                        info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                                and info.st_size == 0 and stat.S_IMODE(info.st_mode) == 0o600
                                and now_ms() - int(info.st_mtime * 1000) >= self.ttl_ms):
                            os.unlink(name, dir_fd=self.directory)
                            os.fsync(self.directory)

    def _open_snapshot(self, state, *, verify=False):
        """按持久身份、尺寸和时间戳验证冻结文件，可额外校验完整 SHA-256。"""
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
        """同一 create 键只采集一次；中断后也不能重新枚举出另一份来源范围。"""
        key = _operation_key(request_id)
        # 配额锁仅覆盖预留阶段，防止多进程同时越过临时快照数量上限。
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
                    # 开始采集正文前持久化文件身份和操作；崩溃恢复只报告原尝试结果。
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
            # Windows 的最后写入时间可能在写句柄关闭时才最终更新。先关闭所有采集
            # 写句柄，再按同一身份只读重开并读回，ready 回执才能保存稳定的版本。
            os.close(fd)
            fd = None
            fd = os.open(export_id + '.zip', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=self.directory)
            info = os.fstat(fd)
            if _identity(info) != state['fileIdentity']:
                raise ValueError('snapshot-replaced')
            _verify_bytes(fd, result['bytes'], result['sha256'])
            candidate = self.destination.reserve(result['bytes'], result['sha256'],
                prefix='codex-trace-' + binding['threadId'] + '-' + str(state['createdAt']))
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
            if fd is not None:
                os.close(fd)
            lock.__exit__(None, None, None)
        return self._response(state, binding)

    def status(self, binding, export_id):
        """查询原采集；能获得快照锁却仍处于 collecting 表明原进程已中断。"""
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
        """先记录保存意图和唯一候选名，再发布；重复请求只恢复原操作。"""
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
                # 发布 I/O 不持有操作锁；获得快照锁前 phase 可能已推进，必须重读，
                # 否则旧 accepted 副本会误授权第二次发布。
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
                    # 只有尚未尝试发布的 accepted 阶段可续写；开始发布后结果不明
                    # 只能核验原候选，不能换名重存。
                    op = old
                else:
                    # 接受保存前先固定快照和候选文件名，使重启恢复不会另选目标。
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
            # 异常不代表未发布：先核验原候选。只有确认候选不存在才返回 failed；
            # 已发布但 fsync/读回失败由 ExportStore 报为结果未确认。
            try:
                saved = self.destination.verify_saved(op['candidate'])
                op.update(phase='verified', save=self._verified(saved, request_id))
            except FileNotFoundError:
                code = ('permissionDenied' if isinstance(error, PermissionError) else
                        'resourceLimit' if isinstance(error, OSError) and error.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else
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
        """只核验原候选与更新回执，不发起保存；缺失回执必须返回 unknown。"""
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
                        # 保留 accepted phase；重启后只有显式同键 commit 能继续
                        # 这个已知尚未发布的阶段，result 查询本身不能触发写文件。
                        op.update(save={'status': 'unknown', 'requestId': request_id, 'reason': 'verificationUnavailable'})
                    self._write(key, op)
        return self._response(state, binding, operation=op)

    def release(self, binding, export_id):
        """释放临时资源；已接受的保存、最终 ZIP 和持久回执继续有效。"""
        state = self._state(export_id, binding)
        # 独立标记让采集长锁外也能请求释放；不能撤销已接受发布，采集在 ready 前检查。
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
        """提供有上限且可重复的字节块；HMAC 游标绑定当前页面选择与冻结导出。"""
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
        """统一导出入口，不接受 UI 筛选、分页范围或本地目标路径。"""
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
