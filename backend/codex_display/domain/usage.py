"""按来源顺序替换用量快照；累计值、单次请求和不同 epoch 不混合求和。"""
from .evidence import identity, timestamp, source_ref


def metric(value):
    """区分记录的零与未记录；Python bool 不应被当成 token 计数。"""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991:
        return {'availability': 'recorded', 'value': value}
    return {'availability': 'notRecorded', 'value': None}


def _snapshot(thread_id, row, payload, values, *, family, scope='thread', scope_id=None, mode='cumulative', attribution='unseparated', coverage=''):
    """把一个来源的计数包装成快照，身份包含来源族、作用域与 session。"""
    scope_id = scope_id or thread_id
    # session 缺失时使用明确的未知范围，不从计数下降或时间间隔推断新 session。
    epoch = payload.get('session_id') if isinstance(payload.get('session_id'), str) and payload['session_id'] else f'unknown-epoch:{thread_id}'
    source_identity = f'{family}:{thread_id}:{scope}:{scope_id}:{epoch}'
    request = payload.get('response_id')
    return {'usageId': 'usage:' + identity(source_identity), 'sourceIdentity': source_identity,
            'ownerThreadId': thread_id, 'scope': scope, 'scopeId': scope_id, 'epoch': epoch,
            'order': row['ordinal'], 'recordedAt': timestamp(row['record'].get('timestamp')),
            'mode': mode, 'requestKey': request if isinstance(request, str) and request else None,
            'input': metric(values.get('input_tokens')), 'output': metric(values.get('output_tokens')),
            'cachedInput': metric(values.get('cached_input_tokens')), 'cacheSemantics': 'includedInInput',
            'attribution': attribution, 'coverage': coverage, 'source': source_ref(row)}


def usage_snapshots(thread_id, records):
    """按可信来源优先级选一组用量，继承记录和子任务记录不算成本任务用量。

    fork/resume 可能保留累计基线；即使 owner 相同，没有隔离证据也不能声明为自身消耗。
    优先累计 token_usage_record，其次明确请求，最后才用 token_count 最近观测。
    """
    records = sorted(records, key=lambda row: row['ordinal'])
    inherited = False
    primary, fallback, requests = {}, None, {}
    for row in records:
        record, payload = row.get('record', {}), row.get('record', {}).get('payload', {})
        if not isinstance(payload, dict):
            continue
        outer = record.get('type')
        if outer == 'session_meta' and payload.get('id') == thread_id:
            inherited = inherited or bool(payload.get('forked_from_id'))
        checkpoint = outer == 'compacted' and 'latest_token_usage_record' in payload
        if row.get('inherited') and (outer == 'token_usage_record' or checkpoint and isinstance(payload.get('latest_token_usage_record'), dict)):
            inherited = True
        if row.get('projectionExcluded') or row.get('inherited'):
            continue
        if checkpoint:
            # checkpoint 是恢复边界，不是新请求。明确的 null 会清空可达基线，
            # 避免把压缩前旧计数继续展示为压缩后仍可证明的累计值。
            checkpoint_payload = payload['latest_token_usage_record']
            if checkpoint_payload is None:
                primary, fallback, requests = {}, None, {}
                inherited = False
                continue
            if not isinstance(checkpoint_payload, dict):
                continue
            payload = checkpoint_payload
        attribution = 'includesInherited' if inherited else 'unseparated'
        if outer == 'token_usage_record' or checkpoint:
            if payload.get('thread_id') != thread_id:
                continue
            values = payload.get('thread_token_usage')
            if isinstance(values, dict):
                coverage = '当前任务名下的已记录累计快照；最新值替换旧快照，不与 token_count 或子任务相加。'
                coverage += '含继承基线，不能视为当前任务自身消耗。' if inherited else '累计归属未完全分离，不声称仅包含主任务自身。'
                if checkpoint:
                    coverage += '此值来自压缩时保存的最近可达用量 checkpoint，不是新增用量。'
                item = _snapshot(thread_id, row, payload, values, family='token_usage_record', attribution=attribution, coverage=coverage)
                previous = primary.get(item['epoch'])
                if previous and any(item[name]['value'] is not None and previous[name]['value'] is not None and item[name]['value'] < previous[name]['value'] for name in ('input', 'output', 'cachedInput')):
                    item['coverage'] += '计数下降，保留最新观测；未据此推定新请求或新 epoch。'
                primary[item['epoch']] = item
            elif isinstance(payload.get('usage'), dict) and isinstance(payload.get('response_id'), str) and payload['response_id']:
                # response_id 只证明这一条请求的归属，不能证明请求覆盖完整或任务总量。
                key = (payload.get('session_id'), payload['response_id'])
                requests[key] = _snapshot(thread_id, row, payload, payload['usage'], family='token_usage_record',
                    scope='request', scope_id=payload['response_id'], mode='perRequest', attribution='selfOnly',
                    coverage='仅此任务明确 response_id 的单次记录；不代表任务累计或完整模型请求数。')
        elif outer == 'event_msg' and payload.get('type') == 'token_count' and payload.get('thread_id') in (None, thread_id):
            info = payload.get('info')
            values = info.get('total_token_usage') if isinstance(info, dict) else None
            if isinstance(values, dict):
                fallback = _snapshot(thread_id, row, {}, values, family='token_count', mode='observation', attribution=attribution,
                    coverage='token_count 最近观测；可能含恢复基线或估计，只展示此来源，不与其他快照相加。')
    # 同一来源族内部以最新快照替换；跨来源和跨 epoch 都不做加法。
    if primary:
        result = sorted(primary.values(), key=lambda item: item['order'])
        if len(result) > 1:
            for item in result:
                item['coverage'] += '存在多个已记录 session 范围，彼此未求和。'
        return result
    if requests:
        return sorted(requests.values(), key=lambda item: item['order'])
    return [fallback] if fallback else []


