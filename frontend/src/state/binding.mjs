/** 完整观察身份；相同任务在不同来源、重开会话或选择代次下的响应也不能混用。 */
export function sameBinding(a, b) {
  return !!a && !!b && a.schemaVersion === b.schemaVersion && a.sourceInstanceId === b.sourceInstanceId
    && a.viewSessionId === b.viewSessionId && a.selectionEpoch === b.selectionEpoch && a.threadId === b.threadId;
}
