"""跨平台测试资源；只跳过缺少 Windows 符号链接权限的单个链接用例。"""
import os
from codex_display.platform import filesystem


def assert_private_mode(test, path, expected):
    """Windows 映射模式来自实际 owner SID/DACL 校验，不能用 Path.stat 的 chmod 位代替。"""
    test.assertEqual(filesystem.stat(path, follow_symlinks=False).st_mode & 0o777, expected)


def symlink(test, path, target, *, target_is_directory=False):
    """Windows 普通用户可能没有创建 symlink 权限；其他文件错误仍须使测试失败。"""
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            test.skipTest("Windows symlink fixture requires Developer Mode or SeCreateSymbolicLinkPrivilege")
        raise
