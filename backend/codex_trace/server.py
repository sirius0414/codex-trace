"""保留的 M0 宿主接入诊断入口；正式插件由 codex_trace.app 启动。

诊断工具验证身份字段和合成下载，不提供任务执行变更接口。这里保留 M0 的
structuredContent 诊断协议；正式任务内容的 UI 私有信封以 app.py 为准。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import jsonschema

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Resource, TextContent, Tool, ToolAnnotations

from . import __version__
from .export_store import ExportStore
from .probe import (DIAGNOSTIC_CODES, DIAGNOSTIC_PHASES, FIELD_NAME_PATTERN, MAX_DIAGNOSTIC_ELAPSED_MS,
                    PANEL_URI, ProbeService, URI, inspect_source)

server = Server("codex-trace", version=__version__)
audit = os.environ.get("CODEX_TRACE_AUDIT_PATH")
service = ProbeService(Path(audit) if audit else None)
export_root = os.environ.get("CODEX_TRACE_EXPORT_ROOT")
export_store = ExportStore(Path(export_root) if export_root else None)
APP_META = {"ui": {"visibility": ["app"]}, "openai/visibility": "private"}
UI_META = {"ui": {"prefersBorder": False,
                  "csp": {"connectDomains": [], "resourceDomains": []}},
           "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}}
OPEN_TOOLS = {
    "open_codex_trace": ("global", URI),
    "open_codex_trace_panel": ("thread", PANEL_URI),
}


def schema(properties=None, required=None):
    """诊断参数同样拒绝未声明字段，避免采集自由文本或任务正文。"""
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


@server.list_tools()
async def list_tools():
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    return [
        Tool(name="open_codex_trace", title="Codex Trace", description="Open the local read-only execution viewer (M0 host validation).",
             inputSchema=schema(), annotations=annotations,
             _meta={"ui": {"resourceUri": URI, "visibility": ["app"]}, "openai/visibility": "private",
                    "openai/ui": {"entrypoints": [{"type": "global"}]}}),
        Tool(name="open_codex_trace_panel", title="Codex Trace", description="Open the local read-only execution viewer in a task panel (M0 host validation).",
             inputSchema=schema(), annotations=annotations,
             _meta={"ui": {"resourceUri": PANEL_URI, "visibility": ["app"]}, "openai/visibility": "private",
                    "openai/ui": {"entrypoints": [{"type": "thread"}]}}),
        Tool(name="probe_host_observation", description="Record M0 identity/instance field names locally; no task bodies.",
             inputSchema=schema({"activationId": {"type": "string"}, "uiInstanceId": {"type": "string"},
                                 "contextKeys": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 200, "pattern": FIELD_NAME_PATTERN}},
                                 "hostCapabilityKeys": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 200, "pattern": FIELD_NAME_PATTERN}},
                                 "displayMode": {"type": ["string", "null"], "maxLength": 50}},
                                ["activationId", "uiInstanceId", "contextKeys"]),
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False), _meta=APP_META),
        Tool(name="probe_diagnostic_event", description="Record bounded M0 UI/download milestones locally; no payloads or free-form errors.",
             inputSchema=schema({"activationId": {"type": "string", "format": "uuid"},
                                 "uiInstanceId": {"type": "string", "format": "uuid"},
                                 "phase": {"type": "string", "enum": list(DIAGNOSTIC_PHASES)},
                                 "code": {"type": "string", "enum": list(DIAGNOSTIC_CODES)},
                                 "elapsedMs": {"type": "integer", "minimum": 0, "maximum": MAX_DIAGNOSTIC_ELAPSED_MS},
                                 "visibility": {"type": "string", "enum": ["visible", "hidden"]},
                                 "contextKeys": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 200, "pattern": FIELD_NAME_PATTERN}},
                                 "hostCapabilityKeys": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 200, "pattern": FIELD_NAME_PATTERN}}},
                                ["activationId", "uiInstanceId", "phase"]),
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False), _meta=APP_META),
        Tool(name="probe_thread_identity", description="Verify an explicitly selected task ID via read-only history and source header.",
             inputSchema=schema({"threadId": {"type": "string", "format": "uuid"}}, ["threadId"]), annotations=annotations, _meta=APP_META),
        Tool(name="probe_local_download", description="Read synthetic ZIP bytes to test pure local host downloading (no actual task data).",
             inputSchema=schema({"offset": {"type": "integer", "minimum": 0}}, ["offset"]), annotations=annotations, _meta=APP_META),
        Tool(name="probe_export_records", description="Save and verify a synthetic M0 ZIP in the configured local export directory; never overwrite files.",
             inputSchema=schema({"activationId": {"type": "string", "format": "uuid"},
                                 "uiInstanceId": {"type": "string", "format": "uuid"},
                                 "requestId": {"type": "string", "format": "uuid"}},
                                ["activationId", "uiInstanceId", "requestId"]),
             annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False), _meta=APP_META),
    ]


@server.list_resources()
async def list_resources():
    return [Resource(uri=uri, name="Codex Trace", mimeType="text/html;profile=mcp-app", _meta=UI_META)
            for uri in (URI, PANEL_URI)]


@server.read_resource()
async def read_resource(uri):
    """两个宿主入口复用离线诊断界面，资源 URI 使用固定白名单。"""
    if str(uri) not in (URI, PANEL_URI):
        raise ValueError("unknown-resource")
    path = Path(os.environ.get("CODEX_TRACE_UI_PATH", Path(__file__).parents[2] / "ui" / "trajectory-v1.html"))
    return [ReadResourceContents(content=path.read_text(encoding="utf-8"), mime_type="text/html;profile=mcp-app", meta=UI_META)]


def verify_thread(thread_id: str) -> dict:
    """只读核对显式选择的身份；冷读状态不能证明原任务仍在运行或已经完成。"""
    from .adapters.history import HistoryAdapter
    executable = os.environ.get("CODEX_TRACE_CODEX_EXECUTABLE")
    if not executable or not Path(executable).is_absolute():
        raise ValueError("app-server-executable-not-configured")
    with HistoryAdapter(executable) as history:
        metadata = history.read(thread_id)
    source = inspect_source(metadata.get("path"), thread_id,
                            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    result = {"threadId": metadata["id"], "identityMatches": metadata["id"] == thread_id,
              "title": metadata.get("name") or thread_id, "source": source,
              "status": "unknown", "statusReason": "cold-reader-status-is-not-execution-evidence"}
    service.audit("explicit-thread-check", {"threadId": thread_id, "identityMatches": result["identityMatches"], "source": source})
    return result


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict):
    """执行 M0 诊断协议；合成导出的路径信息单独保留在 app-only 元数据。"""
    try:
        # SDK 默认校验会回显非法值；在统一错误边界内校验同一份对外 schema。
        tool = next((definition for definition in await list_tools() if definition.name == name), None)
        if tool is None:
            raise ValueError("unknown-tool")
        jsonschema.validate(arguments, tool.inputSchema, format_checker=jsonschema.FormatChecker())
        if name in OPEN_TOOLS:
            meta = server.request_context.meta
            entrypoint, _ = OPEN_TOOLS[name]
            result = service.activate(meta.model_dump(by_alias=True, exclude_none=True) if meta else {},
                                      entrypoint=entrypoint)
        elif name == "probe_host_observation":
            result = service.observe(arguments["activationId"], arguments["uiInstanceId"], arguments["contextKeys"],
                                     arguments.get("displayMode"), arguments.get("hostCapabilityKeys"))
        elif name == "probe_diagnostic_event":
            result = service.diagnostic_event(arguments["activationId"], arguments["uiInstanceId"], arguments["phase"],
                                              code=arguments.get("code"), elapsed_ms=arguments.get("elapsedMs"),
                                              visibility=arguments.get("visibility"), context_keys=arguments.get("contextKeys"),
                                              host_capability_keys=arguments.get("hostCapabilityKeys"))
        elif name == "probe_local_download":
            result = service.download_chunk(arguments["offset"])
        elif name == "probe_export_records":
            result = await asyncio.to_thread(service.export_records, arguments["activationId"],
                                            arguments["uiInstanceId"], arguments["requestId"], export_store)
            # 即便是合成导出，实际保存路径和结果也只进入 UI 私有信封。
            return CallToolResult(content=[TextContent(type="text", text="Local export result is available in the app.")],
                                  _meta={"codex-trace/result": result})
        elif name == "probe_thread_identity":
            result = await asyncio.to_thread(verify_thread, arguments["threadId"])
        else:
            raise ValueError("unknown-tool")
        return CallToolResult(content=[TextContent(type="text", text="Local M0 probe result.")], structuredContent=result,
                              _meta={"ui": {"resourceUri": OPEN_TOOLS[name][1]}} if name in OPEN_TOOLS else None)
    except Exception as error:
        # 诊断错误仅暴露异常类型，不序列化异常正文中的命令、路径或任务文本。
        return CallToolResult(content=[TextContent(type="text", text=f"M0 probe failed ({type(error).__name__}).")], isError=True)


async def main():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
