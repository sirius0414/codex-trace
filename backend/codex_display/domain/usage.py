"""Usage snapshots are replaced by source order, never summed across snapshots."""
from .evidence import identity, timestamp, source_ref


def metric(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9007199254740991:
        return {'availability': 'recorded', 'value': value}
    return {'availability': 'notRecorded', 'value': None}


def _snapshot(thread_id, row, payload, values, *, family, scope='thread', scope_id=None, mode='cumulative', attribution='unseparated', coverage=''):
    scope_id = scope_id or thread_id
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
    """Select one main family; inherited/child records never become own usage.

    The cumulative baseline can survive fork/resume. Without validated isolation,
    even a matching owner id does not imply that the cumulative total is self-only.
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
            # This field is the resume boundary, not another billable response.
            # Explicit null clears the reachable baseline; old values must not be
            # carried into the current summary as if the checkpoint preserved them.
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
                # A single response is attributed explicitly, but its presence does
                # not establish complete request coverage or a thread total.
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
    if primary:
        result = sorted(primary.values(), key=lambda item: item['order'])
        if len(result) > 1:
            for item in result:
                item['coverage'] += '存在多个已记录 session 范围，彼此未求和。'
        return result
    if requests:
        return sorted(requests.values(), key=lambda item: item['order'])
    return [fallback] if fallback else []
