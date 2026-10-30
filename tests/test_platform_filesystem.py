"""文件系统适配契约；Windows 专有句柄/DACL 用例只在原生 Windows 执行。"""
import concurrent.futures
import errno
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from codex_trace.platform import filesystem as fs


class FilesystemContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-trace-")
        # macOS /var 是链接；真正的生产入口同样要求无链接绝对路径。
        self.root = Path(self.temporary.name).resolve() / "中文 空格"
        self.directory = fs.open_directory(self.root, create=True, private=True)

    def tearDown(self):
        fs.close(self.directory)
        self.temporary.cleanup()

    def create(self, name, data=b"abcdef"):
        fd = fs.open(name, fs.O_RDWR | fs.O_CREAT | fs.O_EXCL | fs.O_NOFOLLOW, 0o600, dir_fd=self.directory)
        os.write(fd, data)
        fs.fsync(fd)
        return fd

    def test_private_unicode_path_and_position_independent_read(self):
        fd = self.create("文件 空格.bin")
        try:
            info = fs.fstat(fd)
            self.assertEqual(info.st_uid, fs.getuid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(fs.fstat(self.directory).st_mode), 0o700)
            os.lseek(fd, 1, os.SEEK_SET)
            duplicate = fs.dup(fd)
            try:
                self.assertEqual(fs.pread(duplicate, 2, 3), b"de")
                self.assertEqual(os.read(fd, 2), b"bc")
            finally:
                fs.close(duplicate)
        finally:
            fs.close(fd)

    def test_publish_hardlink_never_overwrites_and_reads_back(self):
        fd = self.create("temporary.bin")
        try:
            fs.link("temporary.bin", "final.zip", src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
            with self.assertRaises(FileExistsError):
                fs.link("temporary.bin", "final.zip", src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
            final = fs.open("final.zip", fs.O_RDONLY | fs.O_NOFOLLOW, dir_fd=self.directory)
            try:
                self.assertEqual(fs.pread(final, 20, 0), b"abcdef")
                self.assertEqual(fs.fstat(final).st_ino, fs.fstat(fd).st_ino)
            finally:
                fs.close(final)
        finally:
            fs.close(fd)

    def test_concurrent_publication_has_one_winner(self):
        descriptors = [self.create(f"temporary-{index}.bin", str(index).encode()) for index in range(8)]
        def publish(index):
            try:
                fs.link(f"temporary-{index}.bin", "winner.zip", src_dir_fd=self.directory,
                        dst_dir_fd=self.directory, follow_symlinks=False)
                return index
            except FileExistsError:
                return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                winners = [value for value in pool.map(publish, range(8)) if value is not None]
            self.assertEqual(len(winners), 1)
            fd = fs.open("winner.zip", fs.O_RDONLY | fs.O_NOFOLLOW, dir_fd=self.directory)
            try:
                self.assertEqual(fs.pread(fd, 10, 0), str(winners[0]).encode())
            finally:
                fs.close(fd)
        finally:
            for fd in descriptors:
                fs.close(fd)

    def test_unlinked_private_spool_stays_usable_until_close(self):
        fd = self.create("spool.bin")
        try:
            fs.unlink("spool.bin", dir_fd=self.directory)
            os.lseek(fd, 0, os.SEEK_SET)
            self.assertEqual(os.read(fd, 20), b"abcdef")
            os.write(fd, b"still writable")
        finally:
            fs.close(fd)
        self.assertNotIn("spool.bin", fs.listdir(self.directory))


@unittest.skipUnless(os.name == "nt", "requires native Windows handles, ACLs and sharing semantics")
class WindowsFilesystemTests(FilesystemContractTests):
    def test_storage_errors_keep_portable_errno_and_sanitized_messages(self):
        from codex_trace.platform import _windows
        for code in (39, 112):
            with self.subTest(code=code):
                error = _windows._error(code)
                self.assertEqual(error.errno, errno.ENOSPC)
                self.assertEqual(error.strerror, "windows-storage-full")
        quota = _windows._error(1816)
        self.assertEqual(quota.errno, getattr(errno, "EDQUOT", errno.ENOSPC))
        self.assertEqual(quota.strerror, "windows-storage-quota-exceeded")
        unknown = _windows._error(87)
        self.assertEqual(unknown.errno, 87)
        self.assertEqual(unknown.strerror, "windows-filesystem-error")

    def test_path_aliases_and_device_names_rejected(self):
        for name in ("..", ".", "a:b", "NUL", "con.txt", "CON .txt", "COM1.zip", "LPT¹.txt", "x.", "x ", "a\\b", "a/b", "a\x7fb"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fs.open(name, fs.O_RDONLY, dir_fd=self.directory)
        for path in ("C:relative", "\\\\server\\share\\x", "\\\\?\\C:\\x", "C:\\x\\..\\y", "C:\\x\\.\\y"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                fs.open_directory(path)

    def test_ancestor_pin_blocks_rename_until_closed(self):
        chain = self.root / "parent"
        (chain / "child").mkdir(parents=True)
        parent = fs.open_directory(chain)
        child = fs.open("child", fs.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW, dir_fd=parent)
        fs.close(parent)
        try:
            with self.assertRaises(OSError):
                chain.rename(chain.with_name("renamed"))
        finally:
            fs.close(child)
        chain.rename(chain.with_name("renamed"))

    def test_sqlite_inherited_journal_acl_remains_private(self):
        from codex_trace.storage import DerivedStore
        with DerivedStore(self.root / "index") as store:
            with store._transaction() as connection:
                connection.execute("INSERT INTO metadata VALUES('journal-fixture','synthetic')")
                journal = fs.stat(store.path.with_name(store.path.name + "-journal"), follow_symlinks=False)
                self.assertEqual(journal.st_uid, fs.getuid())
                self.assertEqual(stat.S_IMODE(journal.st_mode), 0o600)
            self.assertEqual(store.get_receipt("missing"), None)

    def test_junction_is_rejected_at_every_depth(self):
        destination = self.root.parent / "target"
        destination.mkdir()
        junction = self.root.parent / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(destination)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, "native junction fixture could not be created")
        try:
            for path in (junction, junction / "child"):
                with self.subTest(path=path), self.assertRaises(OSError):
                    fs.open_directory(path, create=True)
        finally:
            junction.rmdir()

    def test_exclusive_file_in_use_fails_without_mutation(self):
        from codex_trace.platform import _windows
        fd = self.create("occupied.bin", b"original")
        fs.close(fd)
        handle = _windows._raw_open(str(self.root / "occupied.bin"), _windows._GENERIC_READ, 0)
        try:
            with self.assertRaises(PermissionError):
                fs.open("occupied.bin", fs.O_RDONLY | fs.O_NOFOLLOW, dir_fd=self.directory)
        finally:
            _windows._close(handle)
        self.assertEqual((self.root / "occupied.bin").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
