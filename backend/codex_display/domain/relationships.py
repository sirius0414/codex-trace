"""Explicit ownership edges; communication and fork do not imply child groups."""
from copy import deepcopy
from .evidence import identity, source_ref, timestamp, state, milliseconds


def _text(value):
    return value if isinstance(value, str) and value else None


def resolve_relationships(edges):
    """Merge matching evidence and expose ambiguity/cycles without reparenting."""
    merged = {}
    for original in edges:
        edge = deepcopy(original)
        key = (edge['type'], edge['fromThreadId'], edge['toThreadId'])
        if key not in merged:
            merged[key] = edge
            continue
        previous = merged[key]
        for source in edge['evidence']:
            if source not in previous['evidence']:
                previous['evidence'].append(source)
        a, b = previous['creationEventId'], edge['creationEventId']
        if a is not None and b is not None and a != b:
            previous['creationEventId'] = None
            previous['resolution'] = 'ambiguous'
        elif a is None and previous['resolution'] == 'confirmed':
            previous['creationEventId'] = b
            previous['turnId'] = edge['turnId'] or previous['turnId']
        if edge['resolution'] != 'confirmed':
            previous['resolution'] = edge['resolution']
    result = list(merged.values())
    by_child = {}
    for edge in result:
        if edge['type'] == 'spawned-by':
            by_child.setdefault(edge['fromThreadId'], set()).add(edge['toThreadId'])
    for edge in result:
        if edge['type'] == 'spawned-by' and len(by_child[edge['fromThreadId']]) > 1:
            edge['resolution'] = 'conflict'
    for relation_type in ('spawned-by', 'forked-from'):
        graph = {}
        for edge in result:
            if edge['type'] == relation_type and edge['resolution'] == 'confirmed':
                graph.setdefault(edge['fromThreadId'], set()).add(edge['toThreadId'])
        def reaches(start, target):
            pending, visited = [start], set()
            while pending:
                node = pending.pop()
                if node == target:
                    return True
                if node not in visited:
                    visited.add(node)
                    pending.extend(graph.get(node, ()))
            return False
        for edge in result:
            if edge['type'] == relation_type and reaches(edge['toThreadId'], edge['fromThreadId']):
                edge['resolution'] = 'cycle'
    return result


def _edge(kind, child, parent, source, event=None):
    return {'relationshipId': 'relationship:' + identity(kind, child, parent), 'type': kind,
            'fromThreadId': child, 'toThreadId': parent, 'creationEventId': event['eventId'] if event else None,
            'turnId': event['turnId'] if event else None, 'evidence': [deepcopy(source)], 'resolution': 'confirmed'}


def _reported_status(value):
    if isinstance(value, dict) and len(value) == 1:
        value = next(iter(value))
    return {'completed': 'completed', 'errored': 'failed', 'shutdown': 'stopped',
            'interrupted': 'stopped', 'running': 'running', 'pending_init': 'running',
            'pendingInit': 'running'}.get(value) if isinstance(value, str) else None


