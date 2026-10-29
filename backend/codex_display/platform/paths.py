"""本地派生数据位置；只读 Codex 来源仍由 CODEX_HOME 单独指定。"""
from pathlib import Path
import os


def default_state_root() -> Path:
    """Windows 使用本用户 LocalAppData，macOS 保留已发布的目录布局。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        if not base.is_absolute():
            raise ValueError("absolute-localappdata-required")
        return base / "codex-display" / "index"
    return Path.home() / ".local" / "share" / "codex-display" / "index"
