/** 分组分页属于已加载范围；仅覆写范围字段，避免新游标覆盖其他版本的分组状态/用量。 */
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
