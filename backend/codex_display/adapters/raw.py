"""只读、限量地读取 rollout 物理片段，并验证明确的继承字节/ordinal 边界。

继承格式对应 codex-rs/protocol/src/protocol.rs 的 HistoryPosition 及
thread-store/src/local/rollout_lineage.rs。history_base.thread_id 指 rollout，
revert 后可能与其 owner thread 不同；文件名或 mtime 不能证明逻辑继承。

RawSnapshot 的路径与字节正文只供服务内部使用，不得进入诊断；公开契约只使用
SourceRef/SourceCoverage 元数据。目录、字节、行数和继承深度均有独立上限。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .watch import SourceWatcher

_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_ID = re.compile(_UUID + r"\Z")
_NAME = re.compile(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(" + _UUID + r")(?:(?:_)(" + _UUID + r"))?\.jsonl(?:\.zst)?\Z")
_KNOWN = frozenset({"session_meta", "response_item", "event_msg", "turn_context", "compacted", "token_usage_record"})
_MAX_INTEGER = 2**53 - 1


@dataclass
class RawSnapshot:
    """一次有明确边界的来源观测；可含部分读取及其原因，不代表历史覆盖完整。"""
    records: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""
    generation: int = 0
    issues: list[str] = field(default_factory=list)


class RawError(ValueError):
    """错误只使用固定代码，避免路径或记录正文经工具错误泄露。"""


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RawError("invalid-thread-id")
    return value.lower()


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= _MAX_INTEGER


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _filename(path: Path) -> tuple[str, str] | None:
    match = _NAME.fullmatch(path.name)
    return (match[1].lower(), (match[2] or match[1]).lower()) if match else None


def _open(path: Path) -> int:
    """逐层使用目录描述符拒绝符号链接，阻止父目录被替换后绕过路径检查。"""
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def _json(data: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(data, parse_constant=lambda _: None)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


class RawAdapter:
    """拥有来源缓存与 watcher；调用方必须 close，不能靠文件通知跳过读取校验。"""
    def __init__(self, session_roots: list[Path], *, max_bytes: int = 64 * 1024 * 1024,
                 max_line_bytes: int = 4 * 1024 * 1024, max_records: int = 100_000,
                 max_segments: int = 64, max_catalog_entries: int = 20_000):
        if not session_roots or any(not Path(root).is_absolute() or ".." in Path(root).parts for root in session_roots):
            raise ValueError("absolute-session-roots-required")
        for number in (max_bytes, max_line_bytes, max_records, max_segments, max_catalog_entries):
            if type(number) is not int or number <= 0:
                raise ValueError("positive-resource-limits-required")
        self.roots = tuple(Path(root) for root in session_roots)
        self.max_bytes = max_bytes
        self.max_line_bytes = max_line_bytes
        self.max_records = max_records
        self.max_physical_lines = max(10_000, max_records * 4)
        self.max_segments = max_segments
        self.max_catalog_entries = max_catalog_entries
        self._lock = threading.RLock()
        self._versions: dict[str, dict[str, Any]] = {}
        self._thread_versions: dict[str, tuple[str, int]] = {}
        self._loads = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = max(128 * 1024 * 1024, max_bytes)
        self._watcher = SourceWatcher()
        # 性能计数只含数字，属于内部指标，不作为工具结果返回。
        self.metrics = {"statChecks": 0, "readBytes": 0, "verifiedBytes": 0,
                        "parsedBytes": 0, "parsedLines": 0, "cacheHits": 0,
                        "appendReads": 0, "rebuilds": 0}

    def wait_for_changes(self, timeout: float = .25) -> bool:
        """最多短暂等待变化提示；提示不是内容变更或任务状态的证据。"""
        return self._watcher.wait_for_changes(timeout)

    def close(self) -> None:
        """回收本适配器持有的监听描述符与正文缓存，不触碰会话源文件。"""
        self._watcher.close()
        with self._lock:
            self._loads.clear()
            self._cache_bytes = 0

    def _read_bytes(self, descriptor, length, offset=0):
        chunks = []
        while length > 0:
            data = os.pread(descriptor, min(1024 * 1024, length), offset)
            if not data:
                break
            self.metrics["readBytes"] += len(data)
            chunks.append(data)
            offset += len(data)
            length -= len(data)
        return b"".join(chunks)

    def _hash_prefix(self, descriptor, length):
        """重验缓存前缀；即使文件变大，也须先证明旧字节未被原地改写。"""
        digest = hashlib.sha256()
        offset = 0
        while offset < length:
            data = os.pread(descriptor, min(1024 * 1024, length - offset), offset)
            if not data:
                raise RawError("source-changed-during-read")
            self.metrics["verifiedBytes"] += len(data)
            digest.update(data)
            offset += len(data)
        return digest.hexdigest()

    @staticmethod
    def _stat_identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _cache_load(self, key, value):
        """同时按条目数与缓存字节淘汰最久未用的来源，控制多任务内存占用。"""
        old = self._loads.pop(key, None)
        self._cache_bytes -= len(old["data"]) if old else 0
        self._loads[key] = value
        self._cache_bytes += len(value["data"])
        while len(self._loads) > 128 or self._cache_bytes > self._cache_limit:
            self._cache_bytes -= len(self._loads.popitem(last=False)[1]["data"])

    def _safe_path(self, value: Any) -> Path:
        """限制在允许根目录内的规范绝对路径；打开时还会再次逐级校验。"""
        if not isinstance(value, (str, Path)):
            raise RawError("source-not-recorded")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or not any(path.is_relative_to(root) for root in self.roots):
            raise RawError("outside-session-roots")
        if path.resolve() != path:
            raise RawError("symlink-or-noncanonical-source")
        return path

    def _find(self, *, rollout_id: str | None = None, basename: str | None = None,
              owner_id: str | None = None) -> Path:
        """限量查找唯一物理片段；多个候选必须报歧义，不能选最新文件猜归属。"""
        matches: list[Path] = []
        count = 0
        for root in self.roots:
            # 遍历不跟随符号链接；读正文前仍会独立验证规范路径和目录描述符。
            if root.is_symlink():
                continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                depth = len(Path(directory).relative_to(root).parts)
                dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink()) if depth < 8 else []
                count += len(dirs) + len(files)
                if count > self.max_catalog_entries:
                    raise RawError("source-catalog-limit")
                for name in files:
                    parsed = _filename(Path(name))
                    if basename is not None:
                        wanted = name in (basename, basename + ".zst")
                    elif rollout_id is not None:
                        wanted = parsed is not None and parsed[1] == rollout_id
                    else:
                        wanted = parsed is not None and parsed[0] == owner_id
                    if wanted:
                        matches.append(Path(directory) / name)
        if not matches:
            raise RawError("source-missing")
        # 同名明文与压缩文件不能假定内容等价；多个候选不会按 mtime 自动决断。
        if len(matches) != 1:
            raise RawError("ambiguous-source")
        return self._safe_path(matches[0])

    def _path(self, thread_id: str, metadata: dict[str, Any]) -> Path:
        recorded = metadata.get("path")
        if not recorded:
            return self._find(owner_id=thread_id)
        path = self._safe_path(recorded)
        try:
            path.lstat()
        except FileNotFoundError:
            # 归档移动保留物理文件名；重新找到后仍须校验头部身份，不以 mtime 认领。
            return self._find(basename=path.name)
        return path

    @staticmethod
    def _source_id(thread_id: str, rollout_id: str) -> str:
        return "raw-" + _digest((thread_id + ":" + rollout_id).encode())[:32]

    def _coverage(self, source_id: str, thread_id: str, *, state: str, reason: str | None,
                  generation: int = 0, format: str = "jsonl", end: int | None = None,
                  consumed: int | None = None, inherited: dict | None = None) -> dict[str, Any]:
        return {"sourceId": source_id, "threadId": thread_id, "generation": generation,
                "format": format, "state": state, "readableRange": {"start": 0, "end": end} if end is not None else None,
                "consumedThrough": consumed, "inheritedRange": inherited, "reason": reason}

    def _load(self, path: Path, owner: str | None, rollout_id: str | None,
              cutoff: dict | None, remaining: int) -> dict[str, Any]:
        """冻结本段可读字节并重验前缀；只在旧前缀可信时复用解析检查点。"""
        path = self._safe_path(path)
        descriptor = _open(path)
        try:
            before = os.fstat(descriptor)
            self.metrics["statChecks"] += 1
            if not stat.S_ISREG(before.st_mode):
                raise RawError("not-regular-source")
            physical_size = before.st_size
            if physical_size > _MAX_INTEGER:
                raise RawError("source-size-limit")
            compressed = path.name.endswith(".jsonl.zst")
            end = cutoff["end_byte_offset"] if cutoff else physical_size
            if not compressed and (end > physical_size or end <= 0):
                raise RawError("inheritance-byte-boundary-invalid" if cutoff else "empty-source")
            key = (str(path), cutoff["end_byte_offset"] if cutoff else None, cutoff["end_ordinal_exclusive"] if cutoff else None)
            cached = self._loads.get(key)
            target = min(end, remaining) if not compressed else 0
            same_file = cached and (before.st_dev, before.st_ino) == (cached["stat"].st_dev, cached["stat"].st_ino)
            unchanged = same_file and self._stat_identity(before) == self._stat_identity(cached["stat"]) and len(cached["data"]) == target
            prefix_verified = False
            parsed = None
            if unchanged:
                # 稳定轮询仍安全打开并验证描述符身份，但不重复读正文、哈希或 JSON 解码。
                data, first = cached["data"], cached["first"]
                digest = cached["hasher"]
                parsed = cached.get("parsed")
                prefix_verified = True
                self.metrics["cacheHits"] += 1
            elif compressed:
                try:
                    from compression import zstd
                    with os.fdopen(os.dup(descriptor), "rb") as stream, zstd.open(stream, "rb") as decoded:
                        first = decoded.readline(self.max_line_bytes + 1)
                except (ImportError, OSError, ValueError):
                    raise RawError("compressed-header-unsupported") from None
                data, digest = b"", hashlib.sha256()
            else:
                # stat 变大不证明只追加；先验证已捕获前缀，再读新字节，并从完整行边界续解码。
                if same_file and target >= len(cached["data"]) and physical_size >= cached["physicalBytes"]:
                    prefix_verified = self._hash_prefix(descriptor, len(cached["data"])) == cached["dataHash"]
                if prefix_verified:
                    added = self._read_bytes(descriptor, target - len(cached["data"]), len(cached["data"]))
                    data = cached["data"] + added
                    first = cached["first"]
                    digest = cached["hasher"].copy()
                    digest.update(added)
                    parsed = cached.get("parsed")
                    self.metrics["appendReads"] += bool(added)
                else:
                    data = self._read_bytes(descriptor, target)
                    first = data.split(b"\n", 1)[0] + (b"\n" if b"\n" in data else b"")
                    digest = hashlib.sha256(data)
                    self.metrics["rebuilds"] += 1
                if len(data) != target:
                    raise RawError("source-changed-during-read")
            if len(first) > self.max_line_bytes or not first.endswith(b"\n"):
                raise RawError("header-limit-or-incomplete")
            header = cached["header"] if prefix_verified else _json(first)
            payload = header.get("payload") if header else None
            if not header or header.get("type") != "session_meta" or not isinstance(payload, dict):
                raise RawError("source-identity-mismatch")
            actual_owner = _identifier(payload.get("id"))
            if owner is not None and actual_owner != owner:
                raise RawError("source-identity-mismatch")
            filename_ids = _filename(path)
            if filename_ids and filename_ids[0] != actual_owner:
                raise RawError("filename-owner-mismatch")
            actual_rollout = filename_ids[1] if filename_ids else actual_owner
            if rollout_id is not None and actual_rollout != rollout_id:
                raise RawError("rollout-identity-mismatch")
            if cutoff and payload.get("history_mode") != "paginated":
                raise RawError("inherited-source-not-paginated")
            if not compressed and cutoff and (len(data) != cutoff["end_byte_offset"] or not data.endswith(b"\n")):
                raise RawError("inheritance-byte-boundary-invalid")
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < physical_size:
                raise RawError("source-changed-during-read")
            changed_during = self._stat_identity(after) != self._stat_identity(before)
            if changed_during and after.st_size == before.st_size:
                raise RawError("source-changed-during-read")
            # 首次读取或并发写入后重读校验所捕获前缀，防止发布拼接自不同版本的快照。
            if not compressed and (not unchanged or changed_during) and self._hash_prefix(descriptor, len(data)) != digest.hexdigest():
                raise RawError("source-changed-during-read")
            self._watcher.watch(str(path), descriptor)
            part = {"path": path, "header": header, "payload": payload, "data": data,
                    "threadId": actual_owner, "rolloutId": actual_rollout, "first": first,
                    "headerBytes": len(first), "headerHash": cached["headerHash"] if prefix_verified else _digest(first),
                    "stat": before, "compressed": compressed, "physicalBytes": physical_size,
                    "dataHash": digest.hexdigest(), "hasher": digest, "prefixVerified": prefix_verified,
                    "cacheInvalidated": bool(cached and not unchanged and
                        (not same_file or physical_size < cached["physicalBytes"] or
                         (compressed and self._stat_identity(before) != self._stat_identity(cached["stat"])) or
                         (not compressed and not prefix_verified and data[:len(cached["data"])] != cached["data"][:len(data)]))),
                    "parsed": parsed, "cacheKey": key}
            self._cache_load(key, part)
            return part
        finally:
            os.close(descriptor)

    def _parse(self, part, cutoff):
        """按完整行保存检查点；半行和不完整 UTF-8 留待追加后重读，不提前送入 JSON。"""
        data = part["data"]
        parsed = part.get("parsed")
        if parsed is None:
            parsed = {"rows": [], "issues": [], "completeThrough": 0, "physicalOrdinal": 0,
                      "previousOrdinal": -1, "invalidCutoff": False, "readBytes": 0, "limited": False}
        elif parsed["readBytes"] == len(data):
            return parsed
        # 只复制状态与列表外壳；旧记录保持不可变，既有快照不能看到尚未完成的追加。
        parsed = {**parsed, "rows": list(parsed["rows"]), "issues": list(parsed["issues"])}
        def issue(code):
            if code not in parsed["issues"]:
                parsed["issues"].append(code)
        mode = part["payload"].get("history_mode", "legacy")
        known_mode = mode in ("legacy", "paginated")
        if not known_mode:
            issue("unsupported-history-mode")
        offset = parsed["completeThrough"]
        end = data.rfind(b"\n") + 1
        if parsed["invalidCutoff"]:
            return parsed
        while offset < end:
            if parsed["physicalOrdinal"] >= self.max_physical_lines:
                issue("line-count-limit")
                parsed["limited"] = True
                break
            stop = data.find(b"\n", offset) + 1
            raw = data[offset:stop]
            start, offset = offset, stop
            physical_ordinal = parsed["physicalOrdinal"]
            parsed["physicalOrdinal"] += 1
            parsed["completeThrough"] = offset
            self.metrics["parsedLines"] += 1
            self.metrics["parsedBytes"] += len(raw)
            if len(raw) > self.max_line_bytes:
                issue("line-size-limit")
                continue
            record = _json(raw)
            if record is None:
                issue("malformed-record")
                continue
            ordinal = record.get("ordinal") if mode == "paginated" else physical_ordinal
            if not _integer(ordinal) or ordinal <= parsed["previousOrdinal"]:
                issue("ordinal-missing-or-regressed")
                continue
            parsed["previousOrdinal"] = ordinal
            if cutoff and ordinal >= cutoff["end_ordinal_exclusive"]:
                # 继承边界必须同时满足字节和 ordinal；任一越界都不能投影这段继承记录。
                issue("inheritance-ordinal-boundary-invalid")
                parsed["rows"] = []
                parsed["invalidCutoff"] = True
                break
            if not known_mode or record.get("type") not in _KNOWN:
                continue
            if len(parsed["rows"]) >= self.max_records:
                issue("record-count-limit")
                continue
            parsed["rows"].append({"record": record, "raw": raw, "ordinal": ordinal, "start": start, "end": offset})
        parsed["issues"] = list(dict.fromkeys(parsed["issues"]))
        parsed["readBytes"] = len(data)
        part["parsed"] = parsed
        # _load 已把同一个 part 放入限量缓存，这里同步保存下一次增量解析检查点。
        return parsed

    @staticmethod
    def _base(payload: dict) -> dict | None:
        """只接受受支持分页格式中的显式继承位置，不从 fork 信息猜读取范围。"""
        value = payload.get("history_base")
        if value is None:
            return None
        if payload.get("history_mode") != "paginated" or not isinstance(value, dict):
            raise RawError("unsupported-history-base")
        try:
            identifier = _identifier(value.get("thread_id"))
        except RawError:
            raise RawError("unsupported-history-base") from None
        if not _integer(value.get("end_ordinal_exclusive"), minimum=1) or not _integer(value.get("end_byte_offset"), minimum=1):
            raise RawError("unsupported-history-base")
        return {"thread_id": identifier, "end_ordinal_exclusive": value["end_ordinal_exclusive"],
                "end_byte_offset": value["end_byte_offset"]}

    def read(self, thread_id: str, metadata: dict[str, Any]) -> RawSnapshot:
        """串行采集一个任务及其可达继承范围，元数据身份必须与请求一致。"""
        identifier = _identifier(thread_id)
        if not isinstance(metadata, dict) or metadata.get("id", identifier) != identifier:
            raise RawError("thread-identity-mismatch")
        with self._lock:
            return self._read(identifier, metadata)

    def _read(self, identifier: str, metadata: dict[str, Any]) -> RawSnapshot:
        """先验证从当前片段回溯的继承链，再按祖先到自身的逻辑顺序发布记录。"""
        result = RawSnapshot()
        loaded: list[tuple[dict, dict | None]] = []
        remaining = self.max_bytes
        seen: set[str] = set()
        cutoff = None
        owner = identifier
        rollout_id = None
        path = None
        while True:
            try:
                if len(loaded) >= self.max_segments:
                    raise RawError("lineage-segment-limit")
                if remaining <= 0:
                    raise RawError("source-byte-limit")
                if rollout_id is not None and rollout_id in seen:
                    raise RawError("inheritance-cycle")
                if path is None:
                    path = self._path(identifier, metadata) if cutoff is None else self._find(rollout_id=rollout_id)
                part = self._load(path, owner, rollout_id, cutoff, remaining)
                if part["rolloutId"] in seen:
                    raise RawError("inheritance-cycle")
                seen.add(part["rolloutId"])
                if cutoff:
                    header_ordinal = part["header"].get("ordinal")
                    if not _integer(header_ordinal) or header_ordinal >= cutoff["end_ordinal_exclusive"]:
                        raise RawError("inheritance-ordinal-boundary-invalid")
                loaded.append((part, cutoff))
                remaining -= len(part["data"])
                try:
                    base = self._base(part["payload"])
                except RawError as error:
                    part["issues"] = [str(error)]
                    break
                if base is None:
                    break
                cutoff = base
                rollout_id = base["thread_id"]
                owner = None
                path = None
            except (OSError, ValueError, RuntimeError) as error:
                code = str(error) if isinstance(error, RawError) else "source-unreadable"
                result.issues.append(code)
                missing_id = rollout_id or identifier
                source_id = self._source_id(owner or missing_id, missing_id) + "-unresolved"
                inherited = {"start": 0, "endExclusive": cutoff["end_ordinal_exclusive"]} if cutoff else None
                state = "missing" if code == "source-missing" else "unsupported" if "unsupported" in code else "unreadable"
                result.sources.append(self._coverage(source_id, owner or missing_id, state=state, reason=code, inherited=inherited))
                break
        # 先收集并冻结每段明确边界，再倒序发布祖先范围，避免投影期间继承链漂移。
        signatures = []
        for part, inherited_cutoff in reversed(loaded):
            source_id = self._source_id(part["threadId"], part["rolloutId"])
            data = part["data"]
            info = part["stat"]
            previous = self._versions.get(source_id)
            generation = previous["generation"] if previous else 0
            # generation 表示物理重写/替换而非追加次数；旧字节位置不能跨代继续使用。
            if previous and (part["cacheInvalidated"] or (info.st_dev, info.st_ino) != previous["identity"] or
                             info.st_size < previous["physicalSize"] or
                             (info.st_mtime_ns != previous["mtimeNs"] and info.st_size <= previous["physicalSize"] and len(data) < previous["length"]) or
                             (not part["prefixVerified"] and len(data) >= previous["length"] and _digest(data[:previous["length"]]) != previous["hash"])):
                generation += 1
            self._versions[source_id] = {"generation": generation, "identity": (info.st_dev, info.st_ino),
                                         "physicalSize": info.st_size, "mtimeNs": info.st_mtime_ns,
                                         "length": len(data), "hash": part["dataHash"]}
            inherited = {"start": part["header"].get("ordinal", 0), "endExclusive": inherited_cutoff["end_ordinal_exclusive"]} if inherited_cutoff else None
            if inherited is not None and not _integer(inherited["start"]):
                inherited["start"] = 0
            format = "compressed" if part["compressed"] else "jsonl"
            end = inherited_cutoff["end_byte_offset"] if inherited_cutoff else part["physicalBytes"]
            segment = {"path": str(part["path"]), "threadId": part["threadId"], "rolloutId": part["rolloutId"],
                       "segmentId": source_id, "generation": generation, "byteStart": 0, "byteEnd": end,
                       "inheritedRange": inherited, "physicalBytes": part["physicalBytes"], "format": format,
                       "device": info.st_dev, "inode": info.st_ino, "mtimeNs": info.st_mtime_ns,
                       "headerSha256": part["headerHash"], "readSha256": part["dataHash"], "ctimeNs": info.st_ctime_ns,
                       "readBytes": len(data), "completeThrough": 0, "partialTailBytes": 0,
                       "exportable": not (part["compressed"] and inherited_cutoff is not None)}
            result.segments.append(segment)
            reasons: list[str] = list(part.get("issues", []))
            if part["compressed"]:
                reasons.append("compressed-projection-unsupported")
                if inherited_cutoff:
                    reasons.append("compressed-inherited-range-unavailable")
                consumed = None
            else:
                if len(data) < end:
                    reasons.append("source-byte-limit")
                parsed = self._parse(part, inherited_cutoff)
                consumed = parsed["completeThrough"]
                segment["completeThrough"] = consumed
                reasons.extend(parsed["issues"])
                tail = len(data) - consumed
                if tail and not parsed["invalidCutoff"] and not parsed["limited"]:
                    segment["partialTailBytes"] = tail if len(data) == end else 0
                    reasons.append("incomplete-tail" if len(data) == end else "incomplete-index-line")
                if parsed["invalidCutoff"] or (parsed["limited"] and inherited_cutoff):
                    segment["exportable"] = False
                version = part["payload"].get("cli_version")
                version = version if isinstance(version, str) and len(version) <= 128 else None
                excluded_start = loaded[0][0]["payload"].get("subagent_history_start_ordinal") if loaded else None
                # 继承与子代理上下文边界供领域层分别使用：可保留原始证据，但不能认作自身行为。
                for row in parsed["rows"]:
                    if len(result.records) >= self.max_records:
                        reasons.append("record-count-limit")
                        break
                    source = {"kind": "raw", "threadId": part["threadId"], "segmentId": source_id,
                              "generation": generation, "byteStart": row["start"], "byteEnd": row["end"],
                              "rawType": row["record"]["type"], "producerVersion": version, "association": "physicalRecord"}
                    result.records.append({"record": row["record"], "raw": row["raw"], "source": source, "ordinal": row["ordinal"],
                                           "inherited": inherited_cutoff is not None,
                                           "projectionExcluded": _integer(excluded_start) and row["ordinal"] < excluded_start})
            reasons = list(dict.fromkeys(reasons))
            result.issues.extend(reasons)
            state = "unsupported" if part["compressed"] else "partial" if reasons else "readable"
            result.sources.append(self._coverage(source_id, part["threadId"], state=state, reason=",".join(reasons) or None,
                                                 generation=generation, format=format, end=end, consumed=consumed, inherited=inherited))
            signatures.append((source_id, generation))
        result.issues = list(dict.fromkeys(result.issues))
        # 片段拓扑/版本决定任务代次；正文追加只改变 fingerprint，不使所有游标失效。
        topology = _digest(json.dumps(signatures, separators=(",", ":")).encode())
        previous_thread = self._thread_versions.get(identifier)
        result.generation = previous_thread[1] + (previous_thread[0] != topology) if previous_thread else 0
        self._thread_versions[identifier] = (topology, result.generation)
        result.fingerprint = _digest(json.dumps({"sources": result.sources,
                                                "hashes": [part[0]["headerHash"] + part[0]["dataHash"] for part in loaded]},
                                               sort_keys=True, separators=(",", ":")).encode())
        return result
