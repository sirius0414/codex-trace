"""把已冻结 ZIP 发布到固定本地目录，不接受 UI 指定路径。

先校验同目录的私有临时文件，再由平台层用硬链接原子创建最终名称，已有文件绝不覆盖。
该层只处理冻结文件；不读取 Codex 来源，也不负责持久化幂等回执。
"""
from __future__ import annotations

import hashlib
from .platform import filesystem as os
from pathlib import Path
import re
import stat
from uuid import uuid4

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}-[0-9a-f]{32}\.zip")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK_BYTES = 64 * 1024
_MAX_BYTES = 2**53 - 1


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _write_all(fd: int, data: bytes | memoryview) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("export-short-write")
        remaining = remaining[written:]


def _open_chain(path: Path) -> int:
    """逐级打开已存在目录，任一层符号链接都不能绕过固定目录边界。"""
    current = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _verify_bytes(fd: int, expected_size: int, expected_sha256: str) -> None:
    """读回尺寸和哈希，并要求读取期间文件版本稳定。"""
    for attempt in range(2):
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ValueError("export-size-mismatch")
        digest = hashlib.sha256()
        total = 0
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, _CHUNK_BYTES):
            total += len(chunk)
            if total > expected_size:
                raise ValueError("export-size-mismatch")
            digest.update(chunk)
        after = os.fstat(fd)
        if total != expected_size or digest.hexdigest() != expected_sha256:
            raise ValueError("export-checksum-mismatch")
        if _version(before) == _version(after):
            return
        # 发布进程在读回后移除临时硬链接，其他核验者可能看到 nlink 2→1 与 ctime
        # 改变。仅此情形允许重算一次完整哈希；仍不能忽略 ctime 或接受首次不稳定读。
        # 尺寸、mtime、权限、属主和文件身份必须保持不变。
        cleanup_only = ((before.st_nlink, after.st_nlink) == (2, 1)
                        and _version(before)[:-1] == _version(after)[:-1]
                        and (before.st_mode, before.st_uid, before.st_gid)
                        == (after.st_mode, after.st_uid, after.st_gid))
        if attempt or not cleanup_only:
            raise ValueError("export-changed-during-verification")


def _cleanup_owned_temp(directory: int, name: str, owned: tuple[int, int] | None) -> None:
    """只清理身份未变的自有临时名称；绝不删除最终文件。"""
    if owned is None:
        return
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and _identity(current) == owned:
            os.unlink(name, dir_fd=directory)
    except OSError:
        # 清理失败不能掩盖原始异常，也不能改删其他目录项。
        pass


class ExportUnconfirmedError(RuntimeError):
    """最终名称已发布但结果未确认；调用方只能继续核验同一候选。"""

    def __init__(self, candidate_result: dict):
        super().__init__("export-unconfirmed")
        self.candidate_result = {**candidate_result, "verified": False}