def project_relationships(thread_id, records, api_items, events, mode):
    edges, observations, names = [], {}, {}
    event_by_item, event_by_record, inherited_items = {}, {}, set()
    for event in events:
        if event['kind'] != 'collaboration':
            continue
        for source in event['sources']:
            if source['kind'] == 'api':
                event_by_item[(source.get('turnId'), source['itemId'])] = event
            else:
                event_by_record[(source['segmentId'], source['generation'], source['byteStart'])] = event
    current_meta = None
    for row in records:
        record, payload = row.get('record', {}), row.get('record', {}).get('payload', {})
        if not isinstance(payload, dict):
            continue
        if record.get('type') == 'session_meta' and payload.get('id') == thread_id:
            current_meta = payload
            source = source_ref(row)
            parent = _text(payload.get('parent_thread_id'))
            nested = payload.get('source')
            if isinstance(nested, dict):
                child_source = nested.get('subagent', nested.get('subAgent'))
                if isinstance(child_source, dict):
                    spawned = child_source.get('thread_spawn', child_source.get('threadSpawn'))
                    nested_parent = _text(spawned.get('parent_thread_id')) if isinstance(spawned, dict) else None
                    if nested_parent:
                        edges.append(_edge('spawned-by', thread_id, nested_parent, source))
            if parent:
                edges.append(_edge('spawned-by', thread_id, parent, source))
            fork = _text(payload.get('forked_from_id'))
            if fork:
                edges.append(_edge('forked-from', thread_id, fork, source))
        if row.get('inherited') or row.get('projectionExcluded'):
            item = payload.get('item')
            if record.get('type') == 'event_msg' and isinstance(item, dict) and _text(item.get('id')):
                inherited_items.add((payload.get('turn_id'), item['id']))

    def observe(child, value, source, at, event):
        at = milliseconds(at)
        incoming = {'value': value, 'source': source, 'at': at,
                    'eventId': event['eventId'] if event else None,
                    'order': event['order']['ordinal'] if event and event['order']['basis'] != 'positionNotRecorded' else None}
        previous = observations.get(child)
        if previous is None:
            observations[child] = incoming
            return
        if incoming['eventId'] != previous['eventId'] and incoming['order'] is not None and previous['order'] is not None:
            newer = incoming['order'] > previous['order']
        elif at is not None and previous['at'] is not None:
            newer = at > previous['at']
        elif incoming['eventId'] is not None and incoming['eventId'] == previous['eventId']:
            # Exact same persisted operation can catch up with a stale API item.
            newer = previous['value'] in ('running', 'unknown') and value in ('completed', 'failed', 'stopped')
        elif source['kind'] == previous['source']['kind'] == 'raw':
            # Called in logical source order, which remains usable without time.
            newer = True
        else:
            # Cross-source observations with neither an exact identity nor a
            # comparable position cannot safely supersede one another.
            observations[child] = {**previous, 'value': 'unknown'}
            return
        if newer:
            observations[child] = incoming

    def process(item, event, source, at, *, own_provenance=False):
        at = milliseconds(at)
        kind = item.get('type')
        if kind in ('subAgentActivity', 'SubAgentActivity'):
            child = _text(item.get('agentThreadId', item.get('agent_thread_id')))
            activity = item.get('kind')
            if not child or activity not in ('started', 'interacted', 'interrupted', 'completed'):
                return
            if current_meta and current_meta.get('forked_from_id') and not own_provenance:
                # API fork history can contain ancestor activity without a sender id.
                return
            name = _text(item.get('agentPath', item.get('agent_path')))
            if name:
                names[child] = name
            if activity == 'started':
                edges.append(_edge('spawned-by', child, thread_id, source, event))
            observed = {'started': 'running', 'interacted': 'unknown', 'interrupted': 'stopped', 'completed': 'completed'}[activity]
            observe(child, observed, source, at, event)
        elif kind in ('collabAgentToolCall', 'CollabAgentToolCall'):
            sender = _text(item.get('senderThreadId', item.get('sender_thread_id')))
            receivers = item.get('receiverThreadIds', item.get('receiver_thread_ids', []))
            if sender != thread_id or not isinstance(receivers, list):
                return
            tool = item.get('tool')
            states = item.get('agentsStates', item.get('agents_states', {}))
            for child in receivers:
                if not _text(child):
                    continue
                if tool in ('spawnAgent', 'spawn_agent'):
                    edges.append(_edge('spawned-by', child, sender, source, event))
                elif tool in ('sendInput', 'send_input', 'sendMessage', 'send_message', 'followupTask', 'followup_task'):
                    edges.append(_edge('communicates-with', sender, child, source, event))
                value = _reported_status(states.get(child)) if isinstance(states, dict) else None
                if value:
                    observe(child, value, source, at, event)

    if mode == 'api':
        for wrapper in api_items:
            item = wrapper.get('item', {})
            if item.get('type') not in ('subAgentActivity', 'collabAgentToolCall'):
                continue
            key = (wrapper.get('turnId'), item.get('id'))
            event = event_by_item.get(key)
            if event is not None and key not in inherited_items:
                process(item, event, next(source for source in event['sources'] if source['kind'] == 'api'),
                        wrapper.get('completedAtMs'), own_provenance=any(s['kind'] == 'raw' and s['threadId'] == thread_id for s in event['sources']))
    for row in records:
        if row.get('inherited') or row.get('projectionExcluded'):
            continue
        record, payload = row.get('record', {}), row.get('record', {}).get('payload', {})
        if record.get('type') != 'event_msg' or not isinstance(payload, dict):
            continue
        if payload.get('thread_id') not in (None, thread_id):
            continue
        kind = payload.get('type')
        if kind == 'item_completed':
            item = payload.get('item')
            if not isinstance(item, dict) or item.get('type') not in ('SubAgentActivity', 'CollabAgentToolCall', 'subAgentActivity', 'collabAgentToolCall'):
                continue
        elif kind not in ('sub_agent_activity', 'collab_agent_spawn_end'):
            continue
        source = source_ref(row)
        event = event_by_record.get((source['segmentId'], source['generation'], source['byteStart']))
        if payload.get('type') == 'item_completed' and isinstance(payload.get('item'), dict):
            process(payload['item'], event, source, payload.get('completed_at_ms', timestamp(record.get('timestamp'))), own_provenance=True)
        elif payload.get('type') == 'sub_agent_activity':
            process({**payload, 'type': 'SubAgentActivity'}, event, source, payload.get('occurred_at_ms', timestamp(record.get('timestamp'))), own_provenance=True)
        elif payload.get('type') == 'collab_agent_spawn_end' and payload.get('sender_thread_id') == thread_id:
            child = _text(payload.get('new_thread_id'))
            if child:
                edges.append(_edge('spawned-by', child, thread_id, source, event))
                name = _text(payload.get('new_agent_nickname')) or _text(payload.get('new_agent_role'))
                if name:
                    names[child] = name
                value = _reported_status(payload.get('status'))
                if value:
                    observe(child, value, source, payload.get('completed_at_ms', timestamp(record.get('timestamp'))), event)
    edges = resolve_relationships(edges)
    groups = []
    for edge in edges:
        if edge['type'] != 'spawned-by' or edge['toThreadId'] != thread_id or edge['resolution'] != 'confirmed':
            continue
        child = edge['fromThreadId']
        group_id = f'main:{child}'
        observed = observations.get(child)
        status = {'value': 'unknown', 'evidence': [], 'lastKnown': None, 'explanation': '子任务当前执行状态未记录。'}
        if observed and observed['value'] != 'unknown':
            status = state(observed['value'], 'group', group_id, observed['source'], observed['at'],
                           '父任务持久化的子代理执行观察；新的子轮次可使状态再次变化。', confirmed=observed['value'] != 'running')
        groups.append({'groupId': group_id, 'kind': 'agent', 'threadId': child, 'name': names.get(child, f'子 Agent {child[:8]}'),
                       'parentGroupId': f'main:{thread_id}', 'creationEventId': edge['creationEventId'],
                       'relationshipId': edge['relationshipId'], 'status': status, 'loadedEventCount': 0,
                       'loadedToolCallCount': 0, 'usage': [], 'readIssue': '子任务记录尚未读取。'})
    issues = [f"关系 {edge['relationshipId']}：{edge['resolution']}，未据此建立层级。" for edge in edges if edge['resolution'] != 'confirmed']
    return groups, edges, issues
