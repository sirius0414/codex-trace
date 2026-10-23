/** Exact observation identity shared by every asynchronous read channel. */
export function sameBinding(a, b) {
  return !!a && !!b && a.schemaVersion === b.schemaVersion && a.sourceInstanceId === b.sourceInstanceId
    && a.viewSessionId === b.viewSessionId && a.selectionEpoch === b.selectionEpoch && a.threadId === b.threadId;
}