class ExportStore:
    """在固定目录中无覆盖发布；恢复时以原候选路径和冻结哈希核验现有文件。"""

    def __init__(self, root: Path | None = None):
        # root 只允许在构造时由本地配置或测试注入，不能来自工具请求或前端。
        self.root = Path(root) if root is not None else Path.home() / "Downloads" / "Codex Trace"
        if not self.root.is_absolute() or ".." in self.root.parts or self.root == Path(self.root.anchor):
            raise ValueError("invalid-export-directory")

    def _open_directory(self) -> int:
        # Downloads 等祖先目录必须已存在；只创建或修改本插件最终目录的权限。
        parent = _open_chain(self.root.parent)
        try:
            try:
                os.mkdir(self.root.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            directory = os.open(self.root.name, _DIRECTORY_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid():
                raise PermissionError("export-directory-not-owned")
            os.fchmod(directory, 0o700)
            return directory
        except BaseException:
            os.close(directory)
            raise

    def _validate_result(self, result: dict) -> None:
        if not isinstance(result, dict):
            raise ValueError("invalid-export-result")
        filename, size, checksum = result.get("filename"), result.get("bytes"), result.get("sha256")
        if (not isinstance(filename, str) or _FILENAME.fullmatch(filename) is None
                or result.get("path") != str(self.root / filename)
                or type(size) is not int or not 0 <= size <= _MAX_BYTES
                or not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None
                or type(result.get("verified")) is not bool):
            raise ValueError("invalid-export-result")

    def reserve(self, size: int, sha256: str, prefix: str = "codex-trace") -> dict:
        """选择唯一候选文件名，由调用方先持久化，再调用 publish_file。

        此处不创建目录或文件；同一保存请求重试时必须复用原候选，不能再次分配。
        """
        if type(size) is not int or not 0 <= size <= _MAX_BYTES:
            raise ValueError("invalid-export-size")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("invalid-export-sha256")
        if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
            raise ValueError("invalid-export-prefix")
        filename = f"{prefix}-{uuid4().hex}.zip"
        return {"path": str(self.root / filename), "filename": filename,
                "bytes": size, "sha256": sha256, "verified": False}

    def verify_saved(self, result: dict) -> dict:
        """读回服务端持久化的候选；不重建、不修复用户移动或删除的文件。

        候选只能是固定目录下的服务端文件名，不能来自 UI 路径。即便历史回执
        已 verified，本次核验也必须检查实际字节与目录身份。
        """
        self._validate_result(result)
        filename, size, checksum = result["filename"], result["bytes"], result["sha256"]
        directory = _open_chain(self.root)
        try:
            directory_identity = _identity(os.fstat(directory))
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                original = os.fstat(fd)
                if not stat.S_ISREG(original.st_mode):
                    raise ValueError("export-not-regular-file")
                _verify_bytes(fd, size, checksum)
                current = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(original):
                    raise ValueError("export-published-file-replaced")
            finally:
                os.close(fd)
            current_directory = _open_chain(self.root)
            try:
                if _identity(os.fstat(current_directory)) != directory_identity:
                    raise ValueError("export-directory-replaced")
            finally:
                os.close(current_directory)
            return {**result, "verified": True}
        finally:
            os.close(directory)

    def publish_file(self, source_fd: int, candidate: dict) -> dict:
        """复制调用方持有的冻结 ZIP 描述符，不改变其读取偏移。

        目标只能是已预留并持久化的候选。候选已存在时只读回核验，因此临时
        来源已不存在也能恢复保存结果；核验失败不能换名或覆盖。
        """
        self._validate_result(candidate)
        candidate = dict(candidate)
        if candidate["verified"]:
            # 历史 verified 回执不能授权重建用户之后移动或删除的文件。
            return self.verify_saved(candidate)
        try:
            return self.verify_saved(candidate)
        except FileNotFoundError:
            pass
        # 冲突、符号链接、父目录变化或损坏均保留原候选，调用方不能静默另选目标。
        if type(source_fd) is not int or source_fd < 0:
            raise ValueError("invalid-export-source-descriptor")
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("export-source-not-regular-file")
        if before.st_size != candidate["bytes"]:
            raise ValueError("export-source-size-mismatch")

        def copy_source(destination: int) -> None:
            digest = hashlib.sha256()
            offset = 0
            while offset < candidate["bytes"]:
                chunk = os.pread(source_fd, min(_CHUNK_BYTES, candidate["bytes"] - offset), offset)
                if not chunk:
                    raise ValueError("export-source-size-mismatch")
                digest.update(chunk)
                _write_all(destination, chunk)
                offset += len(chunk)
            if _version(before) != _version(os.fstat(source_fd)):
                raise ValueError("export-source-changed-during-copy")
            if digest.hexdigest() != candidate["sha256"]:
                raise ValueError("export-source-checksum-mismatch")

        return self._publish(candidate, copy_source, existing_ok=True)

    def save_bytes(self, payload: bytes, prefix: str = "codex-trace") -> dict:
        """兼容小型 M0 合成探针；正式导出使用文件描述符流式复制。"""
        if not isinstance(payload, bytes):
            raise TypeError("export-payload-must-be-bytes")
        candidate = self.reserve(len(payload), hashlib.sha256(payload).hexdigest(), prefix)
        def copy_bytes(destination: int) -> None:
            view = memoryview(payload)
            for offset in range(0, len(view), _CHUNK_BYTES):
                _write_all(destination, view[offset:offset + _CHUNK_BYTES])
        return self._publish(candidate, copy_bytes, existing_ok=False)

    def _publish(self, candidate_result: dict, copy, *, existing_ok: bool) -> dict:
        """临时写入、fsync、核验、无覆盖发布、最终读回；发布后异常必须保留目标。"""
        filename, size, checksum = candidate_result["filename"], candidate_result["bytes"], candidate_result["sha256"]
        temporary_name = f".codex-trace-tmp-{uuid4().hex}"
        directory = self._open_directory()
        directory_identity = _identity(os.fstat(directory))
        owned = None
        temporary = None
        publication_complete = False
        try:
            try:
                temporary = os.open(temporary_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=directory)
                owned = _identity(os.fstat(temporary))
                os.fchmod(temporary, 0o600)
                copy(temporary)
                os.fsync(temporary)
                _verify_bytes(temporary, size, checksum)
                current = os.stat(temporary_name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or _identity(current) != owned:
                    raise ValueError("export-temporary-replaced")
                # 关闭写句柄让 Windows 最后写入时间稳定，避免并发核验者将本进程
                # 迟到的时间更新误认成外部改写；发布仍需核对最终文件身份和完整哈希。
                os.close(temporary)
                temporary = None
                # link(2) 仅在目标不存在时原子创建名称，避免先检查再 rename 的覆盖竞态。
                try:
                    os.link(temporary_name, filename, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    if existing_ok:
                        # 复制期间另一重试可能已发布同一候选，仍须通过实际读回核验。
                        return self.verify_saved(candidate_result)
                    raise
                except Exception:
                    # link 成功后、标志赋值前也可能抛异常；通过仍持有的目录描述符
                    # 确认目标归属，即使目录路径已经变化也不误判为未发布。
                    try:
                        entry = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                        publication_complete = stat.S_ISREG(entry.st_mode) and _identity(entry) == owned
                    except OSError:
                        pass
                    raise
                publication_complete = True
                os.fsync(directory)
                published = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    if _identity(os.fstat(published)) != owned:
                        raise ValueError("export-published-file-replaced")
                    _verify_bytes(published, size, checksum)
                finally:
                    os.close(published)
                final = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(final.st_mode) or _identity(final) != owned:
                    raise ValueError("export-published-file-replaced")
                # 返回绝对路径前，还需证明该目录名称仍指向本次写入使用的目录。
                current_directory = _open_chain(self.root)
                try:
                    if _identity(os.fstat(current_directory)) != directory_identity:
                        raise ValueError("export-directory-replaced")
                finally:
                    os.close(current_directory)
                return {**candidate_result, "verified": True}
            finally:
                _cleanup_owned_temp(directory, temporary_name, owned)
                if temporary is not None:
                    os.close(temporary)
                os.close(directory)
        except Exception:
            # fsync 或读回失败时，已发布名称仍属于原请求；保留供同键核验，避免
            # 重试另存一份。向上层只报告未确认状态，不泄露底层异常正文。
            if publication_complete:
                raise ExportUnconfirmedError(candidate_result) from None
            raise
