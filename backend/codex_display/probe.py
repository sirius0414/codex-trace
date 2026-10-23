"""M0 宿主能力诊断：只验证候选身份、合成下载与字段名，不推定真实任务绑定。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import UUID, uuid4
import zipfile

from . import __version__
from .export_store import ExportUnconfirmedError

URI = "ui://codex-display/trajectory-v1.html"
PANEL_URI = "ui://codex-display/trajectory-panel-v1.html"
DIAGNOSTIC_PHASES = (
    "download-start", "chunk-received", "chunk-verified", "blob-requested",
    "download-error", "context-change", "visibility-change", "page-ready",
    "export-start", "export-verified", "export-unconfirmed",
)
DIAGNOSTIC_CODES = (
    "timeout", "rpc-error", "isError", "invalid-envelope", "invalid-chunk",
    "digest-unavailable", "digest-failed", "invalid-manifest", "checksum-mismatch", "unsupported", "save-requested", "unknown",
)
MAX_DIAGNOSTIC_ELAPSED_MS = 3_600_000
FIELD_NAME_PATTERN = r"^[A-Za-z_$][A-Za-z0-9_$./:-]{0,199}$"


def field_names(value: list[str] | None) -> list[str]:
    """仅接受限量字段名，不接受字段值、对象或自由文本，避免诊断带入任务正文。"""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("invalid-field-names")
    if any(not isinstance(item, str) or not re.fullmatch(FIELD_NAME_PATTERN, item) for item in value):
        raise ValueError("invalid-field-names")
    return list(dict.fromkeys(value))


def canonical_uuid(value: str) -> str:
    """只接收标准 UUID 形式并统一大小写，避免同一实例出现多个作用域键。"""
    if not isinstance(value, str):
        raise ValueError("invalid-instance-id")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid-instance-id") from None
    if canonical != value.lower():
        raise ValueError("invalid-instance-id")
    return canonical


def candidate_identity(metadata: dict, *, entrypoint: str = "global") -> dict:
    """识别元数据中的候选 thread ID；格式正确仍不代表入口语义已验证。"""
    if entrypoint not in ("global", "thread"):
        raise ValueError("invalid-entrypoint")
    keys = [key for key in ("thread_id", "threadId") if key in metadata]
    values = []
    for key in keys:
        try:
            value = metadata[key]
            if not isinstance(value, str) or str(UUID(value)) != value.lower():
                raise ValueError()
            values.append(value.lower())
        except (ValueError, AttributeError, TypeError):
            return {"candidateThreadId": None, "identityReason": "invalid-identity"}
    # 两种字段拼写同时出现且冲突时拒绝挑选一个，防止错误绑定当前任务。
    if len(set(values)) > 1:
        return {"candidateThreadId": None, "identityReason": "conflicting-identities"}
    return {
        "candidateThreadId": values[0] if values else None,
        "identityReason": f"unverified-{entrypoint}-semantics" if values else "missing-identity",
    }


def probe_zip() -> bytes:
    """生成含非法字节、未知类型和半行的合成 ZIP，只测试传输保真，不含真实任务。"""
    raw = b'{"type":"m0_fixture","text":"local only"}\r\n\xffbad\n{"partial":'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in {
            "manifest.json": json.dumps({"purpose": "M0 synthetic local-download probe", "realTaskRecords": False,
                                          "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}).encode(),
            "records/synthetic.jsonl": raw,
        }.items():
            item = zipfile.ZipInfo(name, date_time=(2026, 10, 22, 0, 0, 0))
            item.external_attr = 0o600 << 16
            archive.writestr(item, data)
    return stream.getvalue()


class ProbeService:
    """拥有本进程激活记录与合成导出幂等键；服务重启后旧激活不可继续使用。"""
    def __init__(self, audit_path: Path | None = None):
        self.server_instance = str(uuid4())
        self.audit_path = audit_path
        self.activations: dict[str, dict] = {}
        self.download = probe_zip()
        self.export_lock = threading.Lock()
        self.exports: dict[str, tuple[tuple[str, str], dict]] = {}

    def export_records(self, activation_id: str, ui_instance_id: str, request_id: str, store) -> dict:
        """同一服务实例中，每个请求只保存一次合成 ZIP，并绑定激活/UI 身份。

        锁覆盖超时后仍在保存的 RPC worker，避免重试产生第二份文件。旧激活在服务重启后
        直接失效；未确认的候选结果先保留，重试时重新校验，而不是再次创建文件。
        """
        activation_id = canonical_uuid(activation_id)
        ui_instance_id = canonical_uuid(ui_instance_id)
        request_id = canonical_uuid(request_id)
        binding = (activation_id, ui_instance_id)
        with self.export_lock:
            if activation_id not in self.activations:
                raise ValueError("unknown-activation")
            previous = self.exports.get(request_id)
            if previous:
                if previous[0] != binding:
                    raise ValueError("export-request-scope-mismatch")
                verified = store.verify_saved(previous[1])
                self.exports[request_id] = (binding, verified)
                if not previous[1]["verified"]:
                    self.audit("export-recovered", {"activationId": activation_id, "uiInstanceId": ui_instance_id,
                                                    "requestId": request_id, "filename": verified["filename"],
                                                    "bytes": verified["bytes"], "sha256": verified["sha256"]})
                return dict(verified)
            if len(self.exports) >= 256:
                raise ValueError("export-request-limit")
            try:
                saved = store.save_bytes(self.download, prefix="codex-display-m0")
            except ExportUnconfirmedError as error:
                candidate = {"kind": "local-export", "requestId": request_id,
                             **error.candidate_result, "synthetic": True}
                self.exports[request_id] = (binding, candidate)
                raise
            result = {"kind": "local-export", "requestId": request_id, **saved, "synthetic": True}
            self.exports[request_id] = (binding, result)
            self.audit("export-saved", {"activationId": activation_id, "uiInstanceId": ui_instance_id,
                                       "requestId": request_id, "filename": result["filename"],
                                       "bytes": result["bytes"], "sha256": result["sha256"],
                                       "verified": result["verified"], "synthetic": True})
            return dict(result)

    def audit(self, kind: str, fields: dict):
        """追加调用方已筛选的诊断元数据；落盘文件拒绝符号链接并限制为当前用户可读写。"""
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as output:
            if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                raise ValueError("invalid-audit-file")
            os.fchmod(output.fileno(), 0o600)
            output.write(json.dumps({"atMs": int(time.time() * 1000), "kind": kind, **fields}) + "\n")

    def activate(self, metadata: dict, *, entrypoint: str = "global") -> dict:
        """签发仅在本进程有效的激活身份，只记录字段名和候选 ID，不声称自动绑定成功。"""
        result = {"kind": "activation", "activationId": str(uuid4()), "serverInstanceId": self.server_instance,
                  "entrypoint": entrypoint, "serverVersion": __version__,
                  "metadataKeys": sorted(metadata), **candidate_identity(metadata, entrypoint=entrypoint),
                  "automaticIdentityVerified": False, "readOnlyLiveEvents": False,
                  "downloadExpectedSha256": hashlib.sha256(self.download).hexdigest(),
                  "downloadExpectedBytes": len(self.download)}
        self.activations[result["activationId"]] = result
        if len(self.activations) > 256:
            self.activations.pop(next(iter(self.activations)))
        self.audit("activation", result)
        return result

    def observe(self, activation_id: str, ui_instance_id: str, context_keys: list[str], display_mode: str | None,
                host_capability_keys: list[str] | None = None) -> dict:
        """记录指定激活/UI 的能力字段名，用于诊断宿主可见信息的形状。"""
        if not isinstance(activation_id, str) or activation_id not in self.activations:
            raise ValueError("unknown-activation")
        result = {"activationId": activation_id, "uiInstanceId": canonical_uuid(ui_instance_id),
                  "contextKeys": field_names(context_keys), "displayMode": display_mode,
                  "hostCapabilityKeys": field_names(host_capability_keys)}
        self.audit("ui-observation", result)
        return {"recorded": True, **result}

    def diagnostic_event(self, activation_id: str, ui_instance_id: str, phase: str, *,
                         code: str | None = None, elapsed_ms: int | None = None,
                         visibility: str | None = None, context_keys: list[str] | None = None,
                         host_capability_keys: list[str] | None = None) -> dict:
        """通过阶段/代码白名单记录下载与 UI 诊断，禁止传入任意错误消息正文。"""
        if not isinstance(activation_id, str) or activation_id not in self.activations:
            raise ValueError("unknown-activation")
        if phase not in DIAGNOSTIC_PHASES:
            raise ValueError("invalid-diagnostic-phase")
        if code is not None and code not in DIAGNOSTIC_CODES:
            raise ValueError("invalid-diagnostic-code")
        if elapsed_ms is not None and (type(elapsed_ms) is not int or not 0 <= elapsed_ms <= MAX_DIAGNOSTIC_ELAPSED_MS):
            raise ValueError("invalid-diagnostic-elapsed")
        if visibility is not None and visibility not in ("visible", "hidden"):
            raise ValueError("invalid-diagnostic-visibility")
        result = {"activationId": activation_id, "uiInstanceId": canonical_uuid(ui_instance_id), "phase": phase}
        if code is not None:
            result["code"] = code
        if elapsed_ms is not None:
            result["elapsedMs"] = elapsed_ms
        if visibility is not None:
            result["visibility"] = visibility
        if context_keys is not None:
            result["contextKeys"] = field_names(context_keys)
        if host_capability_keys is not None:
            result["hostCapabilityKeys"] = field_names(host_capability_keys)
        self.audit("diagnostic-event", result)
        return {"recorded": True, **result}

    def download_chunk(self, offset: int) -> dict:
        """按字节偏移返回固定合成包，整包/分块摘要供客户端验证完整性。"""
        if type(offset) is not int or offset < 0 or offset > len(self.download):
            raise ValueError("invalid-offset")
        chunk = self.download[offset:offset + 65536]
        self.audit("download-read", {"offset": offset, "bytes": len(chunk),
                                     "sha256": hashlib.sha256(self.download).hexdigest(),
                                     "chunkSha256": hashlib.sha256(chunk).hexdigest()})
        return {"filename": "codex-display-m0-local-download.zip", "offset": offset,
                "data": base64.b64encode(chunk).decode(), "nextOffset": offset + len(chunk),
                "totalBytes": len(self.download), "sha256": hashlib.sha256(self.download).hexdigest(),
                "chunkSha256": hashlib.sha256(chunk).hexdigest(), "synthetic": True,
                "done": offset + len(chunk) == len(self.download)}


def _open_source_without_symlinks(path: Path) -> int:
    """逐层使用目录描述符，防止父目录替换绕过 O_NOFOLLOW。

    非阻塞打开使 FIFO 等特殊文件可在读取前被拒绝，不改变普通文件读取行为。
    """
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def inspect_source(path_string: str | None, thread_id: str, codex_home: Path) -> dict:
    """只检查一个片段的头部身份与可读性；不回溯继承链，不声称历史覆盖完整。"""
    if not path_string:
        return {"readable": False, "reason": "source-not-recorded"}
    try:
        path = Path(path_string)
        roots = [codex_home / "sessions", codex_home / "archived_sessions"]
        if not path.is_absolute() or not any(path.is_relative_to(root) for root in roots):
            return {"readable": False, "reason": "outside-session-roots"}
        if path.resolve() != path:
            return {"readable": False, "reason": "symlink-or-noncanonical-source"}
        fd = _open_source_without_symlinks(path)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return {"readable": False, "reason": "not-regular"}
        with os.fdopen(fd, "rb") as source:
            first = source.readline(1024 * 1024 + 1)
            if len(first) > 1024 * 1024 or not first.endswith(b"\n"):
                return {"readable": False, "reason": "header-limit-or-incomplete"}
            record = json.loads(first)
            payload = record.get("payload", {})
            if record.get("type") != "session_meta" or payload.get("id") != thread_id:
                return {"readable": False, "reason": "source-identity-mismatch"}
            mode = payload.get("history_mode")
            return {"readable": True, "physicalBytes": info.st_size, "headerSha256": hashlib.sha256(first).hexdigest(),
                    "historyMode": mode if isinstance(mode, str) and len(mode) <= 64 else None,
                    "hasHistoryBase": bool(payload.get("history_base")),
                    "coverage": "selected physical segment header only; lineage not traversed",
                    "readOnlyLiveEvents": False}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return {"readable": False, "reason": "unreadable-or-unsupported-header"}
