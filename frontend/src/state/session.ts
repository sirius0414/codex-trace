import type { Binding, ThreadSnapshot, ViewState, OpenResult, EventDetail, ContentPart } from '../../../contracts/types';
import type { DetailData } from '../ui-types';
import { sameBinding } from './binding.mjs';
import { mergeGroupCoverage } from './coverage.mjs';
export { sameBinding } from './binding.mjs';

/** 只持有可恢复的偏好和 ID；正文、来源路径及导出材料不进入 ViewState。 */
export function defaultView(): ViewState {
  return { filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null },
    agentScope: { kind: 'all', groupId: null }, overviewMode: 'duration', selectedEventId: null,
    detailTab: 'preview', expandedTurnIds: [], expandedGroupIds: [], follow: 'following', anchor: null };
}

export type SearchMatches = { contextKey: string; requestKey: string; ids: Set<string> };
const noSearchMatches = new Set<string>();
/** 条件变化建立新搜索上下文；同一上下文的分页/修订变化只刷新请求版本。 */
export function searchKeys(view: ViewState, snapshot: ThreadSnapshot | null) {
  const contextKey = JSON.stringify([view.filters, view.agentScope, snapshot?.binding ?? null, snapshot?.generation ?? null]);
  return { contextKey, requestKey: JSON.stringify([contextKey, snapshot?.revision,
    snapshot?.coverage.loadedSetId, snapshot?.coverage.scopeRevision]) };
}
/** 新查询先显示空结果，避免闪现旧查询结果；同条件更新保留上次已确认集合。 */
export function searchVisibility(text: string, keys: ReturnType<typeof searchKeys>, cached: SearchMatches | null) {
  if (!text) return { matchedIds: null, searching: false };
  if (!cached || cached.contextKey !== keys.contextKey) return { matchedIds: noSearchMatches, searching: true };
  return { matchedIds: cached.ids, searching: cached.requestKey !== keys.requestKey };
}

/** 观察会话与任务选择共用一道响应门禁，详情和偏好读取也必须经过它。 */
export class SessionGate {
  open: OpenResult | null = null;
  binding: Binding | null = null;
  activation = 0;
  epoch = 0;
  isDuplicateOpen(open: OpenResult) {
    return this.open?.sourceInstanceId === open.sourceInstanceId && this.open.viewSessionId === open.viewSessionId
      && JSON.stringify(this.open.identity) === JSON.stringify(open.identity);
  }
  begin(open: OpenResult) {
    this.activation += 1; this.open = open;
    this.epoch = open.identity.state === 'verified' ? open.identity.selectionEpoch : 0;
    this.binding = open.identity.state === 'verified' ? { schemaVersion: '1.0.0', sourceInstanceId: open.sourceInstanceId,
      viewSessionId: open.viewSessionId, selectionEpoch: this.epoch, threadId: open.identity.threadId } : null;
    return this.capture();
  }
  select(threadId: string) {
    if (!this.open) throw new Error('宿主观察会话尚未建立。');
    this.epoch += 1;
    this.binding = { schemaVersion: '1.0.0', sourceInstanceId: this.open.sourceInstanceId,
      viewSessionId: this.open.viewSessionId, selectionEpoch: this.epoch, threadId };
    return this.capture();
  }
  capture() { return { activation: this.activation, binding: this.binding ? { ...this.binding } : null }; }
  accepts(token: ReturnType<SessionGate['capture']>, binding = token.binding) {
    // 仅 threadId 相同仍可能是重开或重新选择后的旧响应，必须核对完整 Binding。
    return token.activation === this.activation && sameBinding(this.binding, token.binding) && sameBinding(this.binding, binding);
  }
  acceptsOpen(activation: number, viewId: string) { return activation === this.activation && viewId === this.open?.viewSessionId; }
  invalidate() { this.activation += 1; this.binding = null; }
}

