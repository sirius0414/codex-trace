"""Production app-only MCP interface; task payloads are UI-private metadata."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading

from jsonschema import Draft202012Validator, FormatChecker
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Resource, TextContent, Tool, ToolAnnotations

from . import __version__
from .adapters.history import HistoryAdapter, HistoryError
from .adapters.raw import RawAdapter
from .probe import URI, PANEL_URI
from .service import TrajectoryService, ServiceError
from .storage import DerivedStore
from .export_store import ExportStore

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "contracts/trajectory.schema.json").read_text())
TOOLS = json.loads((ROOT / "contracts/tools.json").read_text())["tools"]
OPEN = {"open_codex_display": ("global", URI), "open_codex_display_panel": ("thread", PANEL_URI)}
UI_META = {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}},
           "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}}
server = Server("codex-display", version=__version__)
_service = None
_creation_lock = threading.Lock()


class LazyHistory:
    """Reconnect only our observation process after a failed read."""
    def __init__(self, executable):
        self.executable, self.adapter = executable, None
        self.lock = threading.RLock()

    def __getattr__(self, method):
        if method not in ("read", "read_history", "list_threads", "list_turns", "list_items"):
            raise AttributeError(method)
        def call(*args, **kwargs):
            with self.lock:
                try:
                    if self.adapter is None:
                        self.adapter = HistoryAdapter(self.executable)
                    return getattr(self.adapter, method)(*args, **kwargs)
                except HistoryError:
                    self.close()
                    raise
        return call

    def close(self):
        with self.lock:
            if self.adapter is not None:
                self.adapter.close()
                self.adapter = None


def get_service():
    global _service
    with _creation_lock:
        if _service is None:
            home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).absolute()
            state_root = os.environ.get("CODEX_DISPLAY_STATE_ROOT")
            export_root = os.environ.get("CODEX_DISPLAY_EXPORT_ROOT")
            history = LazyHistory(os.environ["CODEX_DISPLAY_CODEX_EXECUTABLE"])
            _service = TrajectoryService(history, RawAdapter([home / "sessions", home / "archived_sessions"]),
                                         DerivedStore(Path(state_root) if state_root else None), observe=True,
                                         export_destination=ExportStore(Path(export_root) if export_root else None))
        return _service


def tool_schema(name):
    definition = TOOLS["open_codex_display" if name in OPEN else name]["input"]
    return {"$defs": CONTRACT["$defs"], **CONTRACT["$defs"][definition]}


@server.list_tools()
async def list_tools():
    result = []
    for name in [*OPEN, *(name for name in TOOLS if name not in OPEN)]:
        metadata = {"ui": {"visibility": ["app"]}, "openai/visibility": "private"}
        if name in OPEN:
            surface, uri = OPEN[name]
            metadata["ui"]["resourceUri"] = uri
            metadata["openai/ui"] = {"entrypoints": [{"type": surface}]}
        result.append(Tool(name=name, title="执行轨迹" if name in OPEN else None,
                           description="Local execution observation. Task data stays in app-only result metadata.",
                           inputSchema=tool_schema(name),
                           annotations=ToolAnnotations(readOnlyHint=name not in ("save_view_state", "download_records"),
                                                       destructiveHint=False, openWorldHint=False), _meta=metadata))
    return result


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    resource_meta = {"ui": {"resourceUri": OPEN[name][1]}} if name in OPEN else {}
    try:
        if name not in OPEN and name not in TOOLS:
            raise ServiceError("notFound", "接口不存在。")
        Draft202012Validator(tool_schema(name), format_checker=FormatChecker()).validate(arguments)
        service = await asyncio.to_thread(get_service)
        if name in OPEN:
            meta = server.request_context.meta
            metadata = meta.model_dump(exclude_none=True) if meta else {}
            data = await asyncio.to_thread(service.open, metadata, OPEN[name][0])
        else:
            data = await asyncio.to_thread(getattr(service, name), arguments)
        definition = TOOLS["open_codex_display" if name in OPEN else name]["output"]
        Draft202012Validator({**CONTRACT, "$ref": f"#/$defs/{definition}"}).validate(data)
        return CallToolResult(content=[TextContent(type="text", text="Local observation result is available in the app.")],
                              _meta={**resource_meta, "codex-display/result": data})
    except Exception as error:
        code = error.code if isinstance(error, ServiceError) else "internalError"
        message = str(error) if isinstance(error, ServiceError) else "本地读取未完成，请重试；已有记录会保留。"
        data = {"viewSessionId": None, "binding": None, "code": code,
                "retryable": error.retryable if isinstance(error, ServiceError) else True, "message": message}
        return CallToolResult(content=[TextContent(type="text", text="Local observation request could not be completed.")],
                              isError=True, _meta={**resource_meta, "codex-display/result": data})


@server.list_resources()
async def list_resources():
    return [Resource(uri=uri, name="执行轨迹", mimeType="text/html;profile=mcp-app", _meta=UI_META) for uri in (URI, PANEL_URI)]


@server.read_resource()
async def read_resource(uri):
    if str(uri) not in (URI, PANEL_URI):
        raise ValueError("unknown-resource")
    path = Path(os.environ.get("CODEX_DISPLAY_UI_PATH", str(ROOT / "ui/trajectory-v1.html")))
    return [ReadResourceContents(content=path.read_text(), mime_type="text/html;profile=mcp-app", meta=UI_META)]


async def main():
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if _service is not None:
            _service.close()


if __name__ == "__main__":
    asyncio.run(main())
