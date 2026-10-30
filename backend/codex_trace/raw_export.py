"""按资源预算只读采集来源，以原始字节流写入私有 ZIP。

采集独立于 UI 已加载范围和保存回执；来源身份、继承范围由 RawAdapter 提供，
关系另行翻页枚举。只有已确认的 spawn 后代进入冻结范围，通信或 fork 不扩大范围。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from .platform import filesystem as os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4
import zipfile

from .domain import project, resolve_relationships

_ID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
_SAFE = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_RELATIONS = ('subAgentActivity', 'collabAgentToolCall', 'SubAgentActivity', 'CollabAgentToolCall')
_META_FIELDS = ('id', 'parent_thread_id', 'forked_from_id', 'source')
_ITEM_FIELDS = ('type', 'id', 'agentThreadId', 'agent_thread_id', 'kind', 'senderThreadId',
                'sender_thread_id', 'receiverThreadIds', 'receiver_thread_ids', 'tool')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RawExportError(RuntimeError):
    """向上层提供固定错误码，必要时附带不含任务正文的部分 manifest。"""
    def __init__(self, code, manifest=None):
        self.code, self.manifest = code, manifest
        super().__init__(code)


@dataclass(frozen=True)
class ExportLimits:
    """枚举和复制的独立预算；max_bytes 计算来源字节总量，非压缩后的 ZIP 大小。"""

    max_tasks: int = 64
    max_depth: int = 16
    max_sources: int = 256
    max_history_pages: int = 256
    max_relation_items: int = 8192
    max_bytes: int = 512 * 1024 * 1024
    max_seconds: float = 120
    chunk_bytes: int = 256 * 1024

    def __post_init__(self):
        for name in ('max_tasks', 'max_depth', 'max_sources', 'max_history_pages', 'max_relation_items', 'max_bytes', 'chunk_bytes'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError('positive-export-limits-required')
        if type(self.max_seconds) not in (int, float) or not 0 < self.max_seconds <= 3600:
            raise ValueError('bounded-export-time-required')


def _now():
    return round(time.time() * 1000)


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RawExportError('invalid-thread-id')
    return value.lower()


def _directory(path):
    current = os.open(path.anchor, _DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_source(path):
    directory = _directory(path.parent)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _identity(info):
    return info.st_dev, info.st_ino


def _safe_reason(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9,-]{1,256}', value) else 'source-incomplete'


class RawExportCollector:
    """先冻结来源成员、字节上界和哈希，再复制核验；不声明跨文件原子快照。"""

    def __init__(self, history, raw, *, limits=None):
        self.history, self.raw = history, raw
        self.limits = limits or ExportLimits()

    def _check_time(self, deadline):
        if time.monotonic() > deadline:
            raise RawExportError('export-time-limit')

    def _relationship_rows(self, rows, unresolved, identifier):
        """只给关系 reducer 保留必要字段；坏行/超限仍会使关系枚举标记为不完整。"""
        selected, relation_count, limited = [], 0, False
        for row in rows:
            record, payload = row['record'], row['record'].get('payload', {})
            if not isinstance(payload, dict):
                continue
            kind = record.get('type')
            if kind == 'session_meta':
                # 关系只依赖显式元数据，避免额外复制系统指令或普通消息正文。
                selected.append({**row, 'raw': b'', 'record': {**record, 'payload': {key: payload[key] for key in _META_FIELDS if key in payload}}})
            elif kind == 'event_msg' and (payload.get('type') in ('task_started', 'turn_started', 'task_complete', 'turn_complete', 'turn_aborted', 'sub_agent_activity', 'collab_agent_spawn_end')
                    or payload.get('type') == 'item_completed' and isinstance(payload.get('item'), dict) and payload['item'].get('type') in _RELATIONS):
                if payload.get('type') in ('item_completed', 'sub_agent_activity', 'collab_agent_spawn_end'):
                    relation_count += 1
                    if relation_count > self.limits.max_relation_items:
                        limited = True
                        continue
                values = {key: payload[key] for key in ('type', 'thread_id', 'turn_id', 'event_id', 'agent_thread_id',
                          'kind', 'sender_thread_id', 'call_id', 'new_thread_id', 'started_at_ms', 'completed_at_ms', 'occurred_at_ms') if key in payload}
                if isinstance(payload.get('item'), dict):
                    values['item'] = {key: payload['item'][key] for key in _ITEM_FIELDS if key in payload['item']}
                selected.append({**row, 'raw': b'', 'record': {**record, 'payload': values}})
            elif kind == 'turn_context' and isinstance(payload.get('turn_id'), str):
                selected.append({**row, 'raw': b'', 'record': {**record, 'payload': {'turn_id': payload['turn_id']}}})
        if limited:
            unresolved.append(f'raw-relationship-item-limit:{identifier}')
        return selected

    def _read_relations(self, identifier, rows, unresolved, deadline):
        """使用独立的完整历史游标查找关系，触限或故障写入未解决范围。"""
        candidates, cursor, seen = [], None, set()
        for _ in range(self.limits.max_history_pages):
            try:
                self._check_time(deadline)
                if hasattr(self.history, 'list_items'):
                    page = self.history.list_items(identifier, cursor=cursor, limit=200)
                    items = page['data']
                else:
                    page = self.history.read_history(identifier, max_items=200, cursor=cursor)
                    items = page['items']
            except Exception:
                unresolved.append(f'history-enumeration-unavailable:{identifier}')
                break
            for wrapper in items:
                item = wrapper.get('item', {})
                if item.get('type') in _RELATIONS:
                    if len(candidates) >= self.limits.max_relation_items:
                        unresolved.append(f'relationship-item-limit:{identifier}')
                        cursor = None
                        break
                    candidates.append({key: wrapper[key] for key in ('turnId', 'startedAtMs', 'completedAtMs') if key in wrapper})
                    candidates[-1]['item'] = {key: item[key] for key in _ITEM_FIELDS if key in item}
            else:
                next_cursor = page.get('nextCursor')
                if next_cursor is None:
                    break
                if next_cursor in seen or next_cursor == cursor:
                    unresolved.append(f'history-cursor-cycle:{identifier}')
                    break
                seen.add(next_cursor)
                cursor = next_cursor
                continue
            break
        else:
            unresolved.append(f'history-page-limit:{identifier}')
        # API 按倒序返回；逆序后仍以精确 ID 合并 raw 中的完成记录。
        return project(identifier, self._relationship_rows(rows, unresolved, identifier), reversed(candidates), mode='api')['relationships']

    def _enumerate(self, root, unresolved, deadline):
        """有界遍历已确认后代；最终重算可达范围，剔除后续变成冲突的父子关系。"""
        tasks, relationships, queue = {}, [], [(root, 0)]
        while queue:
            identifier, depth = queue.pop(0)
            if identifier in tasks:
                continue
            if depth > self.limits.max_depth or len(tasks) >= self.limits.max_tasks:
                unresolved.append(f'task-enumeration-limit:{identifier}')
                continue
            try:
                self._check_time(deadline)
            except RawExportError:
                unresolved.append(f'task-enumeration-time-limit:{identifier}')
                break
            try:
                metadata = self.history.read(identifier)
            except Exception:
                metadata = {'id': identifier}
                unresolved.append(f'history-metadata-unavailable:{identifier}')
            if metadata.get('id') != identifier:
                unresolved.append(f'thread-identity-mismatch:{identifier}')
                tasks[identifier] = {'segments': [], 'sources': []}
                continue
            try:
                snapshot = self.raw.read(identifier, metadata)
            except Exception:
                unresolved.append(f'raw-enumeration-unavailable:{identifier}')
                tasks[identifier] = {'segments': [], 'sources': []}
                continue
            tasks[identifier] = {'segments': snapshot.segments, 'sources': snapshot.sources}
            # 坏行或投影限制可能隐藏创建证据；原字节可导出不代表后代已完整枚举。
            for issue in snapshot.issues:
                if issue != 'incomplete-tail':
                    unresolved.append(f'raw-enumeration-{_safe_reason(issue)}:{identifier}')
            relationships.extend(self._read_relations(identifier, snapshot.records, unresolved, deadline))
            relationships = resolve_relationships(relationships)
            for edge in relationships:
                if edge['type'] == 'spawned-by' and edge['resolution'] == 'confirmed' and edge['toThreadId'] == identifier:
                    try:
                        child = _id(edge['fromThreadId'])
                    except RawExportError:
                        unresolved.append(f'invalid-child-identity:{edge["relationshipId"]}')
                        continue
                    if child not in tasks:
                        queue.append((child, depth + 1))
        relationships = resolve_relationships(relationships)
        for edge in relationships:
            if edge['resolution'] != 'confirmed':
                unresolved.append(f'relationship-{edge["resolution"]}:{edge["relationshipId"]}')
        reachable, pending = {root}, [root]
        while pending:
            parent = pending.pop()
            for edge in relationships:
                if edge['type'] == 'spawned-by' and edge['resolution'] == 'confirmed' and edge['toThreadId'] == parent and edge['fromThreadId'] not in reachable:
                    reachable.add(edge['fromThreadId'])
                    pending.append(edge['fromThreadId'])
        return {key: value for key, value in tasks.items() if key in reachable}, relationships

    def _hash_range(self, descriptor, length, deadline, *, prefix_length=None):
        digest, prefix = hashlib.sha256(), hashlib.sha256()
        offset, last = 0, b''
        while offset < length:
            self._check_time(deadline)
            chunk = os.pread(descriptor, min(self.limits.chunk_bytes, length-offset), offset)
            if not chunk:
                raise RawExportError('source-truncated')
            digest.update(chunk)
            if prefix_length is not None and offset < prefix_length:
                prefix.update(chunk[:prefix_length-offset])
            offset += len(chunk)
            last = chunk[-1:]
        return digest.hexdigest(), prefix.hexdigest(), last

    def _freeze_source(self, segment, remaining, deadline):
        """固定单来源可复制前缀并校验身份；追加可继续，已冻结前缀不能变化。"""
        if not segment.get('exportable', False):
            raise RawExportError('unsafe-inherited-range')
        descriptor = _open_source(Path(segment['path']))
        try:
            before = os.fstat(descriptor)
            expected = segment['device'], segment['inode']
            if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
                raise RawExportError('source-replaced')
            if before.st_size < segment['byteEnd']:
                raise RawExportError('source-truncated')
            compressed = segment['format'] != 'jsonl'
            # 压缩文件的物理字节与逻辑偏移不能互换，故要求枚举后整份文件未变。
            if compressed and (before.st_size != segment['physicalBytes'] or before.st_mtime_ns != segment['mtimeNs'] or before.st_ctime_ns != segment['ctimeNs']):
                raise RawExportError('compressed-source-changed')
            upper = segment['byteEnd'] if segment.get('inheritedRange') is not None else before.st_size
            # 继承段仅复制已证明属于本任务的上界，不能带入父任务后来追加的内容。
            copy_end = min(upper, remaining)
            if copy_end <= 0:
                raise RawExportError('export-byte-limit')
            verified_prefix = segment.get('readBytes', 0) if not compressed else 0
            if verified_prefix <= copy_end:
                digest, prefix, last = self._hash_range(descriptor, copy_end, deadline, prefix_length=verified_prefix)
            else:
                prefix, _, _ = self._hash_range(descriptor, verified_prefix, deadline)
                digest, _, last = self._hash_range(descriptor, copy_end, deadline)
            if verified_prefix and prefix != segment['readSha256']:
                raise RawExportError('source-prefix-changed')
            after = os.fstat(descriptor)
            if _identity(after) != expected or after.st_size < upper:
                raise RawExportError('source-changed-during-freeze')
            if after.st_size == before.st_size and (after.st_mtime_ns, after.st_ctime_ns) != (before.st_mtime_ns, before.st_ctime_ns):
                raise RawExportError('source-changed-during-freeze')
            return {'segment': segment, 'upper': upper, 'end': copy_end, 'hash': digest,
                    'identity': expected, 'partialTail': not compressed and last != b'\n'}
        finally:
            os.close(descriptor)

    def _copy_source(self, frozen, directory, archive, entry_name, deadline):
        """先复制至匿名暂存并复核哈希，再开 ZIP 条目，避免未校验字节进入 ZIP。"""
        descriptor = _open_source(Path(frozen['segment']['path']))
        name = '.segment-' + uuid4().hex
        spool = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or _identity(info) != frozen['identity'] or info.st_size < frozen['upper']:
                raise RawExportError('source-changed-before-copy')
            try:
                spool = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                os.unlink(name, dir_fd=directory)  # 匿名私有暂存，关闭描述符即释放。
            except OSError as error:
                raise RawExportError('private-copy-failed') from error
            offset, digest = 0, hashlib.sha256()
            while offset < frozen['end']:
                self._check_time(deadline)
                chunk = os.pread(descriptor, min(self.limits.chunk_bytes, frozen['end']-offset), offset)
                if not chunk:
                    raise RawExportError('source-truncated')
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    try:
                        count = os.write(spool, view)
                    except OSError as error:
                        raise RawExportError('private-copy-failed') from error
                    if count <= 0:
                        raise RawExportError('private-copy-failed')
                    view = view[count:]
                offset += len(chunk)
            if digest.hexdigest() != frozen['hash']:
                raise RawExportError('source-prefix-changed')
            checked, _, _ = self._hash_range(descriptor, frozen['end'], deadline)
            after = os.fstat(descriptor)
            if checked != frozen['hash'] or after.st_size < frozen['upper']:
                raise RawExportError('source-changed-during-copy')
            current = _open_source(Path(frozen['segment']['path']))
            try:
                if _identity(os.fstat(current)) != frozen['identity']:
                    raise RawExportError('source-replaced')
            finally:
                os.close(current)
            entry = zipfile.ZipInfo(entry_name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            try:
                with archive.open(entry, 'w', force_zip64=True) as target:
                    os.lseek(spool, 0, os.SEEK_SET)
                    while chunk := os.read(spool, self.limits.chunk_bytes):
                        self._check_time(deadline)
                        target.write(chunk)
            except OSError as error:
                raise RawExportError('zip-write-failed') from error
            except RawExportError as error:
                # ZIP 条目头写入后再超时，必须令整包失败，不能降级为成功包中的缺源。
                raise RawExportError('zip-time-limit') from error
        finally:
            os.close(descriptor)
            if spool is not None:
                os.close(spool)

    def collect(self, root_thread_id, destination_path, *, destination_fd=None):
        """生成一次冻结 ZIP；未知类型、坏行和末尾半行按原字节保留在来源条目中。"""
        root = _id(root_thread_id)
        path = Path(destination_path)
        if not path.is_absolute() or '..' in path.parts or path.suffix != '.zip':
            raise RawExportError('invalid-private-destination')
        directory = _directory(path.parent)
        output = None
        owned = destination_fd is None
        identity = None
        started, deadline = _now(), time.monotonic() + self.limits.max_seconds
        manifest = {'schemaVersion': '1.0.0', 'rootThreadId': root, 'enumerationCutoff': started,
                    'collectionStartedAt': started, 'collectionEndedAt': None, 'atomicSnapshot': False,
                    'relationships': [], 'sources': [], 'enumerationComplete': False, 'unresolvedRanges': []}
        unresolved = manifest['unresolvedRanges']
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise RawExportError('private-directory-required')
            output = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory) if owned else os.dup(destination_fd)
            info = os.fstat(output)
            named = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            identity = _identity(info)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size != 0 or stat.S_IMODE(info.st_mode) & 0o077 or identity != _identity(named):
                raise RawExportError('invalid-private-destination')
            os.lseek(output, 0, os.SEEK_SET)
            tasks, relations = self._enumerate(root, unresolved, deadline)
            manifest['relationships'] = relations
            manifest['enumerationCutoff'] = _now()
            # 全部冻结计划先于 ZIP 条目写入；每源采集时间不同，manifest 明确 atomicSnapshot=False。
            plans, remaining = [], self.limits.max_bytes
            for logical_id, task in tasks.items():
                represented = set()
                for segment in task['segments']:
                    source_id = segment['segmentId']
                    if not _SAFE.fullmatch(source_id):
                        unresolved.append(f'invalid-segment-id:{logical_id}')
                        continue
                    represented.add(source_id)
                    entry = {'sourceId': source_id, 'threadId': logical_id, 'segmentId': source_id,
                             'generation': segment['generation'], 'entryName': None,
                             'collectedFrom': _now(), 'collectedThrough': _now(),
                             'frozenUpperBound': segment['byteEnd'], 'copiedRange': None, 'sha256': None,
                             'logicalInheritedRange': segment.get('inheritedRange'), 'trailingPartialLine': False,
                             'state': 'unreadable', 'reason': None}
                    manifest['sources'].append(entry)
                    try:
                        if len(plans) >= self.limits.max_sources:
                            raise RawExportError('export-source-limit')
                        frozen = self._freeze_source(segment, remaining, deadline)
                        entry['frozenUpperBound'] = frozen['upper']
                        plans.append((entry, frozen))
                        remaining -= frozen['end']
                    except (OSError, RawExportError) as error:
                        entry['state'] = 'unsafeInheritedRange' if segment.get('inheritedRange') is not None and not segment.get('exportable') else 'unreadable'
                        entry['reason'] = error.code if isinstance(error, RawExportError) else 'source-unreadable'
                        unresolved.append(f'{entry["reason"]}:{logical_id}:{source_id}')
                    entry['collectedThrough'] = _now()
                for source in task['sources']:
                    if source['sourceId'] in represented:
                        continue
                    manifest['sources'].append({'sourceId': source['sourceId'], 'threadId': logical_id,
                        'segmentId': source['sourceId'], 'generation': source['generation'], 'entryName': None,
                        'collectedFrom': _now(), 'collectedThrough': _now(), 'frozenUpperBound': None,
                        'copiedRange': None, 'sha256': None, 'logicalInheritedRange': source.get('inheritedRange'),
                        'trailingPartialLine': False, 'state': 'unreadable', 'reason': _safe_reason(source.get('reason'))})
                    unresolved.append(f'source-unavailable:{logical_id}:{source["sourceId"]}')
            copied = 0
            with os.fdopen(os.dup(output), 'w+b') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                for entry, frozen in plans:
                    suffix = '.jsonl' if frozen['segment']['format'] == 'jsonl' else '.bin'
                    name = f'records/{entry["threadId"]}/{entry["segmentId"]}{suffix}'
                    try:
                        self._copy_source(frozen, directory, archive, name, deadline)
                        entry.update({'entryName': name, 'copiedRange': {'start': 0, 'end': frozen['end']},
                                      'sha256': frozen['hash'], 'trailingPartialLine': frozen['partialTail'],
                                      'state': 'copied' if frozen['end'] == frozen['upper'] else 'partial',
                                      'reason': None if frozen['end'] == frozen['upper'] else 'export-byte-limit'})
                        copied += 1
                        if entry['reason']:
                            unresolved.append(f'export-byte-limit:{entry["threadId"]}:{entry["segmentId"]}')
                    except (OSError, RawExportError) as error:
                        if isinstance(error, RawExportError) and error.code in ('private-copy-failed', 'zip-write-failed', 'zip-time-limit'):
                            raise
                        entry['state'] = 'unreadable'
                        entry['reason'] = error.code if isinstance(error, RawExportError) else 'source-copy-unavailable'
                        unresolved.append(f'{entry["reason"]}:{entry["threadId"]}:{entry["segmentId"]}')
                    entry['collectedThrough'] = _now()
                manifest['collectionEndedAt'] = _now()
                manifest['unresolvedRanges'] = list(dict.fromkeys(unresolved))
                manifest['enumerationComplete'] = not manifest['unresolvedRanges']
                # 可生成并校验部分来源 ZIP；保存成功与来源完整是两个独立结论。
                if copied == 0:
                    raise RawExportError('allSourcesUnreadable', manifest)
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode() + b'\n')
            os.fsync(output)
            os.fsync(directory)
            check = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(check)
                if _identity(info) != identity:
                    raise RawExportError('private-destination-changed')
                digest, _, _ = self._hash_range(check, info.st_size, deadline)
                return {'manifest': manifest, 'bytes': info.st_size, 'sha256': digest}
            finally:
                os.close(check)
        except BaseException:
            if owned and identity is not None:
                try:
                    if _identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False)) == identity:
                        os.unlink(path.name, dir_fd=directory)
                except OSError:
                    pass
            raise
        finally:
            if output is not None:
                os.close(output)
            os.close(directory)