/** 历史页可乱序到达：事件按自身版本合并，元信息按快照版本选择，加载范围另看 scopeRevision。 */
export function mergeSnapshot(current: ThreadSnapshot | null, incoming: ThreadSnapshot, removed: ReadonlyMap<string, number> = new Map()): ThreadSnapshot {
  if (!current || !sameBinding(current.binding, incoming.binding)) return incoming;
  if (incoming.generation < current.generation) return current;
  if (current.generation !== incoming.generation) return incoming;
  const base = incoming.revision > current.revision || incoming.revision === current.revision && incoming.coverage.scopeRevision >= current.coverage.scopeRevision ? incoming : current;
  const events = new Map(current.events.map(event => [event.eventId, event]));
  for (const event of incoming.events) {
    // 删除墓碑只拦截删除之前的页，后续合法的新版本仍可再次引入同一事件。
    if ((removed.get(event.eventId) ?? -1) >= incoming.revision) continue;
    const previous = events.get(event.eventId);
    if (!previous || event.eventRevision > previous.eventRevision) events.set(event.eventId, event);
  }
  const sorted = [...events.values()].sort((a, b) => (a.order.section === b.order.section ? 0 : a.order.section === 'context' ? -1 : 1)
    || a.order.ordinal - b.order.ordinal || a.eventId.localeCompare(b.eventId));
  const scope = incoming.coverage.scopeRevision > current.coverage.scopeRevision ? incoming
    : incoming.coverage.scopeRevision < current.coverage.scopeRevision ? current : base;
  const coverage = scope.coverage;
  const scopeGroups = scope.groups;
  return { ...base, events: sorted, coverage, groups: mergeGroupCoverage(base.groups, scopeGroups) };
}

/** 调用方先校验绑定、generation 和事件版本；分块偏移使用 UTF-8 字节数。
 * 已有该 part 正文时，只接受紧接其末尾的追加块。
 */
export function mergeDetail(current: DetailData | null, response: EventDetail, append: boolean): DetailData {
  const result: DetailData = current?.eventId === response.eventId ? { ...current, parts: { ...current.parts } }
    : { eventId: response.eventId, summary: response.summary, parts: {}, explanation: response.explanation };
  result.summary = response.summary; result.explanation = response.explanation;
  for (const chunk of response.chunks) {
    const previous = result.parts[chunk.part];
    // 重试同一页时偏移不再等于末尾，跳过它，避免把同一批字节追加两次。
    const previousBytes = previous ? new TextEncoder().encode(previous.text).length : 0;
    const prefix = append && previous && chunk.byteOffset === previousBytes ? previous.text : '';
    if (append && previous && chunk.byteOffset !== previousBytes) continue;
    result.parts[chunk.part] = { text: prefix + chunk.text, nextCursor: chunk.nextCursor,
      completeness: chunk.completeness, sourceTruncated: chunk.sourceTruncated,
      readableBytes: chunk.readableBytes, ...(chunk.reader ? { reader: chunk.reader } : {}) };
  }
  return result;
}

/** 首次使用展开已知轮次；已保存的显式折叠状态优先，不因重开而覆盖。 */
export function restoredView(state: ViewState, revision: number, snapshot: ThreadSnapshot): ViewState {
  return revision === 0 ? { ...state, expandedTurnIds: snapshot.turns.map(turn => turn.turnId) } : state;
}

