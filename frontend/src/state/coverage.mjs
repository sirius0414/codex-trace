/** Group paging belongs to the loaded scope, independently of event revisions. */
export function mergeGroupCoverage(baseGroups, scopeGroups) {
  const byId = new Map(scopeGroups.map(group => [group.groupId, group]));
  return baseGroups.map(group => {
    const scope = byId.get(group.groupId);
    if (!scope) return group;
    return { ...group, loadedEventCount: scope.loadedEventCount, loadedToolCallCount: scope.loadedToolCallCount,
      ...('hasEarlier' in scope ? { hasEarlier: scope.hasEarlier } : {}),
      ...('nextCursor' in scope ? { nextCursor: scope.nextCursor } : {}) };
  });
}
