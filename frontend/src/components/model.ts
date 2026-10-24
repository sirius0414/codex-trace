import type { EventKind, Group, Relationship, ThreadSnapshot, TraceEvent, UsageMetric, UsageSnapshot, ViewState } from '../../../contracts/types';

export const kindLabels: Record<EventKind, string> = {
  userMessage: '用户', assistantMessage: '助手', reasoningSummary: '思考摘要', systemContext: '系统说明',
  developerContext: '开发者说明', environment: '运行环境', toolCall: '工具调用', toolResult: '工具结果',
  compaction: '上下文压缩', collaboration: '协作操作', modelUsage: '模型用量',
};
export const statusLabels = { running: '执行中', completed: '完成', failed: '失败', stopped: '已停止', unknown: '未知' };
type Language = 'zh' | 'en';
const englishKindLabels: Record<EventKind, string> = {
  userMessage: 'User', assistantMessage: 'Assistant', reasoningSummary: 'Reasoning summary', systemContext: 'System context',
  developerContext: 'Developer context', environment: 'Environment', toolCall: 'Tool call', toolResult: 'Tool result',
  compaction: 'Compaction', collaboration: 'Collaboration', modelUsage: 'Model usage',
};
const englishStatusLabels: typeof statusLabels = { running: 'Running', completed: 'Completed', failed: 'Failed', stopped: 'Stopped', unknown: 'Unknown' };
export function localizedKindLabels(language: Language = 'zh') {
  return language === 'en' ? englishKindLabels : kindLabels;
}
export function localizedStatusLabels(language: Language = 'zh') {
  return language === 'en' ? englishStatusLabels : statusLabels;
}
export function relationshipEndpoints(relation: Relationship, language: Language = 'zh'): Array<[string, string]> {
  return relation.type === 'spawned-by' ? [[language === 'en' ? 'Parent task' : '父任务', relation.toThreadId], [language === 'en' ? 'Child task' : '子任务', relation.fromThreadId]]
    : relation.type === 'forked-from' ? [[language === 'en' ? 'Source task' : '来源任务', relation.toThreadId], [language === 'en' ? 'Forked task' : '分叉任务', relation.fromThreadId]]
      : [[language === 'en' ? 'Initiating task' : '发起任务', relation.fromThreadId], [language === 'en' ? 'Recipient' : '通信对象', relation.toThreadId]];
}
export function clockTime(value: number | null | undefined, language: Language = 'zh') {
  return value == null ? '—' : new Date(value).toLocaleTimeString(language === 'en' ? 'en-US' : 'zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
export function duration(value: number | null) {
  if (value == null) return '—';
  return value < 1000 ? `${Math.round(value)}ms` : `${Number((value / 1000).toFixed(1))}s`;
}
export function relativeTime(milliseconds: number) {
  const seconds = Math.floor(Math.abs(milliseconds) / 1000);
  return `${milliseconds < 0 ? '−' : ''}${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
/** 仅按可靠结束时间或 exact 时长延伸标记；缺少时长证据时只显示起点。 */
export function recordedEnd(event: TraceEvent): number | null {
  const { startMs, endMs, durationMs, precision } = event.timing;
  if (startMs == null) return null;
  if (endMs != null && Number.isFinite(endMs) && endMs >= startMs) return endMs;
  if (precision === 'exact' && durationMs != null && Number.isFinite(durationMs) && durationMs >= 0 && Math.abs(startMs + durationMs) <= Number.MAX_SAFE_INTEGER) return startMs + durationMs;
  return startMs;
}
/** 时间轴原点只认主任务创建证据；无事件时间时退为顺序轴，不把位置解释为时长。 */
export function overviewFrame(snapshot: Pick<ThreadSnapshot, 'timelineOrigin'> | null, events: TraceEvent[], language: Language = 'zh') {
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
    explanation: ordered ? language === 'en' ? 'Record order: exact event times are unavailable; position does not indicate duration.' : '记录顺序：没有准确事件时间，位置不表示时长。'
      : origin == null ? language === 'en' ? 'The main task creation time was not recorded. Events use absolute time; child task or loaded record start times do not indicate the main task start.' : '主任务创建时间未记录；按事件的绝对时间显示，不把子任务或已加载记录起点当作主任务开始。'
        : `${language === 'en' ? 'Relative to main task creation, based on thread/read.createdAt at second precision. Creation time does not indicate this execution’s start or elapsed time; inherited records may predate creation. ' : '相对主任务创建时间；依据 thread/read.createdAt，秒精度。创建时间不是本次执行开始或执行耗时，继承记录可能早于创建时间。'}${evidence?.explanation ?? ''}` };
}
export function overviewLane(event: TraceEvent) {
  return ['toolCall', 'toolResult', 'collaboration'].includes(event.kind) ? 2 : ['assistantMessage', 'reasoningSummary', 'modelUsage'].includes(event.kind) ? 1 : 0;
}
export type OverviewMark = {
  key: string; lane: number; start: number; end: number; events: TraceEvent[]; selected: boolean;
};
export function overviewMarks(events: TraceEvent[], options: { min: number; max: number; mode: 'time' | 'order'; selectedEventId: string | null; bins?: number }): OverviewMark[] {
  // 按横向格子和三条轨道聚合，限制 SVG 节点数量；选中事件始终单列，保留定位目标。
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
  // 选中标记最后绘制，避免被聚合簇遮住，也保留可访问的键盘目标。
  return [...groups.values()].sort((a, b) => Number(a.selected) - Number(b.selected) || a.start - b.start || a.lane - b.lane);
}
export function metric(value: UsageMetric | undefined, missing = '—', language: Language = 'zh') {
  return value?.availability === 'recorded' ? value.value.toLocaleString(language === 'en' ? 'en-US' : 'zh-CN') : missing;
}
export function latestUsage(values: UsageSnapshot[]) {
  // 优先选任务范围内最新的用量记录，不叠加累计快照或子任务用量。
  const thread = values.filter(value => value.scope === 'thread');
  return (thread.length ? thread : values).reduce<UsageSnapshot | null>((last, item) => !last || item.order > last.order ? item : last, null);
}
/** 兼容旧层级范围与显式多选：旧分组含后代，显式多选只包含用户列出的 Agent。 */
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
/** 将旧偏好范围映射为按钮选中态，沿用原范围语义。 */
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
  { type: 'turn'; id: string; label: string; count: number; expanded: boolean; usage: UsageSnapshot[] } |
  { type: 'group'; group: Group; expanded: boolean } |
  { type: 'warning'; group: Group } |
  { type: 'earlier'; group?: Group }
);
/** 锚定首个未完全滚出视口的事件；偏移可为负，以保留前方标题行占据的空间。 */
export function readingAnchor(rows: ListRow[], scrollTop: number): ViewState['anchor'] {
  const first = rows.find(row => row.type === 'event' && row.top + row.height > scrollTop);
  return first?.type === 'event' ? { eventId: first.event.eventId, groupId: first.event.groupId, offsetPx: scrollTop - first.top } : null;
}
/** 将分组/轮次折叠树展平为带绝对 top 的行；行高必须与 EventList/CSS 一致。 */
export function makeRows(snapshot: ThreadSnapshot, view: ViewState, visible: TraceEvent[], language: Language = 'zh'): ListRow[] {
  const rows: ListRow[] = [];
  let top = 0;
  const add = (row: Omit<ListRow, 'top'>) => { rows.push({ ...row, top } as ListRow); top += row.height; };
  const append = (row: Record<string, unknown> & { key: string; height: number }) => add(row as Omit<ListRow, 'top'>);
  const visibleIds = new Set(visible.map(event => event.eventId));
  const main = snapshot.events.filter(event => event.groupId === snapshot.rootGroupId);
  const children = snapshot.groups.filter(group => group.groupId !== snapshot.rootGroupId);
  const rendered = new Set<string>();
  // 轮次身份同时包含所属任务，子任务即使复用 turnId 也不能串用计数。
  const allTurns = new Map(snapshot.turns.map(turn => [JSON.stringify([turn.threadId, turn.turnId]), turn]));
  const turnData = (threadId: string, turnId: string | null) => allTurns.get(JSON.stringify([threadId, turnId]));
  const turnNumber = (threadId: string, turnId: string) => {
    const turn = turnData(threadId, turnId);
    return turn ? turn.order + 1 : '—';
  };
  const expandedTurns = new Set(view.expandedTurnIds);
  const expandedGroups = new Set(view.expandedGroupIds);
  const unfiltered = !view.filters.text && !view.filters.kinds.length && !view.filters.toolName && !view.filters.failedOnly && !view.filters.timeRange;
  const allowedGroups = groupScope(snapshot, view);
  if (view.agentScope.kind === 'selected') {
    // 显式多选的 Agent 各自成节，未选父任务及其折叠轮次不能隐藏已选后代。
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
            label: turn == null ? language === 'en' ? 'Task context · position not recorded' : '任务上下文 · 位置未记录' : language === 'en' ? `${child ? 'Child task · ' : ''}Turn ${turnNumber(event.threadId, turn)}` : `${child ? '子任务 · ' : ''}第 ${turnNumber(event.threadId, turn)} 轮`,
            count: counts.get(turn) ?? 0, expanded, height: 23, usage: turnData(event.threadId, turn)?.usage ?? [] });
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
          label: language === 'en' ? `Child task · Turn ${turnNumber(event.threadId, turn)}` : `子任务 · 第 ${turnNumber(event.threadId, turn)} 轮`, count: visible.filter(item => item.turnId === turn && item.groupId === group.groupId).length,
          expanded: expandedTurns.has(turn), height: 23, usage: turnData(event.threadId, turn)?.usage ?? [] });
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
        label: turn == null ? language === 'en' ? 'Task context · position not recorded' : '任务上下文 · 位置未记录' : language === 'en' ? `Turn ${turnNumber(event.threadId, turn)}` : `第 ${turnNumber(event.threadId, turn)} 轮`,
        count: visible.filter(item => item.turnId === turn && item.groupId === snapshot.rootGroupId).length, expanded, height: 23, usage: turnData(event.threadId, turn)?.usage ?? [] });
    }
    if (event.turnId && !expandedTurns.has(event.turnId)) continue;
    if (created.length) created.forEach(appendGroup);
    else if (visibleIds.has(event.eventId)) append({ key: `event:${event.eventId}`, type: 'event', event, child: false, height: 28 });
  }
  // 已确认子任务可能可读，但创建事件尚未加载；保留入口，不虚构其创建位置。
  if (view.agentScope.kind !== 'main') children.filter(group => !rendered.has(group.groupId) && (!group.creationEventId || !snapshot.events.some(event => event.eventId === group.creationEventId))).forEach(appendGroup);
  return rows;
}
/** top 单调递增，二分定位首个可见行；上下预挂载少量行以覆盖滚动和键盘移动。 */
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
