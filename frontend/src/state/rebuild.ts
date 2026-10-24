import type { ChangeBatch, ReadChangesRequest, ThreadSnapshot, ViewState } from '../../../contracts/types';
import { ChangeAssembly, ChangeProtocolError, mergeChanges } from './changes.ts';
import { eventLocation, mergeSnapshot, readAnchorPage, readSelectionPage, sameBinding, type EventLocation } from './session.ts';

export type RebuildPageRequest = { cursor: string | null; anchorEventId: string | null; groupId?: string };

/** 只收集本次重新读取的页面，全部补齐并追平增量后才交给调用方一次提交。
 * 同代次的游标过期不代表已加载历史失效；既不能只留一页，也不能混入旧快照中的已删记录。
 */
export async function rebuildLoadedSnapshot(
  before: ThreadSnapshot,
  view: ViewState,
  readPage: (request: RebuildPageRequest) => Promise<ThreadSnapshot>,
  readChanges: (request: ReadChangesRequest) => Promise<ChangeBatch>,
) {
  let requests = 0, items = 0;
  let generation: number | undefined;
  let earliestRevision = Infinity, latestRevision = -Infinity;
  const reserve = () => { if (++requests > 1024) throw new ChangeProtocolError(); };
  const count = (length: number) => { if ((items += length) > 100000) throw new ChangeProtocolError(); };
  const fresh = async (request: RebuildPageRequest) => {
    reserve();
    const page = await readPage(request);
    if (!sameBinding(page.binding, before.binding) || !Number.isSafeInteger(page.generation)
      || page.generation < before.generation || generation !== undefined && page.generation !== generation
      || !Number.isSafeInteger(page.revision) || page.revision < 0 || !Array.isArray(page.events)
      || page.rootGroupId !== before.rootGroupId) throw new ChangeProtocolError();
    generation ??= page.generation;
    count(page.events.length);
    earliestRevision = Math.min(earliestRevision, page.revision);
    latestRevision = Math.max(latestRevision, page.revision);
    return page;
  };
  const root = await fresh({ cursor: null, anchorEventId: null });
  let rebuilt = root;
  const merge = (page: ThreadSnapshot) => { rebuilt = mergeSnapshot(rebuilt, page); };
  const readableGroups = () => new Set(rebuilt.groups.filter(group => group.groupId === rebuilt.rootGroupId
    || rebuilt.relationships.some(relation => relation.relationshipId === group.relationshipId
      && relation.type === 'spawned-by' && relation.resolution === 'confirmed' && relation.fromThreadId === group.threadId))
    .map(group => group.groupId));
  if (root.generation === before.generation) {
    const oldByGroup = new Map<string, Set<string>>();
    for (const event of before.events) {
      const ids = oldByGroup.get(event.groupId) ?? new Set<string>();
      ids.add(event.eventId); oldByGroup.set(event.groupId, ids);
    }
    const latestPages = new Map<string, ThreadSnapshot>([[root.rootGroupId, root]]);
    // 先恢复各已加载分组的最新页；子任务锚点不能取代主任务自己的最新页。
    for (const group of root.groups) {
      if (group.groupId === root.rootGroupId || !oldByGroup.has(group.groupId) || !readableGroups().has(group.groupId)) continue;
      const page = await fresh({ cursor: null, anchorEventId: null, groupId: group.groupId });
      merge(page); latestPages.set(group.groupId, page);
    }
    for (const [groupId, latest] of latestPages) {
      if (!readableGroups().has(groupId)) continue;
      const missing = new Set(oldByGroup.get(groupId) ?? []);
      for (const event of rebuilt.events) missing.delete(event.eventId);
      let page = latest;
      const cursors = new Set<string>();
      while (missing.size) {
        const scope = groupId === root.rootGroupId ? page.coverage : page.groups.find(group => group.groupId === groupId);
        if (!scope) throw new ChangeProtocolError();
        if (scope.hasEarlier === false) break;
        if (scope.hasEarlier !== true) throw new ChangeProtocolError();
        const cursor = scope.nextCursor;
        if (typeof cursor !== 'string' || !cursor || cursors.has(cursor)) throw new ChangeProtocolError();
        cursors.add(cursor);
        page = await fresh({ cursor, anchorEventId: null, ...(groupId === root.rootGroupId ? {} : { groupId }) });
        merge(page);
        for (const event of page.events) missing.delete(event.eventId);
        // 已删除的旧 ID 不会再次出现；到来源末尾即可确认消失，绝不从 before 补回。
      }
    }
  }
  const location = eventLocation(view, before), anchorId = location.anchorEventId;
  const readLocated = (target: EventLocation) => fresh({ cursor: null, ...target });
  // 关联已撤销的子任务不再尝试定位；保留主任务的新集合，并由 missing 标志说明阅读目标消失。
  const readableLocation = (target: EventLocation) => !target.groupId || readableGroups().has(target.groupId);
  if (anchorId && readableLocation(location) && !rebuilt.events.some(event => event.eventId === anchorId)) {
    const located = await readAnchorPage(readLocated, location);
    merge(located.snapshot);
  }
  const selected = view.selectedEventId;
  const selectedLocation = eventLocation(view, before, selected);
  if (selected && selected !== anchorId && readableLocation(selectedLocation) && !rebuilt.events.some(event => event.eventId === selected)) {
    const located = await readSelectionPage(readLocated, selectedLocation, rebuilt);
    rebuilt = located.snapshot;
  }
  // 分页期间来源仍可更新：从最早页面的 revision 补增量，才能删除在较旧页面里读到的记录。
  // 续页共享同一个基线，全部收齐前不向外暴露任何页面或半个增量批次。
  const request: ReadChangesRequest = { binding: before.binding, generation: root.generation,
    afterRevision: earliestRevision, continuationCursor: null };
  const assembly = new ChangeAssembly({ ...request });
  for (;;) {
    reserve();
    const page = await readChanges({ ...request });
    const state = assembly.append(page);
    if (state === 'reset') throw new ChangeProtocolError();
    count(page.upsertEvents.length + page.removeEventIds.length);
    if (state === 'more') { request.continuationCursor = page.continuationCursor; continue; }
    const batch = assembly.finish();
    if (batch.revision < latestRevision) throw new ChangeProtocolError();
    rebuilt = mergeChanges(rebuilt, batch);
    const readable = readableGroups();
    rebuilt = { ...rebuilt, events: rebuilt.events.filter(event => readable.has(event.groupId)) };
    return { snapshot: rebuilt, confirmedRevision: batch.revision,
      missingAnchor: Boolean(anchorId && !rebuilt.events.some(event => event.eventId === anchorId)),
      missingSelection: Boolean(selected && !rebuilt.events.some(event => event.eventId === selected)) };
  }
}
