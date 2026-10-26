/** 列表展开路径与范围定位；只返回视图补丁，原记录集合和详情选择仍由上游持有。 */
import type { ThreadSnapshot, TimeRange, TraceEvent, ViewState } from '../../../contracts/types';
import { eventRangeIds } from './model.ts';

export function bulkExpansion(snapshot: ThreadSnapshot, view: ViewState, visible: TraceEvent[], expand: boolean) {
  // 只处理当前筛选的已加载记录；其他 Agent/筛选范围保存的展开偏好不受影响。
  const turns = new Set(visible.flatMap(event => event.turnId ? [event.turnId] : []));
  const groups = new Set(visible.filter(event => event.groupId !== snapshot.rootGroupId).map(event => event.groupId));
  const expandedTurnIds = new Set(view.expandedTurnIds);
  const expandedGroupIds = new Set(view.expandedGroupIds);

  if (expand) {
    // 旧层级范围需要同时打开祖先及其创建轮次；显式多选已按独立 Agent 节展示。
    if (view.agentScope.kind !== 'selected') {
      const byGroup = new Map(snapshot.groups.map(group => [group.groupId, group]));
      const byEvent = new Map(snapshot.events.map(event => [event.eventId, event]));
      const visited = new Set<string>();
      for (const id of groups) {
        let group = byGroup.get(id);
        while (group && !visited.has(group.groupId)) {
          visited.add(group.groupId);
          if (group.groupId !== snapshot.rootGroupId) groups.add(group.groupId);
          const creation = group.creationEventId ? byEvent.get(group.creationEventId) : null;
          if (creation?.turnId) turns.add(creation.turnId);
          group = group.parentGroupId ? byGroup.get(group.parentGroupId) : undefined;
        }
      }
    }
    turns.forEach(id => expandedTurnIds.add(id));
    groups.forEach(id => expandedGroupIds.add(id));
  } else {
    // 折叠轮次但保留分组展开态，让再次展开时仍能恢复同一层级路径。
    turns.forEach(id => expandedTurnIds.delete(id));
  }

  return { follow: 'paused' as const, expandedTurnIds: [...expandedTurnIds], expandedGroupIds: [...expandedGroupIds] };
}

/**
 * 在原筛选后的可见记录中定位首个真实时间命中项，只打开其必要路径并设置阅读锚点。
 * 无匹配时不移动；不选择详情、不调整筛选或 Agent 范围，也不删去选区外的记录。
 */
export function rangeReveal(snapshot: ThreadSnapshot, view: ViewState, visible: TraceEvent[], range: TimeRange): Partial<ViewState> | null {
  const matches = eventRangeIds(snapshot, range);
  const target = visible.find(event => matches?.has(event.eventId));
  if (!target) return null;
  return { ...bulkExpansion(snapshot, view, [target], true),
    anchor: { eventId: target.eventId, groupId: target.groupId, offsetPx: 0 } };
}
