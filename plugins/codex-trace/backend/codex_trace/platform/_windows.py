"""本地 Windows 卷的安全文件操作；只在 Windows 宿主加载。

CreateFileW 逐层拒绝所有 reparse point（包括 junction），目录句柄不共享写入或删除，
并保留祖先句柄，防止检查后重定向。普通文件使用 CRT 二进制 fd，私有数据使用当前
用户及 SYSTEM 的 protected DACL。发布用 handle-relative FileLinkInformation 原子拒绝覆盖。
不支持 UNC、设备路径、ADS 或别名组件；不把 chmod/resolve 当作 Windows 安全边界。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import errno
import msvcrt
import ntpath
import os
from pathlib import PureWindowsPath
import re
import stat as _stat
import threading
from types import SimpleNamespace

O_RDONLY, O_WRONLY, O_RDWR = os.O_RDONLY, os.O_WRONLY, os.O_RDWR
O_CREAT, O_EXCL, O_APPEND, O_TRUNC = os.O_CREAT, os.O_EXCL, os.O_APPEND, os.O_TRUNC
# 这些标识只在本适配层解释，绝不传给 CRT。
O_NOFOLLOW, O_DIRECTORY, O_NONBLOCK = 1 << 26, 1 << 27, 1 << 28
_READ_ATTRIBUTES, _READ_CONTROL, _WRITE_DAC = 0x80, 0x20000, 0x40000
_DELETE = 0x10000
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_SHARE_READ, _SHARE_WRITE, _SHARE_DELETE = 1, 2, 4
_BACKUP, _REPARSE, _OPEN_REPARSE = 0x02000000, 0x400, 0x00200000
_DIRECTORY_ATTRIBUTE = 0x10
_INVALID = ctypes.c_void_p(-1).value
_EPOCH = 116444736000000000
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:[ .].*)?", re.I)
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


class _ByHandleInfo(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME), ("accessed", w.FILETIME),
                ("written", w.FILETIME), ("volume", w.DWORD), ("sizeHigh", w.DWORD),
                ("sizeLow", w.DWORD), ("links", w.DWORD), ("indexHigh", w.DWORD), ("indexLow", w.DWORD)]


class _BasicInfo(ctypes.Structure):
    _fields_ = [("CreationTime", ctypes.c_int64), ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64), ("ChangeTime", ctypes.c_int64), ("FileAttributes", w.DWORD)]


class _AclSize(ctypes.Structure):
    _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD)]


class _LinkInfo(ctypes.Structure):
    _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("RootDirectory", w.HANDLE),
                ("FileNameLength", w.DWORD), ("FileName", w.WCHAR * 1)]


class _IoStatus(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


def _api(dll, name, args, result):
    value = getattr(dll, name)
    value.argtypes, value.restype = args, result
    return value


_create = _api(_kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(_SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
_close = _api(_kernel, "CloseHandle", [w.HANDLE], w.BOOL)
_information = _api(_kernel, "GetFileInformationByHandle", [w.HANDLE, ctypes.POINTER(_ByHandleInfo)], w.BOOL)
_basic = _api(_kernel, "GetFileInformationByHandleEx", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL)
_set_information = _api(_kernel, "SetFileInformationByHandle", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL)
_create_directory = _api(_kernel, "CreateDirectoryW", [w.LPCWSTR, ctypes.POINTER(_SecurityAttributes)], w.BOOL)
_reopen = _api(_kernel, "ReOpenFile", [w.HANDLE, w.DWORD, w.DWORD, w.DWORD], w.HANDLE)
_file_type = _api(_kernel, "GetFileType", [w.HANDLE], w.DWORD)
_final_path = _api(_kernel, "GetFinalPathNameByHandleW", [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD)
_free = _api(_kernel, "LocalFree", [w.HLOCAL], w.HLOCAL)
_get_process = _api(_kernel, "GetCurrentProcess", [], w.HANDLE)
_token = _api(_advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
_token_info = _api(_advapi, "GetTokenInformation", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
_sid_text = _api(_advapi, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL)
_sddl = _api(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)], w.BOOL)
_get_security = _api(_advapi, "GetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.LPVOID)], w.DWORD)
_set_security = _api(_advapi, "SetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID, w.LPVOID], w.DWORD)
_security_control = _api(_advapi, "GetSecurityDescriptorControl", [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)], w.BOOL)
_security_dacl = _api(_advapi, "GetSecurityDescriptorDacl", [w.LPVOID, ctypes.POINTER(w.BOOL), ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.BOOL)], w.BOOL)
_acl_info = _api(_advapi, "GetAclInformation", [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int], w.BOOL)
_get_ace = _api(_advapi, "GetAce", [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)], w.BOOL)
_nt_set_information = _api(_ntdll, "NtSetInformationFile", [w.HANDLE, ctypes.POINTER(_IoStatus), w.LPVOID, w.DWORD, ctypes.c_int], w.LONG)
_nt_error = _api(_ntdll, "RtlNtStatusToDosError", [w.LONG], w.ULONG)


def _error(code=None):
    code = ctypes.get_last_error() if code is None else code
    # 不包含来源绝对路径或任务正文。保持常用 Python 异常供上层幂等恢复。
    if code in (2, 3):
        return FileNotFoundError(errno.ENOENT, "windows-file-not-found")
    if code in (80, 183):
        return FileExistsError(errno.EEXIST, "windows-file-exists")
    if code in (5, 32, 33):
        return PermissionError(errno.EACCES, "windows-file-unavailable")
    if code in (39, 112):  # ERROR_HANDLE_DISK_FULL / ERROR_DISK_FULL
        return OSError(errno.ENOSPC, "windows-storage-full")
    if code == 1816:  # ERROR_NOT_ENOUGH_QUOTA
        return OSError(getattr(errno, "EDQUOT", errno.ENOSPC), "windows-storage-quota-exceeded")
    return OSError(code, "windows-filesystem-error")


def _require(value):
    if not value:
        raise _error()


def _sid(value):
    result = w.LPWSTR()
    _require(_sid_text(value, ctypes.byref(result)))
    try:
        return result.value
    finally:
        _free(ctypes.cast(result, w.HLOCAL))


def _current_user():
    token = w.HANDLE()
    _require(_token(_get_process(), 8, ctypes.byref(token)))
    try:
        size = w.DWORD()
        _token_info(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise _error()
        data = ctypes.create_string_buffer(size.value)
        _require(_token_info(token, 1, data, size, ctypes.byref(size)))
        return _sid(ctypes.cast(data, ctypes.POINTER(w.LPVOID))[0])
    finally:
        _close(token)


_USER = _current_user()


def getuid():
    """返回当前用户 SID；上层仅用它做所有权相等比较。"""
    return _USER


def _component(name):
    if (not isinstance(name, str) or not name or name in (".", "..") or name[-1:] in (".", " ")
            or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"/\\|?*' for char in name)
            or _RESERVED.fullmatch(name)):
        raise ValueError("unsafe-windows-path-component")
    return name


def _absolute(value):
    value = os.fspath(value)
    if not isinstance(value, str) or not re.match(r"^[A-Za-z]:[\\/]", value):
        raise ValueError("local-absolute-windows-path-required")
    # 校验原始拼写，不能让 PurePath 先消除点组件或空组件。
    normalized = value.replace("/", "\\")
    tail = normalized[3:]
    if tail:
        for name in tail.split("\\"):
            _component(name)
    return normalized


def _native(path):
    if path.startswith("\\\\?\\Volume{"):
        return path
    return "\\\\?\\" + path


def _volume_path(handle):
    """后续查找绑定卷 GUID，避免检查后盘符映射/SUBST 改变重定向整条路径。"""
    size = _final_path(handle, None, 0, 1)  # VOLUME_NAME_GUID，不接受 SMB/盘符降级。
    if not size:
        raise _error()
    buffer = ctypes.create_unicode_buffer(size + 1)
    length = _final_path(handle, buffer, len(buffer), 1)
    if not length or length >= len(buffer):
        raise _error()
    if not buffer.value.startswith("\\\\?\\Volume{"):
        raise PermissionError("local-volume-required")
    return buffer.value


def _private_descriptor(directory):
    descriptor = w.LPVOID()
    inherit = "OICI" if directory else ""
    _require(_sddl(f"O:{_USER}D:P(A;{inherit};FA;;;{_USER})(A;{inherit};FA;;;SY)", 1, ctypes.byref(descriptor), None))
    return descriptor


def _handle_info(handle):
    result = _ByHandleInfo()
    _require(_information(handle, ctypes.byref(result)))
    if result.attributes & _REPARSE:
        raise PermissionError("windows-reparse-point-rejected")
    return result


def _security(handle):
    owner, dacl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    code = _get_security(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code:
        raise _error(code)
    try:
        owner_sid = _sid(owner)
        control, revision = w.WORD(), w.DWORD()
        _require(_security_control(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        # SQLite 自建边车继承私有根的 ACE，但子项本身不带 PROTECTED 标志。
        # 安全判据是实际 DACL 只授权当前用户/SYSTEM；不可把继承本身误判为公开。
        private = bool(dacl)
        if private:
            size = _AclSize()
            _require(_acl_info(dacl, ctypes.byref(size), ctypes.sizeof(size), 2))
            # 只有当前用户和 SYSTEM 的允许 ACE 才能表达私有数据；未知 ACE 失败关闭。
            for index in range(size.AceCount):
                ace = w.LPVOID()
                _require(_get_ace(dacl, index, ctypes.byref(ace)))
                if ctypes.c_ubyte.from_address(ace.value).value != 0 or _sid(ace.value + 8) not in (_USER, "S-1-5-18"):
                    private = False
                    break
        return owner_sid, private
    finally:
        _free(descriptor)


class _Pin:
    def __init__(self, handle):
        self.handle, self.references = handle, 1
        self.lock = threading.Lock()

    def retain(self):
        with self.lock:
            self.references += 1

    def release(self):
        with self.lock:
            self.references -= 1
            if not self.references:
                _require(_close(self.handle))


class _Directory:
    def __init__(self, path, handle, ancestors=()):
        try:
            self.path = _volume_path(handle)
        except BaseException:
            _close(handle)
            raise
        self.closed = False
        self.pins = (*ancestors, _Pin(handle))
        for pin in ancestors:
            pin.retain()

    @property
    def handle(self):
        if self.closed:
            raise OSError(errno.EBADF, "closed-directory")
        return self.pins[-1].handle


def _raw_open(path, access, share, disposition=3, *, directory=False, security=None):
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), security, False) if security else None
    handle = _create(_native(path), access, share, ctypes.byref(attributes) if attributes else None,
                     disposition, _BACKUP | _OPEN_REPARSE, None)
    if handle == _INVALID:
        raise _error()
    try:
        info = _handle_info(handle)
        if bool(info.attributes & _DIRECTORY_ATTRIBUTE) != directory:
            raise OSError(errno.ENOTDIR if directory else errno.EISDIR, "windows-file-type-mismatch")
        return handle
    except BaseException:
        _close(handle)
        raise


def _relative(path, directory):
    if not isinstance(directory, _Directory):
        raise TypeError("platform-directory-required")
    directory.handle
    return ntpath.join(directory.path, _component(os.fspath(path)))


def open(path, flags, mode=0o777, *, dir_fd=None):
    """安全打开本地文件；相对名称必须只含一个已校验组件。"""
    directory = bool(flags & O_DIRECTORY)
    if dir_fd is None:
        path = _absolute(path)
        pure = PureWindowsPath(path)
        if path == pure.anchor:
            if not directory:
                raise IsADirectoryError("windows-volume-root")
            handle = _raw_open(path, _READ_ATTRIBUTES | _READ_CONTROL, _SHARE_READ, directory=True)
            return _Directory(path, handle)
        parent = open_directory(str(pure.parent))
        try:
            return open(pure.name, flags, mode, dir_fd=parent)
        finally:
            close(parent)
    path = _relative(path, dir_fd)
    if directory:
        handle = _raw_open(path, _READ_ATTRIBUTES | _READ_CONTROL, _SHARE_READ, directory=True)
        return _Directory(path, handle, dir_fd.pins)
    access = _READ_ATTRIBUTES | _READ_CONTROL
    if flags & O_RDWR:
        access |= _GENERIC_READ | _GENERIC_WRITE
    elif flags & O_WRONLY:
        access |= _GENERIC_WRITE
    else:
        access |= _GENERIC_READ
    disposition = 1 if flags & O_CREAT and flags & O_EXCL else 4 if flags & O_CREAT else 3
    if flags & O_TRUNC:
        raise ValueError("unsafe-truncating-open")
    descriptor = _private_descriptor(False) if flags & O_CREAT else None
    try:
        handle = _raw_open(path, access, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, disposition, security=descriptor)
    finally:
        if descriptor:
            _free(descriptor)
    try:
        crt_flags = os.O_BINARY | (flags & (O_RDONLY | O_WRONLY | O_RDWR | O_APPEND))
        return msvcrt.open_osfhandle(handle, crt_flags)
    except BaseException:
        _close(handle)
        raise


def close(descriptor):
    if isinstance(descriptor, _Directory):
        if not descriptor.closed:
            descriptor.closed = True
            for pin in reversed(descriptor.pins):
                pin.release()
    else:
        os.close(descriptor)


def _handle(descriptor):
    return descriptor.handle if isinstance(descriptor, _Directory) else msvcrt.get_osfhandle(descriptor)


def fstat(descriptor):
    """返回真实卷/文件身份、change time 与依据 DACL 判断的权限位。"""
    handle = _handle(descriptor)
    if _file_type(handle) != 1:  # FILE_TYPE_DISK；管道/字符设备不虚构所有权或普通文件类型。
        if isinstance(descriptor, _Directory):
            raise OSError("disk-directory-required")
        return os.fstat(descriptor)
    info, basic = _handle_info(handle), _BasicInfo()
    _require(_basic(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)))
    owner, private = _security(handle)
    directory = bool(info.attributes & _DIRECTORY_ATTRIBUTE)
    permission = (0o700 if directory else 0o600) if private else (0o777 if directory else 0o666)
    def nanos(value):
        return (value - _EPOCH) * 100
    return SimpleNamespace(st_dev=info.volume, st_ino=(info.indexHigh << 32) | info.indexLow,
                           st_size=(info.sizeHigh << 32) | info.sizeLow, st_nlink=info.links,
                           st_uid=owner, st_gid=owner, st_mode=(_stat.S_IFDIR if directory else _stat.S_IFREG) | permission,
                           st_atime_ns=nanos(basic.LastAccessTime), st_mtime_ns=nanos(basic.LastWriteTime),
                           st_ctime_ns=nanos(basic.ChangeTime), st_atime=nanos(basic.LastAccessTime) / 1e9,
                           st_mtime=nanos(basic.LastWriteTime) / 1e9, st_ctime=nanos(basic.ChangeTime) / 1e9)


def stat(path, *, dir_fd=None, follow_symlinks=True):
    """目录项也通过 OPEN_REPARSE_POINT 打开；从不跟随链接，即使调用方省略参数。"""
    if dir_fd is None:
        path = _absolute(path)
        pure = PureWindowsPath(path)
        if path == pure.anchor:
            directory = open_directory(path)
            try:
                return fstat(directory)
            finally:
                close(directory)
        parent = open_directory(str(pure.parent))
        try:
            return stat(pure.name, dir_fd=parent, follow_symlinks=False)
        finally:
            close(parent)
    path = _relative(path, dir_fd)
    handle = _create(_native(path), _READ_ATTRIBUTES | _READ_CONTROL, 7, None, 3, _BACKUP | _OPEN_REPARSE, None)
    if handle == _INVALID:
        raise _error()
    try:
        info = _handle_info(handle)
        if info.attributes & _DIRECTORY_ATTRIBUTE:
            temporary = _Directory(path, handle, dir_fd.pins)
            handle = None
            try:
                return fstat(temporary)
            finally:
                close(temporary)
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None
        try:
            return fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if handle is not None:
            _close(handle)


def mkdir(path, mode=0o777, *, dir_fd=None):
    if dir_fd is None:
        pure = PureWindowsPath(_absolute(path))
        parent = open_directory(str(pure.parent))
        try:
            return mkdir(pure.name, mode, dir_fd=parent)
        finally:
            close(parent)
    path = _relative(path, dir_fd)
    descriptor = _private_descriptor(True)
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        _require(_create_directory(_native(path), ctypes.byref(attributes)))
    finally:
        _free(descriptor)


def fchmod(descriptor, mode):
    """将自有对象收紧到受保护 DACL；mode 仅允许 0600 文件或 0700 目录。"""
    original = fstat(descriptor)
    directory = _stat.S_ISDIR(original.st_mode)
    if mode != (0o700 if directory else 0o600) or original.st_uid != _USER:
        raise PermissionError("unsupported-private-permissions")
    if isinstance(descriptor, _Directory):
        handle = _raw_open(descriptor.path, _READ_ATTRIBUTES | _READ_CONTROL | _WRITE_DAC,
                           _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, directory=True)
    else:
        handle = _reopen(_handle(descriptor), _READ_ATTRIBUTES | _READ_CONTROL | _WRITE_DAC, 7, _OPEN_REPARSE)
        if handle == _INVALID:
            raise _error()
    security = _private_descriptor(directory)
    try:
        info = _handle_info(handle)
        if (info.volume, (info.indexHigh << 32) | info.indexLow) != (original.st_dev, original.st_ino):
            raise PermissionError("private-object-replaced")
        present, defaulted, dacl = w.BOOL(), w.BOOL(), w.LPVOID()
        _require(_security_dacl(security, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
        code = _set_security(handle, 1, 0x80000004, None, None, dacl, None)
        if code:
            raise _error(code)
    finally:
        _free(security)
        _close(handle)


def unlink(path, *, dir_fd=None):
    """按已打开对象标记删除，不在检查后再次按名称删除其他对象。"""
    if dir_fd is None:
        pure = PureWindowsPath(_absolute(path))
        parent = open_directory(str(pure.parent))
        try:
            return unlink(pure.name, dir_fd=parent)
        finally:
            close(parent)
    path = _relative(path, dir_fd)
    handle = _raw_open(path, _DELETE | _READ_ATTRIBUTES, 7)
    try:
        deleting = w.BOOL(True)
        _require(_set_information(handle, 4, ctypes.byref(deleting), ctypes.sizeof(deleting)))
    finally:
        _close(handle)


def link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
    """最终名称通过原生 FileLinkInformation 原子创建；始终拒绝覆盖。

    Win32 SetFileInformationByHandle 的枚举不包含 FileLinkInformation，故仅此操作
    使用 NtSetInformationFile 的原生枚举 11，不能把 11 错传给 Win32 枚举。
    """
    if src_dir_fd is None or dst_dir_fd is None:
        raise ValueError("explicit-link-directories-required")
    source = _relative(src, src_dir_fd)
    name = _component(os.fspath(dst))
    # 目的路径由 pinned 目录句柄解释，不再次遍历名字，拒绝既有项而不覆盖。
    encoded = name.encode("utf-16-le")
    size = max(ctypes.sizeof(_LinkInfo), _LinkInfo.FileName.offset + len(encoded))
    buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(_LinkInfo)))
    info = ctypes.cast(buffer, ctypes.POINTER(_LinkInfo)).contents
    info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = False, dst_dir_fd.handle, len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + _LinkInfo.FileName.offset, encoded, len(encoded))
    handle = _raw_open(source, _DELETE | _READ_ATTRIBUTES, 7)
    try:
        status_block = _IoStatus()
        status = _nt_set_information(handle, ctypes.byref(status_block), buffer, size, 11)
        if status < 0:
            raise _error(_nt_error(status))
    finally:
        _close(handle)


def fsync(descriptor):
    """文件必须 flush；Windows 不提供本层可用的目录 flush/power-loss 保证。"""
    if isinstance(descriptor, _Directory):
        descriptor.handle
        return
    os.fsync(descriptor)


def listdir(descriptor):
    if isinstance(descriptor, _Directory):
        descriptor.handle
        return os.listdir(_native(descriptor.path))
    return os.listdir(descriptor)


dup = os.dup


def pread(descriptor, count, offset):
    """独立重开同一内核文件对象，不改变原 fd 或 dup 共享的文件指针。"""
    if type(count) is not int or type(offset) is not int or count < 0 or offset < 0:
        raise ValueError("invalid-pread-range")
    handle = _reopen(_handle(descriptor), _GENERIC_READ, 7, 0)
    if handle == _INVALID:
        raise _error()
    try:
        reopened = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _close(handle)
        raise
    try:
        os.lseek(reopened, offset, os.SEEK_SET)
        return os.read(reopened, count)
    finally:
        os.close(reopened)


def open_directory(path, *, create=False, private=False):
    path = _absolute(path)
    pure = PureWindowsPath(path)
    current = open(pure.anchor, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        for name in pure.parts[1:]:
            try:
                child = open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    mkdir(name, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=current)
            close(current)
            current = child
        if private:
            fchmod(current, 0o700)
        return current
    except BaseException:
        close(current)
        raise


def open_read(path):
    pure = PureWindowsPath(_absolute(path))
    directory = open_directory(str(pure.parent))
    try:
        return open(pure.name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=directory)
    finally:
        close(directory)
