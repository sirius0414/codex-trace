"""生命周期必须绑定明确作用域：轮次/操作结束不能外推为整个任务结束。"""
from copy import deepcopy
from .evidence import source_ref, timestamp, state, milliseconds

_KNOWN = {'running', 'completed', 'failed', 'stopped'}
_RELIABLE = {'verifiedHost', 'persistedLifecycle', 'verifiedCompleteCoverage'}


def _scoped_state(value, scope, scope_id):
    """只接受身份、作用域和证据基础均匹配的状态，丢弃无依据的显示值。"""
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
    """合并父级观察与身份已确认的子任务证据，不扩大证据原有作用域。

    读取到未知子状态不算新观察，不能覆盖父级的可靠记录。更晚的子轮次只能让旧组状态
    失效，不能终结整个组。没有可比时间时保留冲突；只有线程级证据可映射到子任务组。
    """
    result = _scoped_state(parent_group_status, 'group', group_id)
    # 子任务独立读取和挂到父任务下时都使用 main:<threadId>，以此约束证据身份。
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
            # 无可靠先后关系的矛盾证据不能由采集完成顺序裁决。
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
    """只从本任务自身的持久化生命周期记录归约轮次状态。"""
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
            # 启动曾发生是事实；当前仍在执行需要实时证据，因此保留 unknown。
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
    """解释线程状态为何未知；最近一个轮次结束不意味着不会再有下一轮。"""
    if not turns:
        return '没有可靠的任务执行状态记录。'
    latest = max(turns, key=lambda turn: turn['order'])['status']
    labels = {'completed': '最近轮次已完成', 'failed': '最近轮次出错', 'stopped': '最近轮次已中断'}
    if latest['value'] in labels:
        return labels[latest['value']] + ' · 任务状态未知。'
    if latest.get('lastKnown') == 'running':
        return '最近轮次有启动记录；没有任务级实时状态，当前状态未知。'
    return '没有可靠的任务级执行状态记录。'