export type EventLocation = { anchorEventId: string | null; groupId?: string };
/** 优先使用事件的已知分组，事件尚未加载时保留锚点自带的分组定位线索。 */
export function eventLocation(view: ViewState, snapshot: ThreadSnapshot | null, eventId = view.anchor?.eventId ?? view.selectedEventId): EventLocation {
  if (!eventId) return { anchorEventId: null };
  const groupId = snapshot?.events.find(event => event.eventId === eventId)?.groupId
    ?? (view.anchor?.eventId === eventId ? view.anchor.groupId : null);
  return { anchorEventId: eventId, ...(groupId ? { groupId } : {}) };
}
export function isNotFound(failure: unknown): boolean {
  return !!failure && typeof failure === 'object' && 'code' in failure && failure.code === 'notFound';
}
/** 锚点或分组已不可定位时逐级回退，避免重建永远卡在失效定位上；其他错误继续上抛。 */
export async function readAnchorPage(read: (location: EventLocation) => Promise<ThreadSnapshot>, location: EventLocation) {
  try { return { snapshot: await read(location), missing: false }; }
  catch (failure) {
    if (!location.anchorEventId || !isNotFound(failure)) throw failure;
    try { return { snapshot: await read({ ...location, anchorEventId: null }), missing: true }; }
    catch (fallbackFailure) {
      if (!location.groupId || !isNotFound(fallbackFailure)) throw fallbackFailure;
      return { snapshot: await read({ anchorEventId: null }), missing: true };
    }
  }
}
export async function readSelectionPage(read: (location: EventLocation) => Promise<ThreadSnapshot>, location: EventLocation, current: ThreadSnapshot) {
  try {
    const snapshot = mergeSnapshot(current, await read(location));
    return { snapshot, missing: !snapshot.events.some(event => event.eventId === location.anchorEventId) };
  } catch (failure) {
    if (!isNotFound(failure)) throw failure;
    return { snapshot: current, missing: true };
  }
}

/** 定位详情时展开其轮次和祖先创建路径，防环集合也保护不完整的关系数据。 */
export function selectedView(view: ViewState, snapshot: ThreadSnapshot | null, eventId: string | null): ViewState {
  if (!eventId) return { ...view, selectedEventId: null };
  const event = snapshot?.events.find(value => value.eventId === eventId);
  const expandedTurnIds = new Set(view.expandedTurnIds);
  const expandedGroupIds = new Set(view.expandedGroupIds);
  if (event?.turnId) expandedTurnIds.add(event.turnId);
  let group = snapshot?.groups.find(value => value.groupId === event?.groupId);
  const visited = new Set<string>();
  while (group && !visited.has(group.groupId)) {
    visited.add(group.groupId); expandedGroupIds.add(group.groupId);
    const creation = snapshot?.events.find(value => value.eventId === group?.creationEventId);
    if (creation?.turnId) expandedTurnIds.add(creation.turnId);
    group = snapshot?.groups.find(value => value.groupId === group?.parentGroupId);
  }
  return { ...view, selectedEventId: eventId, detailTab: 'preview', follow: 'paused',
    expandedTurnIds: [...expandedTurnIds], expandedGroupIds: [...expandedGroupIds] };
}

export function partsForTab(tab: ViewState['detailTab'], available: ContentPart[]): ContentPart[] {
  if (tab === 'summary') return ['summary'];
  if (tab === 'raw') return ['raw'];
  if (tab === 'source') return ['source'];
  const parts = available.filter(part => ['input', 'output', 'error'].includes(part));
  return parts.length ? parts : ['summary'];
}

/** 每次最多一个 CAS 保存；冲突后暂停自动保存，等待显式重读，避免覆盖另一窗口。 */
export class PreferenceGate {
  revision = 0;
  blocked = false;
  loaded = false;
  pending: ViewState | null = null;
  saving = false;
  editVersion = 0;
  reset() { this.revision = 0; this.blocked = false; this.loaded = false; this.pending = null; this.saving = false; this.editVersion = 0; }
  edit(state: ViewState) { this.editVersion += 1; this.pending = state; }
  restored(revision: number) {
    this.loaded = true;
    if (revision < this.revision) return false;
    this.revision = revision; return true;
  }
  take() {
    if (this.saving || !this.loaded || this.blocked || !this.pending) return null;
    const request = { expectedPreferenceRevision: this.revision, state: this.pending };
    // 取走本次快照后，保存期间的新编辑仍可独立进入 pending。
    this.pending = null; this.saving = true; return request;
  }
  finish(result: { saved: boolean; preferenceRevision: number }) {
    this.saving = false; this.revision = result.preferenceRevision;
    if (!result.saved) { this.blocked = true; this.pending = null; }
  }
}
