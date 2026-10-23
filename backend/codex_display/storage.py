"""Private, rebuildable SQLite data owned only by Codex Display.

The database contains derived projections, view preferences and durable export
receipts. It never opens or writes a Codex session database. All mutations use
short SQLite transactions so multiple plugin processes can share this store.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_VIEW_BYTES = 128 * 1024
_MAX_INTEGER = 2**53 - 1
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "trajectory.schema.json"
if not _SCHEMA_PATH.is_file():
    _SCHEMA_PATH = Path(__file__).resolve().parents[2] / "trajectory.schema.json"


class StorageError(RuntimeError):
    """Public errors contain fixed codes, never persisted payloads or SQL."""


def _key(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or "\0" in value:
        raise StorageError("invalid-storage-key")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or not 0 <= value < _MAX_INTEGER:
        raise StorageError("invalid-storage-revision")
    return value


def _encode(value: Any, *, maximum: int = _MAX_JSON_BYTES) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise StorageError("invalid-storage-json") from None
    if len(encoded) > maximum:
        raise StorageError("storage-payload-too-large")
    return encoded


def _decode(value: str) -> Any:
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        raise StorageError("invalid-stored-json") from None


def _open_directory(path: Path) -> int:
    """Create missing owned directories without ever traversing symlinks."""
    current = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        info = os.fstat(current)
        if info.st_uid != os.getuid():
            raise StorageError("storage-directory-not-owned")
        os.fchmod(current, 0o700)
        return current
    except BaseException:
        os.close(current)
        raise


class DerivedStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else Path.home() / ".local" / "share" / "codex-display" / "index"
        if not self.root.is_absolute() or ".." in self.root.parts or self.root == Path(self.root.anchor):
            raise StorageError("invalid-storage-directory")
        self.path = self.root / "derived.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        self._directory = -1
        try:
            self._directory = _open_directory(self.root)
            self._directory_identity = self._identity(os.fstat(self._directory))
            descriptor = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=self._directory)
            try:
                info = os.fstat(descriptor)
                self._check_file(info)
                os.fchmod(descriptor, 0o600)
                self._file_identity = self._identity(info)
            finally:
                os.close(descriptor)
            self._verify_location()
            self._connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                              timeout=5, check_same_thread=False, isolation_level=None)
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._verify_location()
            self._initialize()
            schema = json.loads(_SCHEMA_PATH.read_text())
            self._view_validator = Draft202012Validator({**schema, "$ref": "#/$defs/ViewState"})
        except BaseException as error:
            self.close()
            if isinstance(error, (StorageError, KeyboardInterrupt, SystemExit)):
                raise
            raise StorageError("storage-open-failed") from None

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    @staticmethod
    def _check_file(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise StorageError("unsafe-storage-file")

    def _verify_location(self) -> None:
        if self._closed:
            raise StorageError("storage-closed")
        # Verify the entire path again without following any symlink. Existing
        # ancestors are never chmod'ed; only the plugin's own final directory is.
        current = os.open(self.root.anchor, _DIRECTORY_FLAGS)
        try:
            for component in self.root.parts[1:]:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
                os.close(current)
                current = child
            if self._identity(os.fstat(current)) != self._directory_identity:
                raise StorageError("storage-directory-replaced")
            info = os.stat(self.path.name, dir_fd=current, follow_symlinks=False)
            self._check_file(info)
            if self._identity(info) != self._file_identity:
                raise StorageError("storage-file-replaced")
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    sidecar = os.stat(self.path.name + suffix, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                self._check_file(sidecar)
                if stat.S_IMODE(sidecar.st_mode) & 0o077:
                    raise StorageError("unsafe-storage-permissions")
        finally:
            os.close(current)

    def _initialize(self) -> None:
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StorageError("unsupported-storage-version")
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS projections (thread_id TEXT NOT NULL, generation INTEGER NOT NULL, parser_version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (thread_id, generation, parser_version))",
                "CREATE TABLE IF NOT EXISTS views (thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)",
            ):
                connection.execute(statement)
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('sourceInstanceId', ?)", (str(uuid4()),))
            self.source_instance_id = connection.execute("SELECT value FROM metadata WHERE key='sourceInstanceId'").fetchone()[0]
            try:
                if str(UUID(self.source_instance_id)) != self.source_instance_id:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise StorageError("invalid-source-instance-id") from None
            connection.execute("PRAGMA user_version=1")

    class _Transaction:
        def __init__(self, store: DerivedStore):
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            store = self.store
            store._lock.acquire()
            try:
                store._verify_location()
                assert store._connection is not None
                store._connection.execute("BEGIN IMMEDIATE")
                return store._connection
            except BaseException as error:
                store._lock.release()
                if isinstance(error, sqlite3.Error):
                    raise StorageError("storage-transaction-failed") from None
                raise

        def __exit__(self, kind: Any, *_: Any) -> None:
            store = self.store
            try:
                if kind is None:
                    try:
                        store._verify_location()
                    except BaseException:
                        store._connection.execute("ROLLBACK")
                        raise
                store._connection.execute("COMMIT" if kind is None else "ROLLBACK")
                if kind is not None and issubclass(kind, sqlite3.Error):
                    raise StorageError("storage-transaction-failed") from None
            except sqlite3.Error:
                raise StorageError("storage-transaction-failed") from None
            finally:
                store._lock.release()

    def _transaction(self) -> _Transaction:
        return self._Transaction(self)

    def _one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._lock:
            self._verify_location()
            try:
                return self._connection.execute(sql, params).fetchone()
            except sqlite3.Error:
                raise StorageError("storage-read-failed") from None

    def load_projection(self, thread: str, generation: int, parser_version: str) -> Any | None:
        row = self._one("SELECT payload FROM projections WHERE thread_id=? AND generation=? AND parser_version=?",
                        (_key(thread), _revision(generation), _key(parser_version)))
        return None if row is None else _decode(row[0])

    def save_projection(self, thread: str, generation: int, parser_version: str, payload: Any) -> None:
        params = (_key(thread), _revision(generation), _key(parser_version), _encode(payload))
        with self._transaction() as connection:
            connection.execute("INSERT INTO projections VALUES(?,?,?,?) ON CONFLICT(thread_id,generation,parser_version) DO UPDATE SET payload=excluded.payload", params)

    def load_view(self, thread: str) -> dict[str, Any]:
        row = self._one("SELECT revision,payload FROM views WHERE thread_id=?", (_key(thread),))
        return {"preferenceRevision": 0, "state": None} if row is None else {"preferenceRevision": row[0], "state": _decode(row[1])}

    def save_view(self, thread: str, expected: int, state: dict[str, Any]) -> dict[str, Any]:
        identifier, revision = _key(thread), _revision(expected)
        encoded = _encode(state, maximum=_MAX_VIEW_BYTES)
        if not self._view_validator.is_valid(state):
            raise StorageError("invalid-view-state")
        with self._transaction() as connection:
            row = connection.execute("SELECT revision,payload FROM views WHERE thread_id=?", (identifier,)).fetchone()
            current = row[0] if row else 0
            if revision != current:
                return {"saved": False, "preferenceRevision": current, "state": _decode(row[1]) if row else _decode(encoded)}
            connection.execute("INSERT INTO views VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (identifier, current + 1, encoded))
            return {"saved": True, "preferenceRevision": current + 1, "state": json.loads(encoded)}

    def get_receipt(self, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT revision,payload FROM receipts WHERE key=?", (_key(key),))
        return None if row is None else {"revision": row[0], "value": _decode(row[1])}

    def put_receipt(self, key: str, value: Any, expected_revision: int = 0) -> dict[str, Any]:
        """CAS receipt; expected 0 inserts only, so retry cannot erase evidence.

        Return the current value on conflict. Receipt cleanup is deliberately
        absent: releasing temporary export resources must not forget outcomes.
        """
        identifier, expected, encoded = _key(key), _revision(expected_revision), _encode(value)
        with self._transaction() as connection:
            row = connection.execute("SELECT revision,payload FROM receipts WHERE key=?", (identifier,)).fetchone()
            current = row[0] if row else 0
            if current != expected:
                return {"saved": False, "revision": current, "value": _decode(row[1]) if row else None}
            connection.execute("INSERT INTO receipts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (identifier, current + 1, encoded))
            return {"saved": True, "revision": current + 1, "value": json.loads(encoded)}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._connection.close()
            if self._directory >= 0:
                os.close(self._directory)

    def __enter__(self) -> DerivedStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
