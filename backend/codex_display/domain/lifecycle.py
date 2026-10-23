"""Persisted lifecycle evidence has explicit turn/event scope, never guessed thread scope."""
from copy import deepcopy
from .evidence import source_ref, timestamp, state, milliseconds

_KNOWN = {'running', 'completed', 'failed', 'stopped'}
_RELIABLE = {'verifiedHost', 'persistedLifecycle', 'verifiedCompleteCoverage'}


def _scoped_state(value, scope, scope_id):
    evidence = [deepcopy(item) for item in value.get('evidence', [])
                if item.get('scope') == scope and item.get('scopeId') == scope_id
                and item.get('status') in _KNOWN and item.get('basis') in _RELIABLE]
    statuses = {item['status'] for item in evidence}
    current = value.get('value') if value.get('value') in statuses else 'unknown'
    last_known = value.get('lastKnown') if value.get('lastKnown') in statuses else current if current != 'unknown' else None
    return {'value': current, 'evidence': evidence, 'lastKnown': last_known,
            'explanation': value.get('explanation', '') or '子任务执行状态没有可靠记录。'}


def _observed_at(value):
    times = [milliseconds(item.get('observedAt')) for item in value['evidence']
             if item['status'] == (value['lastKnown'] or value['value'])]
    return max((at for at in times if at is not None), default=None)


def _merge_evidence(*values):
    result = []
    for value in values:
        for item in value:
            if item not in result:
                result.append(deepcopy(item))
    return result


def compose_group_status(parent_group_status, child_thread_status, child_turns, group_id):
    """Combine an explicit parent observation with identity-bound child evidence.

    Loading a child whose thread status is unknown is not a new observation and
    must not erase the parent's reliable group state. A strictly later child turn
    can invalidate an older group observation, but can never terminate the group.
    Missing times cannot establish cross-source order. Turn evidence keeps its
    original scope; only a child *thread* observation maps to this child group.
    """
    result = _scoped_state(parent_group_status, 'group', group_id)
    # Group IDs are the projector's stable main:<threadId> identity, including
    # when that standalone main group is composed under its confirmed parent.
    child_id = group_id.removeprefix('main:') if group_id.startswith('main:') else None
    if child_id is None:
        return result
    child = _scoped_state(child_thread_status, 'thread', child_id)
    for item in child['evidence']:
        item['scope'], item['scopeId'] = 'group', group_id
        item['explanation'] = '此子任务的线程级状态：' + item['explanation']
    child['explanation'] = '此子任务的线程级状态：' + child['explanation']
    parent_at, child_at = _observed_at(result), _observed_at(child)
    if child['evidence']:
        if not result['evidence'] or child_at is not None and parent_at is not None and child_at > parent_at:
            result = child
        elif child['value'] == result['value'] and child['lastKnown'] == result['lastKnown']:
            result['evidence'] = _merge_evidence(result['evidence'], child['evidence'])
        elif child_at is None or parent_at is None or child_at == parent_at:
            # Contradictory thread/group observations without a reliable order
            # cannot be resolved by whichever collector happened to finish last.
            result['value'] = 'unknown'
            result['evidence'] = _merge_evidence(result['evidence'], child['evidence'])
            result['explanation'] = '子任务与父级观察存在冲突或时间不足；保留最后可靠观察，当前状态未知。'
    group_at = _observed_at(result)
    turn_evidence = []
    for turn in child_turns:
        if turn.get('threadId') != child_id:
            continue
        scoped = _scoped_state(turn.get('status', {}), 'turn', turn.get('turnId'))
        for item in scoped['evidence']:
            at = milliseconds(item.get('observedAt'))
            if at is not None and group_at is not None and at > group_at:
                turn_evidence.append(item)
    if turn_evidence:
        latest = max(turn_evidence, key=lambda item: item['observedAt'])
        result['value'] = 'unknown'
        result['evidence'] = _merge_evidence(result['evidence'], [latest])
        label = {'running': '启动', 'completed': '结束', 'failed': '失败', 'stopped': '中断'}[latest['status']]
        result['explanation'] = f'旧组观察之后出现了子任务轮次{label}记录；轮次状态不代表子任务整体状态，当前状态未知。'
    return result


def turn_states(thread_id, records):
    result = {}
    for row in sorted(records, key=lambda value: value['ordinal']):
        if row.get('projectionExcluded') or row.get('inherited'):
            continue
        record = row.get('record', {})
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg' or not isinstance(payload, dict):
            continue
        if payload.get('thread_id') not in (None, thread_id):
            continue
        turn_id = payload.get('turn_id')
        if not isinstance(turn_id, str) or not turn_id:
            continue
        kind = payload.get('type')
        if kind not in ('task_started', 'turn_started', 'task_complete', 'turn_complete', 'turn_aborted'):
            continue
        at = timestamp(record.get('timestamp'))
        source = source_ref(row)
        if kind in ('task_started', 'turn_started'):
            result[turn_id] = state('running', 'turn', turn_id, source, at,
                '已保存该轮次启动；当前是否仍执行没有实时证据。', confirmed=False)
        elif kind in ('task_complete', 'turn_complete'):
            value = 'failed' if payload.get('error') is not None else 'completed'
            result[turn_id] = state(value, 'turn', turn_id, source, at,
                '持久化轮次结束记录；不代表整个任务结束。')
        elif kind == 'turn_aborted':
            result[turn_id] = state('stopped', 'turn', turn_id, source, at,
                '该轮次有明确中断记录；不代表整个任务停止。')
    return result


def thread_explanation(turns):
    if not turns:
        return '没有可靠的任务执行状态记录。'
    latest = max(turns, key=lambda turn: turn['order'])['status']
    labels = {'completed': '最近轮次已完成', 'failed': '最近轮次出错', 'stopped': '最近轮次已中断'}
    if latest['value'] in labels:
        return labels[latest['value']] + ' · 任务状态未知。'
    if latest.get('lastKnown') == 'running':
        return '最近轮次有启动记录；没有任务级实时状态，当前状态未知。'
    return '没有可靠的任务级执行状态记录。'
