"""为只读 Preview 提供来源格式与可追溯字段说明，不解析或改写正文。

协议类型决定结构提示；已知工具使用固定白名单。历史没有保存工具 schema 时，
随包提供的官方定义只作 reference，不声称与记录版本匹配，也不按字段名猜工具。
"""
from copy import deepcopy
import json

SHELL_SOURCE = 'https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs'
SHELL_VERSION = 'main · reviewed 2026-11-03 (historical schema not recorded)'
MCP_SOURCE = 'https://modelcontextprotocol.io/specification/2025-06-18/server/tools'


def _field(path, kind, zh, en, *, scope='tool', source=SHELL_SOURCE,
           version=SHELL_VERSION, required=None, unit=None, **extra):
    value = {'path': path, 'scope': scope, 'type': kind, 'description': zh,
             'descriptionEn': en, 'sourceUrl': source, 'sourceVersion': version,
             'versionMatch': 'reference', **extra}
    if required is not None:
        value['required'] = required
    if unit is not None:
        value['unit'] = unit
    return value


_EXEC_INPUT = [
    _field('/cmd', 'string', '交给 shell 的命令正文。', 'Command text passed to the shell.', required=True, format='code'),
    _field('/workdir', 'string', '本次命令的工作目录。', 'Working directory for this command.', required=False),
    _field('/shell', 'string', '用于本次调用的 shell 程序。', 'Shell program for this invocation.', required=False),
    _field('/tty', 'boolean', '是否分配伪终端。', 'Whether to allocate a pseudo-terminal.', required=False),
    _field('/login', 'boolean', '是否启用登录 shell 语义。', 'Whether to use login-shell semantics.', required=False),
    _field('/yield_time_ms', 'number', '返回输出前等待的时长；不表示命令总耗时上限。', 'Wait before returning output; not a command runtime limit.', required=False, unit='ms'),
    _field('/max_output_tokens', 'number', '本次返回输出的 token 预算。', 'Token budget for returned output.', required=False, unit='tokens'),
    _field('/sandbox_permissions', 'string', '本次命令请求的权限模式；可用值取决于版本与配置。', 'Requested permission mode; availability depends on version and configuration.', required=False,
           enum=['use_default', 'with_additional_permissions', 'require_escalated']),
    _field('/justification', 'string', '随权限请求提供的说明。', 'Explanation accompanying the permission request.', required=False),
    _field('/prefix_rule', 'string[]', '请求复用的命令批准前缀。', 'Requested reusable command approval prefix.', required=False),
]
_STDIN_INPUT = [
    _field('/session_id', 'number', '已有进程会话的标识。', 'Identifier of an existing process session.', required=True),
    _field('/chars', 'string', '写入标准输入的内容；空值用于轮询。', 'Standard-input text; an empty value polls output.', required=False, format='text'),
    _field('/yield_time_ms', 'number', '本次等待输出的时长。', 'Wait duration for this output request.', required=False, unit='ms'),
    _field('/max_output_tokens', 'number', '本次返回输出的 token 预算。', 'Token budget for returned output.', required=False, unit='tokens'),
]
_EXEC_OUTPUT = [
    _field('/output', 'string', '命令输出正文，来源可能已截断。', 'Command output text; the source may be truncated.', required=True, format='text'),
    _field('/wall_time_seconds', 'number', '本次等待读取输出经过的时间。', 'Elapsed time spent waiting for this output.', required=True, unit='s'),
    _field('/exit_code', 'number', '本次调用返回的进程退出码。', 'Process exit code reported by this invocation.', required=False),
    _field('/session_id', 'number', '仍在运行的进程会话标识。', 'Session identifier for the running process.', required=False),
    _field('/chunk_id', 'string', '本次返回片段的标识。', 'Identifier of the returned output chunk.', required=False),
    _field('/original_token_count', 'number', '输出截断前的估算 token 数。', 'Estimated token count before truncation.', required=False, unit='tokens'),
]


