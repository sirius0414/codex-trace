import type { ThreadSnapshot, TraceEvent, ViewState } from '../../../contracts/types';

export function bulkExpansion(snapshot: ThreadSnapshot, view: ViewState, visible: TraceEvent[], expand: boolean) {
  // `visible` already applies the current Agent selection and event filters.
  const turns = new Set(visible.flatMap(event => event.turnId ? [event.turnId] : []));
  const groups = new Set(visible.filter(event => event.groupId !== snapshot.rootGroupId).map(event => event.groupId));
  const expandedTurnIds = new Set(view.expandedTurnIds);
  const expandedGroupIds = new Set(view.expandedGroupIds);

  if (expand) {
    // Legacy hierarchical scopes need each creation turn and ancestor group
    // open as well. Explicit Agent selections render independent sections.
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
    turns.forEach(id => expandedTurnIds.delete(id));
  }

  return { follow: 'paused' as const, expandedTurnIds: [...expandedTurnIds], expandedGroupIds: [...expandedGroupIds] };
}
