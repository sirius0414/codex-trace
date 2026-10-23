import type { EventKind, Group, Relationship, ThreadSnapshot, TraceEvent, UsageMetric, UsageSnapshot, ViewState } from '../../../contracts/types';

export const kindLabels: Record<EventKind, string> = {
  userMessage: '用户', assistantMessage: '助手', reasoningSummary: '思考摘要', systemContext: '系统说明',
  developerContext: '开发者说明', environment: '运行环境', toolCall: '工具调用', toolResult: '工具结果',
  compaction: '上下文压缩', collaboration: '协作操作',
};
export const statusLabels = { running: '执行中', completed: '完成', failed: '失败', stopped: '已停止', unknown: '未知' };
export function relationshipEndpoints(relation: Relationship): Array<[string, string]> {
  return relation.type === 'spawned-by' ? [['父任务', relation.toThreadId], ['子任务', relation.fromThreadId]]
    : relation.type === 'forked-from' ? [['来源任务', relation.toThreadId], ['分叉任务', relation.fromThreadId]]
      : [['发起任务', relation.fromThreadId], ['通信对象', relation.toThreadId]];
}
export function clockTime(value: number | null | undefined) {
  return value == null ? '—' : new Date(value).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
export function duration(value: number | null) {
  if (value == null) return '—';
  return value < 1000 ? `${Math.round(value)}ms` : `${Number((value / 1000).toFixed(1))}s`;
}
export function relativeTime(milliseconds: number) {
  const seconds = Math.floor(Math.abs(milliseconds) / 1000);
  return `${milliseconds < 0 ? '−' : ''}${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
export function recordedEnd(event: TraceEvent): number | null {
  const { startMs, endMs, durationMs, precision } = event.timing;
  if (startMs == null) return null;
  if (endMs != null && Number.isFinite(endMs) && endMs >= startMs) return endMs;
  if (precision === 'exact' && durationMs != null && Number.isFinite(durationMs) && durationMs >= 0 && Math.abs(startMs + durationMs) <= Number.MAX_SAFE_INTEGER) return startMs + durationMs;
  return startMs;
}
export function overviewFrame(snapshot: Pick<ThreadSnapshot, 'timelineOrigin'> | null, events: TraceEvent[]) {
  const evidence = snapshot?.timelineOrigin;
  const origin = evidence?.basis === 'threadCreatedAt' && evidence.precision === 'second'
    && Number.isSafeInteger(evidence.timestampMs) && evidence.timestampMs >= 0 && evidence.timestampMs % 1000 === 0
    ? evidence.timestampMs : null;
  let first = Infinity, last = -Infinity;
  for (const event of events) {
    const start = event.timing.startMs;
    if (start == null || !Number.isFinite(start)) continue;
    first = Math.min(first, start); last = Math.max(last, recordedEnd(event) ?? start);
  }
  const ordered = first === Infinity;
  const min = ordered ? 1 : origin == null ? first : Math.min(origin, first);
  return { origin, ordered, min, max: ordered ? Math.max(2, events.length) : Math.max(min + 1000, last, origin ?? -Infinity),
    explanation: ordered ? '记录顺序：没有准确事件时间，位置不表示时长。'
      : origin == null ? '主任务创建时间未记录；按事件的绝对时间显示，不把子任务或已加载记录起点当作主任务开始。'
        : `相对主任务创建时间；依据 thread/read.createdAt，秒精度。创建时间不是本次执行开始或执行耗时，继承记录可能早于创建时间。${evidence?.explanation ?? ''}` };
}
export function overviewLane(event: TraceEvent) {
  return ['toolCall', 'toolResult', 'collaboration'].includes(event.kind) ? 2 : ['assistantMessage', 'reasoningSummary'].includes(event.kind) ? 1 : 0;
}
export type OverviewMark = {
  key: string; lane: number; start: number; end: number; events: TraceEvent[]; selected: boolean;
};
export function overviewMarks(events: TraceEvent[], options: { min: number; max: number; mode: 'time' | 'order'; selectedEventId: string | null; bins?: number }): OverviewMark[] {
  // Bound SVG DOM by horizontal cells and three tracks, not source length.
  // A selected event is always kept separate so its identity stays visible.
  const bins = Math.max(1, Math.min(40, Math.floor(options.bins ?? 40)));
  const groups = new Map<string, OverviewMark>();
  const span = Math.max(1, options.max - options.min);
  for (let index = 0; index < events.length; index++) {
    const event = events[index];
    const start = options.mode === 'order' ? index + 1 : event.timing.startMs;
    if (start == null) continue;
    const end = options.mode === 'order' ? start : recordedEnd(event) ?? start;
    if (start > options.max || end < options.min) continue;
    const lane = overviewLane(event), selected = event.eventId === options.selectedEventId;
    const bucket = Math.max(0, Math.min(bins - 1, Math.floor((start - options.min) / span * bins)));
    const key = selected ? `selected:${event.eventId}` : `${lane}:${bucket}`;
    const current = groups.get(key);
    if (current) { current.events.push(event); current.start = Math.min(current.start, start); current.end = Math.max(current.end, end); }
    else groups.set(key, { key, lane, start, end, events: [event], selected });
  }
  // Paint the selected mark last, keeping its keyboard/visual target available.
  return [...groups.values()].sort((a, b) => Number(a.selected) - Number(b.selected) || a.start - b.start || a.lane - b.lane);
}
export function metric(value: UsageMetric | undefined, missing = '—') {
  return value?.availability === 'recorded' ? value.value.toLocaleString('zh-CN') : missing;
}
export function latestUsage(values: UsageSnapshot[]) {
  // Choose a recorded scope, never add cumulative snapshots or child usage.
  const thread = values.filter(value => value.scope === 'thread');
  return (thread.length ? thread : values).reduce<UsageSnapshot | null>((last, item) => !last || item.order > last.order ? item : last, null);
}
export function groupScope(snapshot: ThreadSnapshot, view: ViewState) {
  if (view.agentScope.kind === 'all') return null;
  if (view.agentScope.kind === 'main') return new Set([snapshot.rootGroupId]);
  if (view.agentScope.kind === 'selected') return new Set(view.agentScope.groupIds);
  const result = new Set<string>();
  const add = (id: string) => {
    if (result.has(id)) return;
    result.add(id);
    snapshot.groups.filter(group => group.parentGroupId === id).forEach(group => add(group.groupId));
  };
  if (view.agentScope.groupId) add(view.agentScope.groupId);
  return result;
}
/** Resolve old saved scopes for the buttons without changing their meaning. */
export function selectedAgentIds(snapshot: ThreadSnapshot, view: ViewState): Set<string> {
  return groupScope(snapshot, view) ?? new Set(snapshot.groups.map(group => group.groupId));
}
export function scopedEvents(snapshot: ThreadSnapshot, view: ViewState) {
  const groups = groupScope(snapshot, view);
  return snapshot.events.filter(event => !groups || groups.has(event.groupId));
}
export function visibleEvents(snapshot: ThreadSnapshot, view: ViewState, matchedIds: Set<string> | null) {
  const filters = view.filters;
  return scopedEvents(snapshot, view).filter(event => {
    if (matchedIds && !matchedIds.has(event.eventId)) return false;
    if (filters.kinds.length && !filters.kinds.includes(event.kind)) return false;
    if (filters.toolName && event.toolName !== filters.toolName) return false;
    if (filters.failedOnly && event.status.value !== 'failed') return false;
    if (filters.timeRange) {
      if (event.timing.startMs == null) return false;
      const end = recordedEnd(event) ?? event.timing.startMs;
      if (end < filters.timeRange.startMs || event.timing.startMs > filters.timeRange.endMs) return false;
    }
    return true;
  });
}
export type ListRow = { key: string; height: number; top: number } & (
  { type: 'event'; event: TraceEvent; child: boolean } |
  { type: 'turn'; id: string; label: string; count: number; expanded: boolean } |
  { type: 'group'; group: Group; expanded: boolean } |
  { type: 'warning'; group: Group } |
  { type: 'earlier'; group?: Group }
);
export function readingAnchor(rows: ListRow[], scrollTop: number): ViewState['anchor'] {
  const first = rows.find(row => row.type === 'event' && row.top + row.height > scrollTop);
  return first?.type === 'event' ? { eventId: first.event.eventId, groupId: first.event.groupId, offsetPx: scrollTop - first.top } : null;
}
export function makeRows(snapshot: ThreadSnapshot, view: ViewState, visible: TraceEvent[]): ListRow[] {
  const rows: ListRow[] = [];
  let top = 0;
  const add = (row: Omit<ListRow, 'top'>) => { rows.push({ ...row, top } as ListRow); top += row.height; };
  const append = (row: Record<string, unknown> & { key: string; height: number }) => add(row as Omit<ListRow, 'top'>);
  const visibleIds = new Set(visible.map(event => event.eventId));
  const main = snapshot.events.filter(event => event.groupId === snapshot.rootGroupId);
  const children = snapshot.groups.filter(group => group.groupId !== snapshot.rootGroupId);
  const rendered = new Set<string>();
  const allTurns = new Map(snapshot.turns.map(turn => [turn.turnId, turn.order + 1]));
  const expandedTurns = new Set(view.expandedTurnIds);
  const expandedGroups = new Set(view.expandedGroupIds);
  const unfiltered = !view.filters.text && !view.filters.kinds.length && !view.filters.toolName && !view.filters.failedOnly && !view.filters.timeRange;
  const allowedGroups = groupScope(snapshot, view);
  if (view.agentScope.kind === 'selected') {
    // Explicit Agent selections are independent sections. An unselected parent,
    // or its collapsed turn, must never hide a selected descendant.
    const selected = selectedAgentIds(snapshot, view);
    const appendEvents = (groupId: string, child: boolean) => {
      const events = visible.filter(event => event.groupId === groupId);
      const counts = new Map<string | null, number>();
      for (const event of events) counts.set(event.turnId, (counts.get(event.turnId) ?? 0) + 1);
      let turn: string | null | undefined;
      for (const event of events) {
        if (event.turnId !== turn) {
          turn = event.turnId;
          const expanded = turn == null || expandedTurns.has(turn);
          append({ key: `turn:${groupId}:${turn ?? 'context'}`, type: 'turn', id: turn ?? '__context__',
            label: turn == null ? '任务上下文 · 位置未记录' : `${child ? '子任务 · ' : ''}第 ${allTurns.get(turn) ?? '—'} 轮`,
            count: counts.get(turn) ?? 0, expanded, height: 23 });
        }
        if (event.turnId && !expandedTurns.has(event.turnId)) continue;
        append({ key: `event:${event.eventId}`, type: 'event', event, child, height: 28 });
      }
    };
    if (selected.has(snapshot.rootGroupId)) {
      if (snapshot.coverage.hasEarlier) append({ key: 'earlier', type: 'earlier', height: 23 });
      appendEvents(snapshot.rootGroupId, false);
    }
    for (const group of children) {
      if (!selected.has(group.groupId) || !unfiltered && !visible.some(event => event.groupId === group.groupId)) continue;
      const expanded = expandedGroups.has(group.groupId);
      append({ key: `group:${group.groupId}`, type: 'group', group, expanded, height: 28 });
      if (group.readIssue) append({ key: `warning:${group.groupId}`, type: 'warning', group, height: 25 });
      if (!expanded) continue;
      if (group.hasEarlier && group.nextCursor) append({ key: `earlier:${group.groupId}`, type: 'earlier', group, height: 23 });
      appendEvents(group.groupId, true);
    }
    return rows;
  }
  if (snapshot.coverage.hasEarlier) append({ key: 'earlier', type: 'earlier', height: 23 });
  const hasVisibleGroup = (group: Group, seen = new Set<string>()): boolean => {
    if (seen.has(group.groupId)) return false;
    seen.add(group.groupId);
    if (unfiltered && (!allowedGroups || allowedGroups.has(group.groupId))) return true;
    return visible.some(event => event.groupId === group.groupId) || Boolean(group.creationEventId && visibleIds.has(group.creationEventId)) ||
      children.some(child => child.parentGroupId === group.groupId && hasVisibleGroup(child, seen));
  };
  function appendGroup(group: Group) {
    if (rendered.has(group.groupId) || !hasVisibleGroup(group)) return;
    rendered.add(group.groupId);
    const expanded = expandedGroups.has(group.groupId);
    append({ key: `group:${group.groupId}`, type: 'group', group, expanded, height: 28 });
    if (group.readIssue) append({ key: `warning:${group.groupId}`, type: 'warning', group, height: 25 });
    if (!expanded) return;
    if (group.hasEarlier && group.nextCursor) append({ key: `earlier:${group.groupId}`, type: 'earlier', group, height: 23 });
    const events = snapshot.events.filter(event => event.groupId === group.groupId);
    let turn: string | null | undefined;
    for (const event of events) {
      const nested = children.filter(child => child.parentGroupId === group.groupId && child.creationEventId === event.eventId);
      if (!visibleIds.has(event.eventId) && !nested.some(child => hasVisibleGroup(child))) continue;
      if (event.turnId !== turn && event.turnId != null) {
        turn = event.turnId;
        append({ key: `turn:${group.groupId}:${turn}`, type: 'turn', id: turn,
          label: `子任务 · 第 ${allTurns.get(turn) ?? '—'} 轮`, count: visible.filter(item => item.turnId === turn && item.groupId === group.groupId).length,
          expanded: expandedTurns.has(turn), height: 23 });
      }
      if (event.turnId && !expandedTurns.has(event.turnId)) continue;
      if (nested.length) nested.forEach(appendGroup);
      else if (visibleIds.has(event.eventId)) append({ key: `event:${event.eventId}`, type: 'event', event, child: true, height: 28 });
    }
    children.filter(child => child.parentGroupId === group.groupId && !rendered.has(child.groupId)).forEach(appendGroup);
  }
  let turn: string | null | undefined;
  for (const event of main) {
    const created = children.filter(group => group.creationEventId === event.eventId && view.agentScope.kind !== 'main');
    if (!visibleIds.has(event.eventId) && !created.some(group => hasVisibleGroup(group))) continue;
    if (event.turnId !== turn) {
      turn = event.turnId;
      const expanded = turn == null || expandedTurns.has(turn);
      append({ key: `turn:main:${turn ?? 'context'}`, type: 'turn', id: turn ?? '__context__',
        label: turn == null ? '任务上下文 · 位置未记录' : `第 ${allTurns.get(turn) ?? '—'} 轮`,
        count: visible.filter(item => item.turnId === turn && item.groupId === snapshot.rootGroupId).length, expanded, height: 23 });
    }
    if (event.turnId && !expandedTurns.has(event.turnId)) continue;
    if (created.length) created.forEach(appendGroup);
    else if (visibleIds.has(event.eventId)) append({ key: `event:${event.eventId}`, type: 'event', event, child: false, height: 28 });
  }
  // A confirmed child may be readable when its creation event is not in the
  // loaded page. Keep it accessible without inventing a creation position.
  if (view.agentScope.kind !== 'main') children.filter(group => !rendered.has(group.groupId) && (!group.creationEventId || !snapshot.events.some(event => event.eventId === group.creationEventId))).forEach(appendGroup);
  return rows;
}
export function virtualWindow(rows: ListRow[], scrollTop: number, height: number, overscan = 5) {
  let low = 0, high = rows.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (rows[middle].top + rows[middle].height <= scrollTop) low = middle + 1; else high = middle;
  }
  const start = Math.max(0, low - overscan);
  let end = low;
  while (end < rows.length && rows[end].top < scrollTop + height) end++;
  return { start, end: Math.min(rows.length, end + overscan), totalHeight: rows.at(-1) ? rows.at(-1)!.top + rows.at(-1)!.height : 0 };
}
