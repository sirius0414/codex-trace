#!/usr/bin/env python3
"""仅供仓库本地调试的浏览器宿主，不属于正式插件或安装流程。

HTTP 仅监听回环地址，原样转发白名单工具到独立的正式 MCP stdio 进程。
界面仍使用正式离线构建；索引和导出放在 build/dev-browser，避免混用安装数据。
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from uuid import uuid4

from package_plugin import offline_html
from platform_runtime import npm_command
from platform_dev import private_browser_launch, protect_windows_children

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "dist/ui"
DEV_ROOT = ROOT / "build/dev-browser"
HOST_FILES = ROOT / "scripts/browser_dev"
MAX_REQUEST_BYTES = 1024 * 1024


def create_app(python: Path, codex: Path, port: int, *, assets: Path = ASSETS,
               state_root: Path = DEV_ROOT / "index",
               export_root: Path = DEV_ROOT / "Downloads/Codex Trace",
               home: Path | None = None, instance_id: str | None = None,
               shutdown_token: str | None = None, shutdown_callback=None):
    """建立开发宿主；MCP 生命周期与 HTTP 服务绑定，工具契约仍由正式入口校验。"""
    # 延迟导入，允许系统 Python 先找到已有的锁定运行环境，再启动此服务。
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    token = secrets.token_urlsafe(32)
    instance = instance_id or str(uuid4())
    authorities = {f"localhost:{port}", f"127.0.0.1:{port}"}
    if port == 80:
        authorities.update({"localhost", "127.0.0.1"})
    tools = set(json.loads((ROOT / "contracts/tools.json").read_text(encoding="utf-8"))["tools"])
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                   "CODEX_TRACE_CODEX_EXECUTABLE": str(codex),
                   "CODEX_TRACE_STATE_ROOT": str(state_root.absolute()),
                   "CODEX_TRACE_EXPORT_ROOT": str(export_root.absolute())}
    if home is not None:
        environment["CODEX_HOME"] = str(home.absolute())

    @asynccontextmanager
    async def lifespan(app):
        # 正式导出器只创建最终目录；开发宿主负责准备独立的 Downloads 祖先目录。
        export_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parameters = StdioServerParameters(command=str(python), args=["-m", "codex_trace.app"],
                                           cwd=str(ROOT), env=environment)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                await session.initialize()
                app.state.session = session
                yield

    async def index(request):
        # 回环地址上的其他 OS 用户也能 GET；HTTP 只能返回不含凭据的页面。
        return HTMLResponse((HOST_FILES / "host.html").read_text(encoding="utf-8"))

    async def host_script(request):
        return Response((HOST_FILES / "host.js").read_text(encoding="utf-8"), media_type="text/javascript")

    async def health(request):
        # 仅确认本开发宿主实例已完成 lifespan 初始化，不读取任务、不创建观察会话。
        return JSONResponse({"service": "codex-trace-dev-browser", "pid": os.getpid(), "instanceId": instance})

    async def shutdown(request):
        if (not shutdown_token or shutdown_callback is None or not secrets.compare_digest(
                request.headers.get("x-codex-trace-shutdown", ""), shutdown_token)):
            return JSONResponse({"message": "停止凭据无效。"}, status_code=403)
        shutdown_callback()
        return JSONResponse({"accepted": True}, status_code=202)

    async def ui(request):
        # 每次刷新读取最新构建；npm run build 后不必重启 Python 服务。
        try:
            # 正式嵌入面板默认固定高度；仅在开发宿主覆盖，使浏览器窗口内的控件完整可见。
            html = offline_html(assets).replace("</head>", "<style>.cx-panel{--cx-height:100%}</style></head>")
            return HTMLResponse(html)
        except (OSError, ValueError):
            return HTMLResponse("前端构建不可用，请运行 npm run build 后刷新页面。", status_code=503)

    async def rpc(request):
        if not secrets.compare_digest(request.headers.get("x-codex-trace-token", ""), token):
            return JSONResponse({"message": "本地调试凭据已失效，请刷新页面。"}, status_code=403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse({"message": "请求必须使用 JSON。"}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return JSONResponse({"message": "请求过大。"}, status_code=413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"message": "JSON 请求无效。"}, status_code=400)
        if (not isinstance(payload, dict) or set(payload) != {"name", "arguments"}
                or not isinstance(payload["name"], str) or not isinstance(payload["arguments"], dict)):
            return JSONResponse({"message": "工具请求格式无效。"}, status_code=400)
        # 全局 open 强制选择任务；浏览器参数不能伪造宿主的 thread 身份或调用任意 MCP 工具。
        if payload["name"] not in tools:
            return JSONResponse({"message": "接口不存在。"}, status_code=404)
        try:
            result = await request.app.state.session.call_tool(payload["name"], payload["arguments"])
            return JSONResponse(result.model_dump(by_alias=True, exclude_none=True))
        except Exception:
            # 不暴露底层异常、请求内容或路径。保存请求失败后仍由原有界面按请求 ID 核对。
            return JSONResponse({"message": "本地后端连接未完成，请重试；重启服务后请刷新页面。"}, status_code=502)

    app = Starlette(routes=[Route("/", index), Route("/host.js", host_script), Route("/app.html", ui),
                            Route("/rpc", rpc, methods=["POST"]), Route("/_dev/health", health),
                            Route("/_dev/shutdown", shutdown, methods=["POST"])], lifespan=lifespan)
    app.state.token = token

    async def local_boundary(request, call_next):
        authority = request.headers.get("host", "")
        origin = request.headers.get("origin")
        # file:// 私有入口的顶层跳转可被浏览器标成 cross-site。只允许无 Origin
        # 的 GET 文档导航取得不含凭据的根页面；资源及 RPC 仍保留原同源边界。
        public_navigation = (request.method == "GET" and request.url.path == "/" and origin is None
                             and request.headers.get("sec-fetch-mode") == "navigate"
                             and request.headers.get("sec-fetch-dest") == "document")
        # 即使绑定回环地址仍校验 Host/Origin，防止其他网页借本机服务读取会话。
        if (authority not in authorities or (origin is not None and origin != f"http://{authority}")
                or (request.headers.get("sec-fetch-site") == "cross-site" and not public_navigation)):
            return JSONResponse({"message": "仅允许本机同源访问。"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                 "frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"})
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_boundary)
    return app


def main() -> int:
    from package_plugin import executable, locked_versions, verify_runtime
    from update_plugin import runtime_paths

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    parser.add_argument("--python", help="已有的 Python ≥3.14 运行环境")
    parser.add_argument("--codex", help="桌面配套的 Codex app-server 可执行文件")
    parser.add_argument("--skip-build", action="store_true", help="复用 dist/ui，跳过启动时的前端构建")
    parser.add_argument("--instance-id", help=argparse.SUPPRESS)
    parser.add_argument("--shutdown-token", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 之间")
    try:
        python, codex = runtime_paths(args.python, args.codex)
        verify_runtime(python, locked_versions(ROOT / "requirements.lock"))
        # 保留 venv 路径，不能 resolve 到系统 Python 后丢失已安装依赖。
        if os.path.normcase(str(Path(sys.executable).absolute())) != os.path.normcase(str(python)):
            os.execv(str(python), [str(python), str(Path(__file__).absolute()),
                                  *sys.argv[1:], "--python", str(python), "--codex", str(codex)])
        if not args.skip_build:
            subprocess.run(npm_command("run", "build"), cwd=ROOT, check=True)
        offline_html(ASSETS)  # 缺失或无效构建在监听之前报错。
        import uvicorn

        windows_job = protect_windows_children()

        instance = args.instance_id or str(uuid4())
        app = create_app(python, codex, args.port, instance_id=instance,
                         shutdown_token=args.shutdown_token,
                         shutdown_callback=lambda: setattr(server, "should_exit", True))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                    access_log=False, log_level="warning", proxy_headers=False, timeout_graceful_shutdown=5))
        # fragment 不随 HTTP 请求发送；终端/管理器只输出私有文件 URI，不输出凭据。
        with private_browser_launch(DEV_ROOT, instance, args.port, app.state.token) as launch:
            print(f"本地调试入口：{launch.as_uri()}", flush=True)
            print(f"调试数据：{DEV_ROOT}\nCtrl+C 停止服务；正式插件无需更新。", flush=True)
            server.run()
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"本地调试服务启动失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
