"""本地派生数据位置；只读 Codex 来源仍由 CODEX_HOME 单独指定。"""
from pathlib import Path
import os


def default_state_root() -> Path:
    """在当前用户的本地数据目录中使用 Codex Trace 独立命名空间。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        if not base.is_absolute():
            raise ValueError("absolute-localappdata-required")
        return base / "codex-trace" / "index"
    return Path.home() / ".local" / "share" / "codex-trace" / "index"
