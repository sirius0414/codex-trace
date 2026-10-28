"""Codex Display 正式 MCP 入口，由 ``python -m codex_display.app`` 启动。

本模块位于宿主与业务服务之间：向宿主登记工具和离线 UI 资源，把工具请求交给
TrajectoryService，并把正常结果或受控错误封装成前端可读取的 MCP 响应。
来源读取、完整 Binding 校验、轨迹投影和导出恢复由下游模块负责，不在这里实现。

一次正常工具调用依次经过：工具名白名单 → 请求 schema 校验 → 工作线程中的服务
调用 → 响应 schema 校验 → 私有结果封装。同步文件、数据库和历史 API 操作移到
工作线程，避免占用 MCP 的 asyncio 事件循环；共享对象仍需由各自的锁保护。

任务正文、来源路径和导出材料只能放在 ``_meta["codex-display/result"]``，由
frontend/src/bridge/client.ts 读取。content 仅含固定提示，structuredContent
不承载业务数据；工具可见性声明和实际响应信封都要遵守这一边界。底层异常也不能
直接回显，以免异常文案携带请求内容或本机路径。
"""
from __future__ import annotations

import asyncio
from base64 import b64encode
import json
import os
from pathlib import Path
import threading

from jsonschema import Draft202012Validator, FormatChecker
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Icon, Resource, TextContent, Tool, ToolAnnotations

from . import __version__
from .adapters.history import HistoryAdapter, HistoryError
from .adapters.raw import RawAdapter
from .probe import URI, PANEL_URI
from .service import TrajectoryService, ServiceError
from .storage import DerivedStore
from .export_store import ExportStore

# 源码与安装包都采用 <根目录>/backend/codex_display/app.py 布局，因此可用同一
# 相对关系定位契约、图标和 UI，不依赖宿主启动进程时的当前工作目录。
ROOT = Path(__file__).resolve().parents[2]
# schema 是数据形状的权威定义，tools.json 只映射工具名与输入／输出定义名称。
# 导入时读取随包文件；前端不能通过工具参数指定另一份契约或资源路径。
CONTRACT = json.loads((ROOT / "contracts/trajectory.schema.json").read_text())
TOOLS = json.loads((ROOT / "contracts/tools.json").read_text())["tools"]
# 两个入口共用 open 契约及服务方法，但保留不同宿主来源语义和资源 URI。
# global 要求用户选择任务；thread 可验证宿主候选身份，最终是否绑定由 service.open 决定。
OPEN = {"open_codex_display": ("global", URI), "open_codex_display_panel": ("thread", PANEL_URI)}
# 同时提供 ui 与 openai/widgetCSP 两套宿主元数据键。资源声明不允许远程连接或
# 远程资源加载；UI 所需 JS/CSS 已内联，业务请求经宿主 MCP 桥接而非浏览器直连。
UI_META = {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}},
           "openai/widgetCSP": {"connect_domains": [], "resource_domains": []}}
server = Server("codex-display", version=__version__)
# 一个 MCP 进程复用一个业务服务，页面级选择与加载范围由服务内部的 View 隔离。
# 首次工具调用才构造服务；仅列举工具／资源或读取离线 HTML 不启动业务服务。
_service = None
_creation_lock = threading.Lock()


