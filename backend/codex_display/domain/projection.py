"""Versioned persisted-record projection; this module performs no I/O.

API pages are passed in canonical ascending order. The reader reverses descending
pages before calling this function. Raw ordinals are logical source positions,
not wall-clock ordering. Only exact identity can attach a raw record to an API item.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import hashlib
import json
import math
from typing import Any, Iterable

from .lifecycle import turn_states, thread_explanation
from .usage import usage_snapshots
from .relationships import project_relationships, resolve_relationships

PARSER_VERSION = '1.1.0'
_PREVIEW_LENGTH = 280
_ITEM_TYPES = {
    'UserMessage': 'userMessage', 'AgentMessage': 'agentMessage',
    'Reasoning': 'reasoning', 'CommandExecution': 'commandExecution',
    'McpToolCall': 'mcpToolCall', 'DynamicToolCall': 'dynamicToolCall',
    'FileChange': 'fileChange', 'ContextCompaction': 'contextCompaction',
    'CollabAgentToolCall': 'collabAgentToolCall', 'FunctionCallOutput': 'functionCallOutput',
    'HookPrompt': 'hookPrompt', 'SubAgentActivity': 'subAgentActivity',
}
_LABELS = {'userMessage': '用户输入', 'assistantMessage': '助手输出',
           'reasoningSummary': '思考摘要', 'systemContext': '系统说明',
           'developerContext': '开发者说明', 'environment': '运行环境',
           'toolCall': '工具调用', 'toolResult': '工具结果',
           'compaction': '上下文压缩', 'collaboration': '协作操作'}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _identity(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:32]


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 9007199254740991 else None


def _timestamp(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return round(parsed.timestamp() * 1000) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def unknown_status(explanation: str = '没有可靠的任务执行状态记录。') -> dict:
    return {'value': 'unknown', 'evidence': [], 'lastKnown': None, 'explanation': explanation}


def _status(value: Any, scope: str, scope_id: str, source: dict, observed: int | float | None) -> dict:
    # Cold "inProgress" and "interrupted" are not evidence of desktop execution.
    normalized = {'completed': 'completed', 'failed': 'failed', 'declined': 'stopped'}.get(value) if isinstance(value, str) else None
    if normalized is None:
        return unknown_status('独立历史读取的非终态或中断状态不能证明当前执行状态。')
    explanation = '该操作的持久化结果状态；不代表整个任务结束。'
    return {'value': normalized, 'evidence': [{'scope': scope, 'scopeId': scope_id,
            'status': normalized, 'basis': 'persistedLifecycle', 'observedAt': observed,
            'source': source, 'explanation': explanation}], 'lastKnown': normalized, 'explanation': explanation}


def _timing(start=None, end=None, duration=None, source=None, basis='时间未记录') -> dict:
    start, end, duration = _number(start), _number(end), _number(duration)
    # The public contract uses integer milliseconds. Rust Duration carries ns;
    # round explicitly at this adapter boundary, preserving a real sub-ms zero.
    start = round(start) if start is not None else None
    end = round(end) if end is not None else None
    duration = round(duration) if duration is not None else None
    if start is not None and end is not None and end < start:
        end = None
    if duration is None and start is not None and end is not None:
        duration = end - start
    # An end timestamp is not a recorded start. Keep that evidence on status /
    # raw details; the timeline contract has no end-only interval variant.
    if start is None:
        end = None
    precision = 'exact' if start is not None and (end is not None or duration is not None) else 'startOnly' if start is not None else 'durationOnly' if duration is not None else 'orderOnly'
    return {'startMs': start, 'endMs': end, 'durationMs': duration, 'precision': precision,
            'originalUnit': 'milliseconds' if precision != 'orderOnly' else 'notRecorded',
            'basis': basis + ('；亚毫秒值舍入到整数毫秒' if precision != 'orderOnly' else ''), 'source': source if precision != 'orderOnly' else None}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get('type') in ('text', 'input_text', 'output_text', 'summary_text', 'Text') and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return '\n'.join(parts)
    return ''


def _value(value: Any) -> str:
    return value if isinstance(value, str) else _json(value)


def _content(part: str, text: str, completeness: str = 'unknown') -> dict:
    size = len(text.encode('utf-8'))
    return {'part': part, 'loaded': False, 'sourceTruncated': False, 'chunked': size > 65536,
            'readableBytes': size, 'chunkCursor': None, 'completeness': completeness}


def _event(thread_id: str, event_id: str, turn_id: str | None, kind: str, parts: dict,
           source: dict, order: int, *, role=None, tool=None, timing=None, status=None,
           call_key=None, completeness='unknown', section=None) -> tuple[dict, dict]:
    parts = {part: text for part, text in parts.items() if isinstance(text, str)}
    summary = _LABELS[kind] + (f' · {tool}' if tool else '')
    parts.setdefault('summary', summary)
    parts.setdefault('source', _json([source]))
    preview = parts.get('input') or parts.get('output') or summary
    preview = ' '.join(preview.split())[:_PREVIEW_LENGTH]
    return {'eventId': event_id, 'threadId': thread_id, 'turnId': turn_id,
            'groupId': f'main:{thread_id}', 'parentEventId': None, 'kind': kind,
            'role': role, 'toolName': tool, 'preview': preview,
            'order': {'section': section or ('timeline' if turn_id else 'context'), 'ordinal': order,
                      'basis': 'officialOrder' if source['kind'] == 'api' else 'logicalSourcePosition'},
            'status': status or unknown_status('此类记录不提供操作执行状态。'),
            'timing': timing or _timing(),
            'content': [_content(part, text, completeness if part == 'output' else 'unknown') for part, text in parts.items()],
            'sources': [source], 'eventRevision': 1, 'callKey': call_key, 'modelRequestKey': None}, parts


def _normalize_raw_item(item: dict) -> dict:
    result = dict(item)
    kind = _ITEM_TYPES.get(item.get('type')) if isinstance(item.get('type'), str) else None
    if kind is None:
        return result
    result['type'] = kind
    mapping = {'aggregated_output': 'aggregatedOutput', 'exit_code': 'exitCode',
               'content_items': 'contentItems', 'sender_thread_id': 'senderThreadId',
               'receiver_thread_ids': 'receiverThreadIds', 'agents_states': 'agentsStates',
               'agent_thread_id': 'agentThreadId', 'agent_path': 'agentPath'}
    for old, new in mapping.items():
        if old in item:
            result[new] = item[old]
    if kind == 'agentMessage':
        result['text'] = _text(item.get('content'))
    if kind == 'reasoning':
        result['summary'] = item.get('summary_text', [])
    duration = item.get('duration')
    # Rust std::time::Duration serde serializes explicit seconds/nanoseconds.
    if isinstance(duration, dict) and _number(duration.get('secs')) is not None and _number(duration.get('nanos')) is not None:
        result['durationMs'] = duration['secs'] * 1000 + duration['nanos'] / 1_000_000
    if kind == 'commandExecution' and isinstance(item.get('command'), list):
        result['command'] = _json(item['command'])
    return result


def _item_event(thread_id: str, wrapper: dict, ordinal: int, source: dict) -> tuple[dict, dict] | None:
    item = wrapper.get('item')
    turn_id = _str(wrapper.get('turnId'))
    if not isinstance(item, dict) or not _str(item.get('id')) or turn_id is None:
        return None
    kind = item.get('type')
    event_id = 'event:' + _identity('item', thread_id, turn_id, item['id'])
    timing = _timing(wrapper.get('startedAtMs'), wrapper.get('completedAtMs'), item.get('durationMs'), source,
                     'item 包装层毫秒时间及明确持续时间')
    observed_end = _number(wrapper.get('completedAtMs'))
    observed_end = round(observed_end) if observed_end is not None else None
    status = _status(item.get('status'), 'event', event_id, source, observed_end)
    parts, role, tool, call_key, completeness = {}, None, None, None, 'unknown'
    if kind == 'userMessage':
        event_kind, role, parts = 'userMessage', 'user', {'input': _text(item.get('content'))}
    elif kind == 'agentMessage':
        event_kind, role, parts = 'assistantMessage', 'assistant', {'output': _text(item.get('text'))}
    elif kind == 'reasoning':
        summary = _text(item.get('summary'))
        if not summary:
            return None  # Encrypted/private reasoning is never reconstructed.
        event_kind, role, parts = 'reasoningSummary', 'assistant', {'output': summary}
    elif kind == 'hookPrompt':
        fragments = item.get('fragments')
        text = '\n'.join(f['text'] for f in fragments if isinstance(f, dict) and isinstance(f.get('text'), str)) if isinstance(fragments, list) else ''
        event_kind, role, parts = 'developerContext', 'developer', {'input': text}
    elif kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange', 'collabAgentToolCall'):
        event_kind, role = ('collaboration' if kind == 'collabAgentToolCall' else 'toolCall'), 'tool'
        tool = {'commandExecution': 'command', 'fileChange': 'apply_patch'}.get(kind) or _str(item.get('tool')) or kind
        if kind == 'mcpToolCall' and _str(item.get('server')):
            tool = item['server'] + '.' + tool
        input_field = {'commandExecution': 'command', 'fileChange': 'changes', 'collabAgentToolCall': 'prompt'}.get(kind, 'arguments')
        if item.get(input_field) is not None:
            parts['input'] = _value(item[input_field])
        output_field = {'commandExecution': 'aggregatedOutput', 'mcpToolCall': 'result', 'dynamicToolCall': 'contentItems', 'collabAgentToolCall': 'agentsStates'}.get(kind)
        if output_field and item.get(output_field) is not None:
            parts['output'] = _value(item[output_field])
            # These persisted result fields prove readable bytes, not absence of
            # upstream truncation. Operation terminality is a separate property.
            completeness = 'fragment' if item.get('status') == 'inProgress' else 'unknown'
        else:
            parts['output'] = ''
            completeness = 'notRecorded'
        if item.get('error') is not None:
            parts['error'] = _value(item['error'])
        exit_code = item.get('exitCode')
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0 or item.get('success') is False:
            status = _status('failed', 'event', event_id, source, observed_end)
        call_key = 'item:' + _identity(thread_id, turn_id, item['id'])
    elif kind == 'subAgentActivity':
        activity = item.get('kind')
        child = _str(item.get('agentThreadId'))
        if activity not in ('started', 'interacted', 'interrupted', 'completed') or child is None:
            return None
        event_kind, role = 'collaboration', 'tool'
        labels = {'started': '创建子代理', 'interacted': '子代理交互', 'interrupted': '子代理中断', 'completed': '子代理本轮完成'}
        tool = 'spawn_agent' if activity == 'started' else labels[activity]
        parts = {'input': labels[activity] + ' · ' + (_str(item.get('agentPath')) or child)}
        if activity == 'started':
            call_key = 'item:' + _identity(thread_id, turn_id, item['id'])
    elif kind == 'functionCallOutput':
        event_kind, role, tool = 'toolResult', 'tool', _str(item.get('name'))
        parts = {'output': _value(item.get('output'))}
        # An output's id is not documented as its owning call id.
    elif kind == 'contextCompaction':
        event_kind, parts = 'compaction', {'output': '摘要未记录'}
    else:
        return None
    return _event(thread_id, event_id, turn_id, event_kind, parts, source, ordinal, role=role, tool=tool,
                  timing=timing, status=status, call_key=call_key, completeness=completeness)


def _raw_source(record: dict, association='physicalRecord') -> dict:
    # SourceRef is a flat validated contract of scalar fields, not raw payload.
    source = dict(record['source'])
    source['association'] = association
    return source


def _raw_key(thread_id: str, record: dict, suffix: str = '') -> str:
    source = record['source']
    return 'event:' + _identity('raw', thread_id, source['segmentId'], source['generation'], source['byteStart'], suffix)


def _attach(event: dict, parts: dict, record: dict, association='exactItemId') -> None:
    source = _raw_source(record, association)
    is_new = source not in event['sources']
    if is_new:
        event['sources'].append(source)
    if is_new or 'raw' not in parts:
        raw_text = record.get('raw', b'').decode('utf-8', errors='replace')
        parts['raw'] = parts.get('raw', '') + raw_text
    if is_new or 'source' not in parts:
        parts['source'] = _json(event['sources'])


def statistics(events: Iterable[dict]) -> dict:
    events = list(events)
    tools = {event['callKey'] for event in events if event.get('callKey')}
    starts, ends, durations = [], [], []
    categories = {'input': [], 'model': [], 'tool': []}
    for event in events:
        timing = event['timing']
        start, end, duration = timing['startMs'], timing['endMs'], timing['durationMs']
        if start is not None:
            starts.append(start)
            ends.append(end if end is not None else start)
        if duration is not None:
            durations.append(duration)
        if start is not None and duration is not None:
            category = 'tool' if event['kind'] in ('toolCall', 'collaboration') else 'input' if event['kind'] == 'userMessage' else 'model'
            categories[category].append((start, end if end is not None else start + duration))
    def union(intervals):
        if not intervals:
            return None
        intervals = sorted(intervals)
        total, left, right = 0, *intervals[0]
        for start, end in intervals[1:]:
            if start <= right:
                right = max(right, end)
            else:
                total += right-left
                left, right = start, end
        return total + right-left
    return {'toolCalls': {'value': len(tools), 'scope': 'loaded', 'explanation': '已加载的稳定工具操作键，输出片段不重复计数。'},
            'modelRequests': {'value': None, 'scope': 'notRecorded', 'explanation': '模型请求的完整身份与覆盖范围未记录。'},
            'executionWallClockMs': None, 'displayedRangeMs': max(ends)-min(starts) if starts else None,
            'operationCumulativeMs': sum(durations) if durations else None,
            'inputUnionMs': union(categories['input']), 'modelUnionMs': union(categories['model']),
            'toolUnionMs': union(categories['tool']), 'explanation': '仅已加载记录；并行操作累计时长不代表任务墙钟时长。'}


def project(thread_id: str, records: Iterable[dict], api_items: Iterable[dict] = (), api_turns: Iterable[dict] = (), mode: str = 'api') -> dict:
    """Project known records. Inputs must belong to an identity-checked source catalog.

    API items/turns are in canonical ascending order; raw records carry canonical
    logical ordinals. No cross-source correspondence is inferred from text/time.
    """
    if mode not in ('api', 'raw'):
        raise ValueError('unsupported projection mode')
    all_records = sorted((r for r in records if isinstance(r, dict) and isinstance(r.get('record'), dict)), key=lambda r: r['ordinal'])
    records = [r for r in all_records if not r.get('projectionExcluded')]
    api_items, api_turns = list(api_items), list(api_turns)
    events: dict[str, dict] = {}
    details: dict[str, dict] = {}
    official_order: dict[str, int] = {}
    raw_positions: dict[str, int] = {}
    raw_calls: dict[tuple, list[str]] = {}
    raw_results: list[tuple] = []
    turn_order: dict[str, int] = {}
    raw_turn_order: dict[str, int] = {}
    def add(pair):
        event, parts = pair
        event_id = event['eventId']
        events[event_id], details[event_id] = event, parts
        return event_id
    for ordinal, wrapped_turn in enumerate(api_turns):
        turn = wrapped_turn.get('turn', wrapped_turn)
        if _str(turn.get('id')):
            turn_order[turn['id']] = ordinal
    if mode == 'api':
        for ordinal, wrapper in enumerate(api_items):
            item = wrapper.get('item', {})
            source = {'kind': 'api', 'threadId': thread_id, 'turnId': wrapper.get('turnId'),
                      'itemId': item.get('id'), 'rawType': item.get('type'), 'producerVersion': None, 'association': 'exactItemId'}
            pair = _item_event(thread_id, wrapper, ordinal, source)
            if pair:
                event_id = add(pair)
                official_order[event_id] = ordinal
    persisted_items = any(r['record'].get('type') == 'event_msg' and isinstance(r['record'].get('payload'), dict) and r['record']['payload'].get('type') == 'item_completed' for r in records)
    persisted_compactions = any(e['kind'] == 'compaction' for e in events.values()) or any(r['record'].get('type') == 'event_msg' and isinstance(r['record'].get('payload'), dict) and isinstance(r['record']['payload'].get('item'), dict) and r['record']['payload']['item'].get('type') == 'ContextCompaction' for r in records)
    active_turn = None
    for row in records:
        record, ordinal = row.get('record'), row['ordinal']
        if not isinstance(record, dict) or not isinstance(row.get('source'), dict):
            continue
        payload = record.get('payload', {})
        if not isinstance(payload, dict):
            continue
        outer, subtype = record.get('type'), payload.get('type')
        owner = payload.get('thread_id')
        if owner is not None and owner != thread_id and not row.get('inherited'):
            continue
        explicit_turn = _str(payload.get('turn_id'))
        if explicit_turn and (outer == 'turn_context' or outer == 'event_msg' and subtype in (
                'task_started', 'turn_started', 'task_complete', 'turn_complete', 'turn_aborted', 'item_completed')):
            raw_turn_order.setdefault(explicit_turn, ordinal)
        if outer == 'event_msg' and subtype in ('task_started', 'turn_started'):
            active_turn = _str(payload.get('turn_id'))
            if active_turn and active_turn not in turn_order:
                turn_order[active_turn] = len(turn_order)
            continue
        if outer == 'turn_context' and _str(payload.get('turn_id')):
            active_turn = payload['turn_id']
        turn_id = _str(payload.get('turn_id')) or active_turn
        source = _raw_source(row)
        timing = None if outer == 'event_msg' and subtype in ('item_completed', 'sub_agent_activity', 'collab_agent_spawn_end') else _timing(_timestamp(record.get('timestamp')), source=source, basis='记录保存时间；不推测持续时长')
        pair = None
        if outer == 'event_msg' and subtype == 'item_completed':
            if not _str(payload.get('turn_id')) or not isinstance(payload.get('item'), dict):
                continue
            item = _normalize_raw_item(payload['item'])
            source['association'] = 'exactItemId'
            wrapper = {'turnId': payload['turn_id'], 'item': item, 'startedAtMs': payload.get('started_at_ms'), 'completedAtMs': payload.get('completed_at_ms')}
            pair = _item_event(thread_id, wrapper, ordinal, source)
            if pair:
                event_id = pair[0]['eventId']
                raw_positions[event_id] = ordinal
                if event_id in events:
                    previous = events[event_id]
                    previous_end = previous['timing']['endMs']
                    raw_end = pair[0]['timing']['endMs']
                    raw_terminal = pair[0]['status']['value'] in ('completed', 'failed', 'stopped')
                    prior_terminal = previous['status']['value'] in ('completed', 'failed', 'stopped')
                    # Persisted completion can arrive before history has projected it.
                    # A stale API item must not erase that newer, same-ID completion.
                    if raw_terminal and (not prior_terminal or (raw_end is not None and previous_end is not None and raw_end > previous_end)):
                        old_sources = previous['sources']
                        old_raw = details[event_id].get('raw')
                        add(pair)
                        events[event_id]['sources'] = old_sources
                        if old_raw is not None:
                            details[event_id]['raw'] = old_raw
                    elif previous['timing']['precision'] == 'orderOnly' and pair[0]['timing']['precision'] != 'orderOnly':
                        previous['timing'] = pair[0]['timing']
                    _attach(events[event_id], details[event_id], row)
                    continue
        elif outer == 'event_msg' and subtype in ('sub_agent_activity', 'collab_agent_spawn_end'):
            # These public records have explicit IDs. They can share an exact
            # item identity with API history, without matching text or timestamps.
            item = None
            if subtype == 'sub_agent_activity' and _str(payload.get('event_id')):
                item = _normalize_raw_item({**payload, 'type': 'SubAgentActivity', 'id': payload['event_id']})
            elif subtype == 'collab_agent_spawn_end' and _str(payload.get('call_id')):
                child = _str(payload.get('new_thread_id'))
                item = {'id': payload['call_id'], 'type': 'collabAgentToolCall', 'tool': 'spawn_agent',
                        'status': 'completed' if child else 'failed', 'senderThreadId': payload.get('sender_thread_id'),
                        'receiverThreadIds': [child] if child else [], 'prompt': payload.get('prompt'),
                        'agentsStates': {child: payload.get('status')} if child else {}}
            if item is not None and turn_id is not None:
                source['association'] = 'exactItemId'
                pair = _item_event(thread_id, {'turnId': turn_id, 'item': item,
                    'startedAtMs': payload.get('occurred_at_ms') if subtype == 'sub_agent_activity' else None,
                    'completedAtMs': payload.get('completed_at_ms', payload.get('occurred_at_ms'))}, ordinal, source)
                if pair and pair[0]['eventId'] in events:
                    event_id = pair[0]['eventId']
                    _attach(events[event_id], details[event_id], row)
                    raw_positions[event_id] = ordinal
                    continue
        elif outer == 'session_meta':
            # Session instructions are a recorded snapshot, not proof that a skill executed.
            base = payload.get('base_instructions')
            base_text = base.get('text') if isinstance(base, dict) else None
            if isinstance(base_text, str):
                pair = _event(thread_id, _raw_key(thread_id, row, 'instructions'), None, 'systemContext', {'input': base_text}, source, ordinal, role='system', timing=timing)
                event_id = add(pair)
                _attach(events[event_id], details[event_id], row, 'explicitMetadata')
                raw_positions[event_id] = ordinal
            environment = {k: payload[k] for k in ('cwd', 'runtime_workspace_roots', 'cli_version', 'model_provider') if k in payload}
            pair = _event(thread_id, _raw_key(thread_id, row, 'environment'), None, 'environment', {'input': _json(environment)}, source, ordinal, timing=timing) if environment else None
        elif outer == 'turn_context':
            environment = {k: payload[k] for k in ('cwd', 'model', 'effort', 'approval_policy', 'sandbox_policy', 'permission_profile', 'runtime_workspace_roots') if k in payload}
            pair = _event(thread_id, _raw_key(thread_id, row), turn_id, 'environment', {'input': _json(environment)}, source, ordinal, timing=timing) if environment else None
        elif outer == 'response_item':
            if subtype == 'message':
                role = payload.get('role')
                if role in ('system', 'developer') or mode == 'raw' and not persisted_items and role in ('user', 'assistant'):
                    kind = {'system': 'systemContext', 'developer': 'developerContext', 'user': 'userMessage', 'assistant': 'assistantMessage'}[role]
                    part = 'output' if role == 'assistant' else 'input'
                    pair = _event(thread_id, _raw_key(thread_id, row), turn_id, kind, {part: _text(payload.get('content'))}, source, ordinal, role=role, timing=timing)
            elif subtype == 'reasoning' and mode == 'raw' and not persisted_items:
                summary = _text(payload.get('summary'))
                if summary:
                    pair = _event(thread_id, _raw_key(thread_id, row), turn_id, 'reasoningSummary', {'output': summary}, source, ordinal, role='assistant', timing=timing)
            elif subtype in ('custom_tool_call', 'custom_tool_call_output') or mode == 'raw' and not persisted_items and subtype in ('function_call', 'function_call_output'):
                call_id = _str(payload.get('call_id'))
                is_output = subtype.endswith('_output')
                call_key = (turn_id, call_id) if turn_id and call_id else None
                if is_output:
                    raw_results.append((row, turn_id, call_key, timing))
                    continue
                event_id = 'event:' + _identity('call', thread_id, turn_id, call_id) if call_key else _raw_key(thread_id, row)
                # Duplicate input records are ambiguous unless they are literally the same source.
                if event_id in events:
                    event_id = _raw_key(thread_id, row)
                pair = _event(thread_id, event_id, turn_id, 'toolCall', {'input': _value(payload.get('input', payload.get('arguments', ''))), 'output': ''}, source, ordinal,
                              role='tool', tool=_str(payload.get('name')), timing=timing,
                              call_key='call:' + _identity(thread_id, turn_id, call_id) if call_key else event_id,
                              completeness='notRecorded')
                if call_key:
                    raw_calls.setdefault(call_key, []).append(event_id)
            # Ordinary legacy function calls are omitted in API mode, avoiding duplicate inner tools.
        elif outer == 'compacted' and mode == 'raw' and not persisted_compactions:
            pair = _event(thread_id, _raw_key(thread_id, row), turn_id, 'compaction', {'output': payload.get('message') if isinstance(payload.get('message'), str) else '摘要未记录'}, source, ordinal, timing=timing)
        if pair:
            event_id = add(pair)
            _attach(events[event_id], details[event_id], row, source['association'])
            raw_positions[event_id] = ordinal
        if outer == 'event_msg' and subtype in ('task_complete', 'turn_complete', 'turn_aborted'):
            active_turn = None
    for row, turn_id, key, timing in raw_results:
        candidates = raw_calls.get(key, []) if key else []
        payload = row['record']['payload']
        text = _value(payload.get('output', ''))
        if len(candidates) == 1:
            event_id = candidates[0]
            event, parts = events[event_id], details[event_id]
            parts['output'] = parts.get('output', '') + text
            _attach(event, parts, row, 'exactCallId')
            # A returned custom-tool fragment does not prove the nested operation completed.
            event['content'] = [_content(part, value, 'fragment' if part == 'output' else 'unknown') for part, value in parts.items()]
        else:
            source = _raw_source(row)
            pair = _event(thread_id, _raw_key(thread_id, row), turn_id, 'toolResult', {'output': text}, source, row['ordinal'], role='tool', timing=timing, completeness='fragment')
            event_id = add(pair)
            _attach(events[event_id], details[event_id], row)
            raw_positions[event_id] = row['ordinal']
    # Official items anchor supplemental raw records. Without a comparable anchor,
    # keep the supplement in the separate context section instead of guessing.
    anchors = sorted((raw_positions[eid], ordinal) for eid, ordinal in official_order.items() if eid in raw_positions)
    anchor_positions = [position for position, _ in anchors]
    final_anchor_position = anchor_positions[-1] if anchor_positions else 0
    def sort_key(event):
        eid = event['eventId']
        if mode == 'raw':
            return (0 if event['order']['section'] == 'context' else 1, raw_positions.get(eid, 0), 0)
        if eid in official_order:
            return (1, official_order[eid], 1)
        position = raw_positions.get(eid, 0)
        if event['turnId'] is None:
            return (0, position, 0)
        index = bisect_right(anchor_positions, position)
        if anchors:
            anchor_order = anchors[index-1][1] if index else anchors[0][1] - 1
            return (1, anchor_order, 2 + position / (1 + max(final_anchor_position, position)))
        event['order']['section'] = 'context'
        event['order']['basis'] = 'positionNotRecorded'
        return (0, position, 1)
    ordered = sorted(events.values(), key=sort_key)
    for ordinal, event in enumerate(ordered):
        event['order']['ordinal'] = ordinal
        if event['turnId'] and event['turnId'] not in turn_order:
            turn_order[event['turnId']] = len(turn_order)
        parts = details[event['eventId']]
        prior = {p['part']: p for p in event['content']}
        # Only source/raw attachment changes these parts after event creation.
        # Returned tool fragments already rebuild their output metadata above.
        event['content'] = [prior[part] if part in prior and part not in ('source', 'raw')
                            else _content(part, value, prior.get(part, {}).get('completeness', 'unknown'))
                            for part, value in parts.items()]
    lifecycle = turn_states(thread_id, all_records)
    for turn_id in lifecycle:
        if turn_id not in turn_order:
            turn_order[turn_id] = len(turn_order)
    # Raw first appearance is a logical sequence, independent of wall-clock time.
    # API-only turns retain official order; raw-only turns are inserted around
    # their exact shared turn IDs rather than appended after a later API page.
    official_turns, official_turn_set = [], set()
    for wrapped_turn in api_turns:
        turn = wrapped_turn.get('turn', wrapped_turn)
        if _str(turn.get('id')) and turn['id'] not in official_turn_set:
            official_turns.append(turn['id'])
            official_turn_set.add(turn['id'])
    for event in ordered:
        if event['eventId'] in official_order and event['turnId'] not in official_turn_set:
            official_turns.append(event['turnId'])
            official_turn_set.add(event['turnId'])
    # Accumulate insertions before each exact official anchor, then flatten once.
    # Repeated list.index/insertion made many-turn histories quadratic.
    insertions, pending = {}, []
    for turn_id in raw_turn_order:
        if turn_id in official_turn_set:
            insertions[turn_id] = pending
            pending = []
        elif turn_id in turn_order:
            pending.append(turn_id)
    turn_ids = []
    for turn_id in official_turns:
        turn_ids.extend(insertions.get(turn_id, ()))
        turn_ids.append(turn_id)
    turn_ids.extend(pending)
    included_turns = set(turn_ids)
    turn_ids.extend(tid for tid in turn_order if tid not in included_turns)
    turn_order = {tid: index for index, tid in enumerate(turn_ids)}
    turns = [{'threadId': thread_id, 'turnId': tid, 'order': order, 'status': lifecycle.get(tid, unknown_status('冷读取轮次状态不能独自证明实际执行状态。'))} for tid, order in turn_order.items()]
    usage = usage_snapshots(thread_id, all_records)
    child_groups, relationships, relationship_issues = project_relationships(thread_id, all_records, api_items, ordered, mode)
    state = unknown_status(thread_explanation(turns))
    return {'events': ordered, 'turns': turns, 'details': details,
            'groups': [{'groupId': f'main:{thread_id}', 'kind': 'main', 'threadId': thread_id, 'name': '主 Agent',
                        'parentGroupId': None, 'creationEventId': None, 'relationshipId': None, 'status': state,
                        'loadedEventCount': len(ordered), 'loadedToolCallCount': len({e['callKey'] for e in ordered if e['callKey']}),
                        'usage': usage, 'readIssue': None}, *child_groups],
            'relationships': relationships, 'mainUsage': usage, 'status': state, 'statistics': statistics(ordered),
            'relationshipIssues': relationship_issues}
