"""Publish frozen ZIP files to the fixed local export directory.

The UI supplies no path. A private temporary file is verified before an atomic,
no-overwrite hard-link publication in the same directory. No Codex data is read.
"""
from __future__ import annotations

import hashlib
import os
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
    """Walk existing directories without following any symlink component."""
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
        # The winning publisher removes its temporary hard link after final
        # readback. A concurrent verifier can see nlink 2 -> 1 and a new ctime
        # without any byte change. Retry the entire hash once; never ignore
        # ctime or accept the first pass as stable. Size/mtime, permissions,
        # ownership and file identity must otherwise be unchanged.
        cleanup_only = ((before.st_nlink, after.st_nlink) == (2, 1)
                        and _version(before)[:-1] == _version(after)[:-1]
                        and (before.st_mode, before.st_uid, before.st_gid)
                        == (after.st_mode, after.st_uid, after.st_gid))
        if attempt or not cleanup_only:
            raise ValueError("export-changed-during-verification")


def _cleanup_owned_temp(directory: int, name: str, owned: tuple[int, int] | None) -> None:
    """Never clean the final name or a temp name whose identity has changed."""
    if owned is None:
        return
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISREG(current.st_mode) and _identity(current) == owned:
            os.unlink(name, dir_fd=directory)
    except OSError:
        # Cleanup must not mask the original failure or remove any other entry.
        pass


class ExportUnconfirmedError(RuntimeError):
    """Publication succeeded, but the caller must verify this same candidate."""

    def __init__(self, candidate_result: dict):
        super().__init__("export-unconfirmed")
        self.candidate_result = {**candidate_result, "verified": False}


class ExportStore:
    def __init__(self, root: Path | None = None):
        # root is constructor-only injection for local tests/configuration. It is
        # never accepted from a tool request or from the UI.
        self.root = Path(root) if root is not None else Path.home() / "Downloads" / "Codex Display"
        if not self.root.is_absolute() or ".." in self.root.parts or self.root == Path(self.root.anchor):
            raise ValueError("invalid-export-directory")

    def _open_directory(self) -> int:
        # Existing ancestors, including Downloads, must already be directories.
        # Only the plugin's own final directory is created or chmod'ed.
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

    def reserve(self, size: int, sha256: str, prefix: str = "codex-display") -> dict:
        """Choose one final name before the caller durably records its receipt.

        This does not create a directory, temporary file or final file. The
        caller must persist this exact candidate before calling publish_file;
        retrying publication must never reserve another candidate.
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
        """Re-read a server-cached result; never recreate or repair an export.

        A result must name a generated basename directly inside this store's
        fixed root. It may be an unconfirmed candidate from a publication error;
        neither cached state is trusted until readback passes. No path supplied
        by a UI/tool request is accepted here.
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
        """Copy a caller-owned frozen ZIP FD without changing its offset.

        The only path accepted is the already reserved server-side candidate.
        Existing candidates are verified without reading source_fd, writing or
        changing their names; even a missing source can therefore be recovered.
        """
        self._validate_result(candidate)
        candidate = dict(candidate)
        if candidate["verified"]:
            # A previous verified receipt never authorizes recreating a file
            # that the user subsequently moved or removed.
            return self.verify_saved(candidate)
        try:
            return self.verify_saved(candidate)
        except FileNotFoundError:
            pass
        # All other failures (collision, symlink, replaced parent, corruption)
        # leave the candidate untouched; callers must not silently choose anew.
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

    def save_bytes(self, payload: bytes, prefix: str = "codex-display") -> dict:
        """Compatibility path for small M0 probes; production uses file FDs."""
        if not isinstance(payload, bytes):
            raise TypeError("export-payload-must-be-bytes")
        candidate = self.reserve(len(payload), hashlib.sha256(payload).hexdigest(), prefix)
        def copy_bytes(destination: int) -> None:
            view = memoryview(payload)
            for offset in range(0, len(view), _CHUNK_BYTES):
                _write_all(destination, view[offset:offset + _CHUNK_BYTES])
        return self._publish(candidate, copy_bytes, existing_ok=False)

    def _publish(self, candidate_result: dict, copy, *, existing_ok: bool) -> dict:
        filename, size, checksum = candidate_result["filename"], candidate_result["bytes"], candidate_result["sha256"]
        temporary_name = f".codex-display-tmp-{uuid4().hex}"
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
                # link(2) atomically creates the destination only if it does not
                # already exist. There is no exists-check followed by rename/replace.
                try:
                    os.link(temporary_name, filename, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    if existing_ok:
                        # A concurrent retry may have published this exact
                        # candidate while we copied. It must pass readback.
                        return self.verify_saved(candidate_result)
                    raise
                except Exception:
                    # A failure injected immediately after a successful link
                    # may arrive before the assignment below. Check through
                    # the still-open directory FD, even if its pathname moved.
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
                # Returning a real path also requires the configured directory name
                # to still refer to the directory through which this save occurred.
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
            # The published name belongs to this request even when fsync or
            # readback fails. Preserve it so a retry verifies instead of saving
            # another copy; never expose the original exception text.
            if publication_complete:
                raise ExportUnconfirmedError(candidate_result) from None
            raise