class LazyHistory:
    """只读历史适配器的延迟代理，拥有本插件独立启动的 app-server 连接。

    构造代理时不启动子进程，第一次读取才创建 HistoryAdapter。多个请求和后台
    采集器可共用此代理；锁使连接创建、一次读取及故障清理按顺序执行，防止另一
    线程在旧连接尚未关闭时继续使用它。HistoryError 会向调用方传播，本次请求
    不自动重放；后续读取才尝试创建新连接，重试节奏由上层服务／采集器决定。
    """
    def __init__(self, executable):
        """保留运行配置中的 Codex 可执行路径，具体路径约束由 HistoryAdapter 检查。"""
        self.executable, self.adapter = executable, None
        # 读取失败时会在已持锁的 call() 内调用 close()，因此需要可重入锁。
        self.lock = threading.RLock()

    def __getattr__(self, method):
        """为允许的读取方法生成包装器，返回值与异常沿用底层适配器的语义。

        方法解析放在每次实际调用中，不能提前捕获旧 adapter 的绑定方法，否则
        重连后的调用仍可能落到已关闭连接。未列入白名单的属性直接拒绝。
        """
        # 代理只暴露读取白名单，不能借动态属性访问启动、停止或审批等任务操作。
        if method not in ("read", "read_history", "list_threads", "list_turns", "list_items"):
            raise AttributeError(method)
        def call(*args, **kwargs):
            with self.lock:
                try:
                    if self.adapter is None:
                        self.adapter = HistoryAdapter(self.executable)
                    return getattr(self.adapter, method)(*args, **kwargs)
                except HistoryError:
                    # 协议、超时或 I/O 故障后废弃连接，避免迟到响应污染下一次读取。
                    # 构造器失败时 adapter 仍为 None，也可沿同一路径清理后向上报错。
                    self.close()
                    raise
        return call

    def close(self):
        """释放已有观察连接；未创建或已关闭时不产生额外操作。

        这里只关闭插件自己的历史子进程，不中断用户任务。清空 adapter 后，若
        服务尚在运行，下一次允许的读取仍可重新建立连接。
        """
        with self.lock:
            if self.adapter is not None:
                self.adapter.close()
                self.adapter = None


def get_service():
    """在线程安全的首次调用中装配共享业务服务，后续调用复用同一实例。

    CODEX_HOME 决定只读任务来源，默认 ~/.codex；CODEX_DISPLAY_STATE_ROOT
    与 CODEX_DISPLAY_EXPORT_ROOT 覆盖派生存储及最终导出目录，未设置时由
    对应存储类使用默认目录。
    CODEX_DISPLAY_CODEX_EXECUTABLE 为必需的运行配置，由安装／开发启动器提供。
    这些配置不接受前端工具参数覆盖，避免把 UI 请求变成任意本地路径访问。

    构造成功后才给 _service 赋值；初始化异常交给 call_tool 统一封装。此函数
    会被 to_thread 调用，故首次构造也需用锁防止多个工作线程重复创建采集器。
    """
    global _service
    with _creation_lock:
        if _service is None:
            home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).absolute()
            state_root = os.environ.get("CODEX_DISPLAY_STATE_ROOT")
            export_root = os.environ.get("CODEX_DISPLAY_EXPORT_ROOT")
            history = LazyHistory(os.environ["CODEX_DISPLAY_CODEX_EXECUTABLE"])
            # 服务拥有这些依赖的生命周期：原始适配器只读两个来源根目录，SQLite
            # 保存派生数据／偏好／回执，ExportStore 负责固定目标目录的文件发布。
            # observe=True 开启共享 raw/API 后台采集，实际观察范围由活跃页面决定。
            _service = TrajectoryService(history, RawAdapter([home / "sessions", home / "archived_sessions"]),
                                         DerivedStore(Path(state_root) if state_root else None), observe=True,
                                         export_destination=ExportStore(Path(export_root) if export_root else None))
        return _service


def tool_schema(name):
    """把工具输入定义展开为独立 schema，供宿主声明和入口运行时校验复用。

    OPEN 中两个名称都映射到 open_codex_display 的输入定义。保留完整 $defs，
    使请求内部的 Binding 等 $ref 能解析；调用方须先保证工具名在白名单中。
    """
    definition = TOOLS["open_codex_display" if name in OPEN else name]["input"]
    return {"$defs": CONTRACT["$defs"], **CONTRACT["$defs"][definition]}