def tool_reader(name: str | None, part: str, *, structured=False, function=False, body=None) -> dict | None:
    """只匹配本地工具的完整已知名称；server.exec_command 等 MCP 名称不能冒充。"""
    # 后端此处拥有整个可读正文，可验证 JSON 而不把 UI 未加载分块当成格式错误。
    # 仅用于格式判断，显示和复制仍取原正文，不使用解析后的数值。
    if isinstance(body, str) and not structured and not function:
        try:
            def invalid_constant(value):
                raise ValueError(value)
            json.loads(body, parse_int=str, parse_float=str, parse_constant=invalid_constant)
            structured = True
        except (ValueError, RecursionError):
            pass
    origin = 'recorded function arguments' if function else 'recorded structured value' if structured else 'known tool identity; body format unspecified'
    if name in ('exec_command', 'functions.exec_command'):
        return {'format': 'json' if structured or function else 'text', 'origin': origin,
                'adapter': 'command', 'fields': deepcopy(_EXEC_INPUT if part == 'input' else _EXEC_OUTPUT)}
    if name in ('write_stdin', 'functions.write_stdin'):
        return {'format': 'json' if structured or function else 'text', 'origin': origin,
                'adapter': 'function', 'fields': deepcopy(_STDIN_INPUT if part == 'input' else _EXEC_OUTPUT)}
    if name in ('apply_patch', 'functions.apply_patch') and part == 'input':
        return {'format': 'json' if structured or function else 'diff', 'origin': 'recorded apply_patch input', 'adapter': 'patch'}
    if structured or function:
        return {'format': 'json', 'origin': origin, 'adapter': 'function'}
    return None


def item_readers(kind: str, item: dict, tool: str | None) -> dict:
    """以已确认的 item 类型生成每块提示，命令参数数组始终保留为 JSON。"""
    result = {}
    if kind == 'commandExecution':
        command = item.get('command')
        result['input'] = {'format': 'json' if isinstance(command, (dict, list)) else 'code',
                           'origin': 'commandExecution.command', 'adapter': 'command'}
        # 原始记录没有 shell 类型时保留通用代码，不能把命令都视为 Bash。
        auxiliary = [{'name': key, 'value': str(item[key])} for key in ('cwd', 'exitCode')
                     if key in item and isinstance(item[key], (str, int)) and not isinstance(item[key], bool)]
        if auxiliary:
            result['input']['auxiliary'] = auxiliary
        result['output'] = {'format': 'text', 'origin': 'commandExecution.aggregatedOutput'}
    elif kind == 'fileChange':
        result['input'] = {'format': 'json', 'origin': 'fileChange.changes', 'adapter': 'patch'}
        changes = item.get('changes')
        # 具体 fileChange 的 diff 字段来自该来源类型；同名业务字段不会获得此提示。
        if isinstance(changes, list):
            result['input']['fields'] = [
                _field(f'/{index}/diff', 'string', '该文件已记录的差异正文。', 'Recorded diff text for this file.',
                       scope='protocol', source='https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartResponse.json',
                       version='main · reviewed 2026-11-03 (historical schema not recorded)',
                       format='diff' if isinstance(change.get('kind'), dict) and change['kind'].get('type') == 'update' else 'code')
                for index, change in enumerate(changes[:256]) if isinstance(change, dict) and isinstance(change.get('diff'), str)]
    elif kind in ('mcpToolCall', 'dynamicToolCall'):
        arguments = item.get('arguments')
        hint = ({'format': 'json', 'origin': 'mcpToolCall.arguments', 'adapter': 'function'}
                if kind == 'mcpToolCall' and isinstance(arguments, (dict, list)) else None)
        if kind == 'dynamicToolCall':
            hint = tool_reader(tool, 'input', structured=isinstance(arguments, (dict, list)), function=True)
        if hint:
            result['input'] = hint
        output = item.get('result') if kind == 'mcpToolCall' else item.get('contentItems')
        if isinstance(output, (dict, list)):
            result['output'] = {'format': 'json', 'origin': f'{kind}.result'}
            if kind == 'mcpToolCall' and isinstance(output, dict):
                result['output']['fields'] = mcp_result_fields(output)
    return result


def mcp_result_fields(value: dict) -> list[dict]:
    """只说明 MCP 外层字段，绝不把协议说明套用到工具的业务参数。"""
    def field(path, kind, zh, en, **extra):
        return _field(path, kind, zh, en, scope='protocol', source=MCP_SOURCE,
                      version='MCP 2025-06-18 (recorded protocol version unknown)', **extra)
    fields = [field('/content', 'ContentBlock[]', 'MCP 返回的内容块。', 'Content blocks returned by MCP.'),
              field('/isError', 'boolean', '工具结果是否报告执行错误。', 'Whether the tool result reports an execution error.'),
              field('/structuredContent', 'object', '工具返回的结构化数据；其业务字段由工具定义。', 'Structured result data; its application fields are defined by the tool.')]
    content = value.get('content')
    if isinstance(content, list):
        for index, block in enumerate(content[:250]):
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                fields.append(field(f'/content/{index}/text', 'string', 'MCP 文本块的正文。', 'Body of an MCP text content block.', format='text'))
    return fields
