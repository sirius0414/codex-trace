"""由隔离 Python -I -B 启动；业务代码来自插件副本，依赖来自内置或 uv 管理的环境。"""
from pathlib import Path
import site
import sys


def main() -> int:
    # -I 排除当前目录、用户 site 和 PYTHONPATH；再明确加入随包目录，保持可移动；社区包依赖由 uv 环境提供。
    root = Path(__file__).resolve().parent
    sys.path[:0] = [str(root / "backend"), str(root / "runtime-deps")]
    try:
        # 仅处理可信随包依赖的 .pth；pywin32 通过它注册 win32/lib 及 DLL 目录。
        # 单独改 sys.path 不会触发此初始化，Windows 原生模块因此无法加载。
        site.addsitedir(str(root / "runtime-deps"))
        from codex_trace.marketplace import main as launch
    except Exception:
        print("Codex Trace: 插件启动文件不完整；请重新安装完整插件包。", file=sys.stderr)
        return 1
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