def entrypoint_icons():
    """返回宿主原生入口使用的 Tool.icons，图标内容来自随包 SVG。

    工具图标需要跟随 MCP 元数据传给宿主，因此编码成 data URI，不依赖宿主
    解析插件目录、访问本机文件路径或发起网络请求。普通读取工具不附带入口图标。
    """
    # 安装包将 plugin/ 展平到包根；开发源码仍把资源放在 plugin/assets/。
    assets = ROOT / "assets" if (ROOT / "assets").is_dir() else ROOT / "plugin/assets"
    data = b64encode((assets / "icon.svg").read_bytes()).decode("ascii")
    return [Icon(src="data:image/svg+xml;base64," + data, mimeType="image/svg+xml", sizes=["any"])]


@server.list_tools()
async def list_tools():
    """向宿主声明入口与 app-only 工具，不在列举时访问任务或启动服务。

    OPEN 补充聊天面板入口，TOOLS 提供其余契约工具；排除重名，避免全局 open
    重复注册。只有入口工具携带原生标题、图标、入口类型和关联的 UI 资源 URI。
    visibility 约束宿主如何暴露工具，annotations 描述调用语义；实际任务身份、
    来源和文件操作边界仍须由服务校验，不能把这些声明当成授权检查。
    """
    result = []
    icons = entrypoint_icons()
    for name in [*OPEN, *(name for name in TOOLS if name not in OPEN)]:
        # 同时声明 MCP Apps 的 app 可见性和宿主的 private 可见性，避免读取工具
        # 成为模型普通工具调用入口；结果中的敏感内容仍须独立限制在 _meta。
        metadata = {"ui": {"visibility": ["app"]}, "openai/visibility": "private"}
        if name in OPEN:
            surface, uri = OPEN[name]
            metadata["ui"]["resourceUri"] = uri
            metadata["openai/ui"] = {"entrypoints": [{"type": surface}]}
        # 只读描述针对被观察任务；偏好保存和 ZIP 导出会写插件本地文件，所以这
        # 两个工具的 readOnlyHint=False。即便普通读取也可能更新内部派生缓存。
        result.append(Tool(name=name, title="执行轨迹" if name in OPEN else None,
                           icons=icons if name in OPEN else None,
                           description="Local execution observation. Task data stays in app-only result metadata.",
                           inputSchema=tool_schema(name),
                           annotations=ToolAnnotations(readOnlyHint=name not in ("save_view_state", "download_records"),
                                                       destructiveHint=False, openWorldHint=False), _meta=metadata))
    return result


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    """处理全部正式工具调用，统一校验、分发和封装 app-only 结果。

    arguments 先按工具输入定义校验，成功返回值再按输出定义校验，防止前后端
    契约漂移后的数据进入 UI。schema 只证明形状；Binding、来源归属、版本和
    导出幂等语义由 TrajectoryService 及其下游检查。

    open 的身份候选只取本次宿主请求元数据；普通工具只接受契约中的参数。
    同步服务调用放到工作线程，不因此省略服务内部的锁和响应身份检查。
    try 内的异常统一转为私有错误；不会向模型可见字段返回异常或任务内容。
    """
    # 打开失败也保留资源关联，宿主／前端才能将错误归到本次打开的面板；普通
    # 读取结果不携带此标记，避免被前端误当成尚未绑定任务的初始化结果。
    resource_meta = {"ui": {"resourceUri": OPEN[name][1]}} if name in OPEN else {}
    try:
        if name not in OPEN and name not in TOOLS:
            raise ServiceError("notFound", "接口不存在。")
        # 禁用 SDK 默认校验后在 try 内自行处理；jsonschema 异常可能包含输入值，
        # 必须走下方固定错误封装。FormatChecker 执行 schema 中声明的格式约束。
        Draft202012Validator(tool_schema(name), format_checker=FormatChecker()).validate(arguments)
        service = await asyncio.to_thread(get_service)
        if name in OPEN:
            # 先在当前请求协程取出上下文，再把普通字典交给工作线程。候选 ID 还需
            # service.open 只读核验；全局入口即使带候选 ID 也不直接认定当前任务。
            meta = server.request_context.meta
            metadata = meta.model_dump(exclude_none=True) if meta else {}
            data = await asyncio.to_thread(service.open, metadata, OPEN[name][0])
        else:
            # name 已经过 TOOLS 白名单检查，动态分发仅覆盖契约中登记的接口，
            # 不能用任意属性名调用服务内部方法。
            data = await asyncio.to_thread(getattr(service, name), arguments)
        # 两个 open 入口也共享输出定义。业务方法即使正常返回，结果不符合契约
        # 仍应作为受控内部错误返回，不能把不完整或错误形状的数据交给客户端。
        definition = TOOLS["open_codex_display" if name in OPEN else name]["output"]
        Draft202012Validator({**CONTRACT, "$ref": f"#/$defs/{definition}"}).validate(data)
        # resource_meta 属于 MCP Apps 资源关联，codex-display/result 才是业务
        # 载荷；不要为了方便调试再把 data 复制到 content 或 structuredContent。
        return CallToolResult(content=[TextContent(type="text", text="Local observation result is available in the app.")],
                              _meta={**resource_meta, "codex-display/result": data})
    except Exception as error:
        # 只有 ServiceError 约定为可公开给 UI 的稳定错误码／文案。校验异常、
        # 文件错误或上游异常可能带正文和路径，统一替换为固定提示，不拼入 str(error)。
        code = error.code if isinstance(error, ServiceError) else "internalError"
        message = str(error) if isinstance(error, ServiceError) else "本地读取未完成，请重试；已有记录会保留。"
        # 请求可能尚未通过契约或身份校验，因此不从参数中猜测错误 Binding。
        # 前端按原请求上下文处理错误；retryable 也不授权用新键重做未决导出保存。
        data = {"viewSessionId": None, "binding": None, "code": code,
                "retryable": error.retryable if isinstance(error, ServiceError) else True, "message": message}
        return CallToolResult(content=[TextContent(type="text", text="Local observation request could not be completed.")],
                              isError=True, _meta={**resource_meta, "codex-display/result": data})