def scoped_usage_snapshots(thread_id, records):
    """保留明确 turn/session 的轮次快照和 response 身份的单次用量。

    与任务总量独立归约：轮次累计只替换，同一请求重复记录只保留最新值。
    token_count 也会因配额更新或重算产生，不能按相邻条目分摊成步骤用量。
    checkpoint 可恢复轮次快照，但不会创建一次新的模型请求。
    """
    turns, requests = {}, {}
    inherited = False
    for row in sorted(records, key=lambda row: row['ordinal']):
        record = row.get('record', {})
        payload = record.get('payload', {})
        if not isinstance(payload, dict):
            continue
        outer = record.get('type')
        if outer == 'session_meta' and payload.get('id') == thread_id:
            inherited = inherited or bool(payload.get('forked_from_id'))
        checkpoint = outer == 'compacted' and 'latest_token_usage_record' in payload
        if row.get('inherited') and (outer == 'token_usage_record' or checkpoint and isinstance(payload.get('latest_token_usage_record'), dict)):
            inherited = True
        if row.get('projectionExcluded') or row.get('inherited'):
            continue
        if checkpoint:
            payload = payload['latest_token_usage_record']
            if payload is None:
                # 清空恢复基线不会撤销历史轮次或已完成请求的独立用量证据。
                inherited = False
            if not isinstance(payload, dict):
                continue
        elif outer != 'token_usage_record':
            continue
        if payload.get('thread_id') != thread_id:
            continue
        turn_id = payload.get('turn_id')
        if not isinstance(turn_id, str) or not turn_id:
            continue
        values = payload.get('turn_token_usage')
        if isinstance(values, dict):
            item = _snapshot(thread_id, row, payload, values, family='token_usage_record', scope='turn', scope_id=turn_id,
                             attribution='includesInherited' if inherited else 'unseparated',
                             coverage='此轮已记录累计用量；最新快照替换旧值，不与任务总量或单次请求相加。'
                                      + ('包含继承基线。' if inherited else '仅按来源明确的轮次归属展示。')
                                      + ('来自压缩 checkpoint，不是新增消费。' if checkpoint else ''))
            key = (turn_id, item['epoch'])
            previous = turns.get(key)
            if previous and any(item[name]['value'] is not None and previous[name]['value'] is not None
                                and item[name]['value'] < previous[name]['value'] for name in ('input', 'output', 'cachedInput')):
                item['coverage'] += '计数下降，保留最新快照，不推定新 session。'
            turns[key] = item
        response = payload.get('response_id')
        if not checkpoint and isinstance(payload.get('usage'), dict) and isinstance(response, str) and response:
            item = _snapshot(thread_id, row, payload, payload['usage'], family='token_usage_record',
                             scope='request', scope_id=response, mode='perRequest', attribution='selfOnly',
                             coverage='此 response_id 的单次模型请求用量；不分摊到工具或消息，不代表当前上下文占用。')
            key = (item['epoch'], response)
            requests[key] = (turn_id, item, row)
    by_turn = {}
    for (turn_id, _), item in sorted(turns.items(), key=lambda entry: entry[1]['order']):
        by_turn.setdefault(turn_id, []).append(item)
    for values in by_turn.values():
        if len(values) > 1:
            for item in values:
                item['coverage'] += '此轮存在多个 session 范围，展示最新范围，彼此未求和。'
    return by_turn, sorted(requests.values(), key=lambda entry: entry[1]['order'])
