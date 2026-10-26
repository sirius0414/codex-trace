/** Agent 独立页面及协作跳转的纯投影；跳转只使用结构化关系和来源身份，不解析预览正文。 */
import type { Group, SourceRef, ThreadSnapshot, TraceEvent, ViewState } from '../../../contracts/types';

/** 旧版多选偏好恢复到详情所属 Agent 或主 Agent；单页范围从不隐式包含后代。 */
export function activeAgentId(snapshot: ThreadSnapshot, view: ViewState): string {
  const available = (id: string | null | undefined): id is string => !!id &&
    (id === snapshot.rootGroupId || snapshot.groups.some(group => group.groupId === id));
  if (view.agentScope.kind === 'main') return snapshot.rootGroupId;
  if (view.agentScope.kind === 'selected' && view.agentScope.groupIds.length === 1 && available(view.agentScope.groupIds[0])) {
    return view.agentScope.groupIds[0];
  }
  if (view.agentScope.kind === 'group' && available(view.agentScope.groupId)) return view.agentScope.groupId;
  const selected = snapshot.events.find(event => event.eventId === view.selectedEventId);
  return available(selected?.groupId) ? selected.groupId : snapshot.rootGroupId;
}

/** 切页展开目标已加载轮次，并清除前页锚点；显式事件定位同时清除会隐藏目标的列表筛选。 */
export function agentPagePatch(snapshot: ThreadSnapshot, view: ViewState, groupId: string, eventId?: string | null): Partial<ViewState> {
  if (groupId !== snapshot.rootGroupId && !snapshot.groups.some(group => group.groupId === groupId)) return {};
  const loaded = eventId ? snapshot.events.find(event => event.eventId === eventId) : null;
  // 已知分组中的未加载事件可由调用方续读；保留定位意图，但不能将已知事件放到另一页面。
  const selectedEventId = eventId && (!loaded || loaded.groupId === groupId) ? eventId : null;
  const expandedTurnIds = new Set(view.expandedTurnIds);
  for (const event of snapshot.events) if (event.groupId === groupId && event.turnId) expandedTurnIds.add(event.turnId);
  return {
    agentScope: { kind: 'selected', groupIds: [groupId] },
    selectedEventId,
    detailTab: 'preview', follow: 'paused',
    anchor: selectedEventId ? { eventId: selectedEventId, groupId, offsetPx: 0 } : null,
    expandedGroupIds: [...new Set([...view.expandedGroupIds, groupId])],
    expandedTurnIds: [...expandedTurnIds],
    ...(selectedEventId ? { filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null } } : {}),
  };
}

function sameSource(a: SourceRef, b: SourceRef): boolean {
  if (a.kind !== b.kind || a.threadId !== b.threadId) return false;
  if (a.kind === 'api' && b.kind === 'api') return a.turnId === b.turnId && a.itemId === b.itemId;
  return a.kind === 'raw' && b.kind === 'raw' && a.segmentId === b.segmentId && a.generation === b.generation
    && a.byteStart === b.byteStart && a.byteEnd === b.byteEnd;
}

/** 只返回当前快照内可打开的对端；多接收者去重，未解析目标和关系冲突不生成入口。 */
export function interactionTargets(snapshot: ThreadSnapshot, event: TraceEvent): Group[] {
  if (event.kind !== 'collaboration') return [];
  const targets = new Set<string>();
  for (const group of snapshot.groups) {
    if (event.relatedThreadIds?.includes(group.threadId)) targets.add(group.groupId);
    if (group.creationEventId === event.eventId && group.parentGroupId === event.groupId) targets.add(group.groupId);
  }
  for (const relation of snapshot.relationships) {
    const matchingSource = relation.evidence.some(source => event.sources.some(eventSource => sameSource(source, eventSource)));
    // 重复通信合并后只会失去唯一 creationEventId；精确来源仍可证明当前操作的通信对端。
    const usable = relation.resolution === 'confirmed' || relation.type === 'communicates-with'
      && relation.resolution === 'ambiguous' && matchingSource;
    if (!usable || relation.creationEventId !== event.eventId && !matchingSource) continue;
    const other = relation.fromThreadId === event.threadId ? relation.toThreadId
      : relation.toThreadId === event.threadId ? relation.fromThreadId : null;
    if (other) for (const group of snapshot.groups) if (group.threadId === other) targets.add(group.groupId);
  }
  return snapshot.groups.filter(group => group.groupId !== event.groupId && group.threadId !== event.threadId && targets.has(group.groupId));
}