@server.list_resources()
async def list_resources():
    """列出两个固定的 MCP App HTML 资源，供宿主发现和加载。

    URI 区分全局与聊天面板，实际内容由 read_resource 提供。MIME profile
    告知宿主按 MCP App 加载；这里声明的资源级 CSP 在读取响应中也会再次附带。
    """
    return [Resource(uri=uri, name="执行轨迹", mimeType="text/html;profile=mcp-app", _meta=UI_META) for uri in (URI, PANEL_URI)]


@server.read_resource()
async def read_resource(uri):
    """将固定资源 URI 映射为离线 HTML；两个入口共用同一份界面内容。

    默认文件由 package_plugin.py 将构建后的 JS/CSS 内联生成。覆盖配置
    CODEX_DISPLAY_UI_PATH 仅来自进程环境，便于开发／验证注入资源；客户端
    URI 不参与本地路径拼接。
    未知 URI 直接拒绝，资源读取错误交给 MCP 资源协议处理，不经过工具结果封装。
    """
    if str(uri) not in (URI, PANEL_URI):
        raise ValueError("unknown-resource")
    path = Path(os.environ.get("CODEX_DISPLAY_UI_PATH", str(ROOT / "ui/trajectory-v1.html")))
    return [ReadResourceContents(content=path.read_text(), mime_type="text/html;profile=mcp-app", meta=UI_META)]


async def main():
    """在宿主提供的 stdin/stdout 上运行 MCP，并在退出时释放已创建的服务。

    stdio_server 管理传输通道，server.run 负责初始化和请求分发。stdout 是
    协议数据通道，不能插入普通 print 调试内容。finally 覆盖正常关闭和异常退出；
    若从未创建业务服务，则无需启动它来做清理。
    """
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if _service is not None:
            # service.close 统一停止自有采集线程、历史连接、导出管理器和存储。
            # 资源回收不等于撤销已保存 ZIP，也不向被观察的用户任务发送停止指令。
            _service.close()


if __name__ == "__main__":
    asyncio.run(main())
