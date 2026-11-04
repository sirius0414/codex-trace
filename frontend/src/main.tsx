import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { ChangeBatch, ContentPart, DownloadResult, EventDetail, ListThreadsResult, OpenResult, QueryResult, ReadViewStateResult,
  SaveViewStateResult, ThreadSnapshot, ThreadSummary, ViewState } from '../../contracts/types';
import { TrajectoryClient } from './bridge/client';
import { defaultView, eventLocation, mergeDetail, mergeSnapshot, partsForTab, readAnchorPage, readSelectionPage, restoredView, searchKeys, searchVisibility, selectedView, PreferenceGate, SessionGate, type EventLocation, type SearchMatches } from './state/session';
import { ChangeProtocolError, ChangeReader, mergeChanges, recordChangeTiming, type ChangeTiming } from './state/changes';
import { DetailReadWindows, readDetailWindow } from './state/detail';
import { rebuildLoadedSnapshot } from './state/rebuild';
import { ExportCoordinator } from './state/export';
import { GroupReader } from './state/groups';
import { TrackingGate } from './state/tracking';
import TraceUI from './components/TraceUI';
import type { DetailData } from './ui-types';
import { PreferencesProvider, usePreferences } from './preferences';
import { localizeClientError } from './ui-preferences';
import './style.css';

type Token = ReturnType<SessionGate['capture']>;
// 保留双语提示，在渲染时选取语言，避免切换语言重建连接或丢失已有反馈。
type AppMessage = string | { zh: string; en: string };
const translated = (zh: string, en: string): AppMessage => ({ zh, en });
const displayMessage = (message: AppMessage, language: 'zh' | 'en') => typeof message === 'string' ? localizeClientError(message, language) : message[language];

/** 连接宿主、读取状态和展示组件；异步结果写入界面前核对对应会话、选择及请求代次。 */
function TrajectoryApp() {
  const { language } = usePreferences();
  const [snapshot, setSnapshot] = useState<ThreadSnapshot | null>(null);
  const [view, setView] = useState<ViewState>(defaultView);
  const [trackingPaused, setTrackingPaused] = useState(false);
  const tracking = useRef(new TrackingGate());
  const [detail, setDetail] = useState<DetailData | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [threadsMore, setThreadsMore] = useState(false);
  const [threadScope, setThreadScope] = useState<'user' | 'all'>('user');
  const threadScopeRef = useRef<'user' | 'all'>('user');
  const [searchResult, setSearchResult] = useState<SearchMatches | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<AppMessage>('');
  const [exportMessage, setExportMessage] = useState<AppMessage>('');
  const [exporting, setExporting] = useState(false);
  const exports = useRef<ExportCoordinator | null>(null);
  const client = useRef<TrajectoryClient | null>(null);
  const gate = useRef(new SessionGate());
  const preferences = useRef(new PreferenceGate());
  // 异步回调通过 ref 读取最新值，避免使用发起请求时的 React 闭包快照。
  // 写入时同时更新 ref 与 state，保证同一轮回调里的后续请求也能看到新状态。
  const currentView = useRef(view);
  const currentSnapshot = useRef(snapshot);
  const currentDetail = useRef(detail);
  const detailRequests = useRef(new Set<string>());
  const detailSerial = useRef(0);
  const detailVersions = useRef(new Map<string, number>());
  const detailWindows = useRef(new DetailReadWindows());
  const threadSerial = useRef(0);
  const threadCursor = useRef<string | null>(null);
  const threadQuery = useRef('');
  const querySerial = useRef(0);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);
  const selectionIntent = useRef(0);
  const saveOperations = useRef(new Map<PreferenceGate, Promise<void>>());
  const changes = useRef<ChangeReader | null>(null);
  const groupReads = useRef<GroupReader | null>(null);
  const confirmedRevision = useRef<number | null>(null);
  const removedEvents = useRef(new Map<string, number>());
  const selectionAbort = useRef(new AbortController());
  const partRequests = useRef(new Map<ContentPart, { revision: number; controller: AbortController }>());
  const transportError = useRef<AppMessage>('');
  const pendingTiming = useRef<ChangeTiming | null>(null);

  function showError(value: unknown, token?: Token) {
    if (mounted.current && (!token || gate.current.accepts(token))) setError(value instanceof Error ? value.message : translated('本地读取未完成，请重试。', 'The local read did not complete. Please retry.'));
  }
  function applyView(next: ViewState) { currentView.current = next; setView(next); }
  function applySnapshot(next: ThreadSnapshot) {
    if (tracking.current.paused) return;
    const current = currentSnapshot.current;
    if (current && next.generation > current.generation) {
      // generation 变化代表来源已重建，旧删除标记和详情游标不能跨代沿用。
      removedEvents.current.clear(); confirmedRevision.current = next.revision;
      invalidateDetails();
    }
    const merged = mergeSnapshot(current, next, removedEvents.current);
    currentSnapshot.current = merged; setSnapshot(merged);
  }
  function invalidateDetails() {
    detailSerial.current += 1;
    for (const entry of partRequests.current.values()) entry.controller.abort();
    partRequests.current.clear(); detailRequests.current.clear(); detailVersions.current.clear(); setDetailLoading(false);
  }
  function stopReads() {
    // 停止旧界面的读取不会撤销后端已接受的导出保存，保存结果仍需用原请求核对。
    exports.current?.bind(null);
    groupReads.current?.cancel();
    changes.current?.pause(); confirmedRevision.current = null;
    selectionAbort.current.abort(); selectionAbort.current = new AbortController();
    invalidateDetails();
  }
  function resetSelection() {
    detailWindows.current.clear();
    preferences.current = new PreferenceGate();
    currentSnapshot.current = null; setSnapshot(null);
    currentDetail.current = null; setDetail(null);
    applyView(defaultView()); setSearchResult(null); setError(''); setExportMessage('');
    invalidateDetails(); removedEvents.current.clear(); transportError.current = ''; pendingTiming.current = null;
    tracking.current.setPaused(false); setTrackingPaused(false);
    groupReads.current?.setVisible(document.visibilityState !== 'hidden');
  }

  /** 同一个偏好对象串行执行 CAS 保存；切换任务后旧对象不能回写新任务状态。 */
  async function flushPreferences(token: Token, owner = preferences.current): Promise<void> {
    const existing = saveOperations.current.get(owner);
    if (existing) { await existing; return flushPreferences(token, owner); }
    if (!gate.current.accepts(token) || owner !== preferences.current || !client.current) return;
    const adapter = client.current;
    const pending = owner.take();
    if (!pending) return;
    let succeeded = false;
    const operation = (async () => {
    try {
      const result = await adapter.call<SaveViewStateResult>('save_view_state', { binding: token.binding, ...pending });
      if (!gate.current.accepts(token, result.binding) || owner !== preferences.current) return;
      owner.finish(result);
      succeeded = result.saved;
      if (!result.saved) setError(translated('另一窗口已更新阅读状态；已保留本窗口当前视图，点击重试可重新读取。', 'Another window updated the reading state. This window’s current view is preserved. Click Retry to reload it.'));
    } catch (failure) {
      owner.saving = false;
      // 保留保存期间产生的最新编辑，交给后续编辑或显式重试，避免盲目重试 CAS。
      owner.pending ??= pending.state;
      showError(failure, token);
    }
    })();
    saveOperations.current.set(owner, operation);
    await operation;
    saveOperations.current.delete(owner);
    // 只有已确认保存成功才能继续排空；传输错误不触发无界自动重试。
    if (succeeded && owner.pending) await flushPreferences(token, owner);
  }
  function patchView(patch: Partial<ViewState>) {
    const next = { ...currentView.current, ...patch };
    applyView(next); preferences.current.edit(next);
    if (saveTimer.current) clearTimeout(saveTimer.current);
    const token = gate.current.capture();
    saveTimer.current = setTimeout(() => { void flushPreferences(token); }, 400);
  }
  function pauseTracking(paused: boolean) {
    const wasPaused = tracking.current.paused;
    tracking.current.setPaused(paused); setTrackingPaused(paused);
    patchView({ follow: paused ? 'paused' : 'following' });
    if (!paused && !wasPaused) { changes.current?.retry(); return; }
    // 先同步使在途快照失效，再停调度；只隐藏按钮或等待 effect 会漏收最后一个响应。
    if (paused) {
      groupReads.current?.setVisible(false);
      changes.current?.pause(); invalidateDetails(); setLoading(false);
    } else {
      if (currentSnapshot.current && confirmedRevision.current !== null) changes.current?.start();
      else void readSnapshot(gate.current.capture(), null, true);
      // 子任务队列若开始补读，会暂停增量读取；不能在其后重新 start 造成并发。
      groupReads.current?.setVisible(document.visibilityState !== 'hidden');
    }
  }
  function selectEvent(eventId: string | null) {
    const wasSelected = currentView.current.selectedEventId === eventId;
    patchView(selectedView(currentView.current, currentSnapshot.current, eventId));
    // 创建来源可能早于已加载页；页面提供的分组锚点可复用现有分页门禁补读。
    if (eventId && !currentSnapshot.current?.events.some(event => event.eventId === eventId)) {
      const location = eventLocation(currentView.current, currentSnapshot.current, eventId);
      patchView({ anchor: { eventId, offsetPx: 0, groupId: location.groupId ?? null } });
      void readSnapshot(gate.current.capture());
    }
    // 重复选择同一行时保留已加载详情及其滚动位置。
    if (wasSelected) return;
    invalidateDetails();
    currentDetail.current = null; setDetail(null); setDetailLoading(false);
  }

  /** 首屏/重试先恢复偏好与锚点，再开放增量读取；历史分页只扩展当前加载范围。 */
  async function readSnapshot(token: Token, cursor: string | null = null, restore = false, groupId: string | null = null) {
    if (!token.binding || !client.current || tracking.current.paused) return;
    const adapter = client.current;
    const signal = AbortSignal.any([selectionAbort.current.signal, tracking.current.signal]);
    const previousConfirmed = confirmedRevision.current;
    // 分页也会改变服务端增量范围；先取消进行中的重建，避免它随后覆盖新加载的历史。
    changes.current?.pause();
    if (restore) confirmedRevision.current = null;
    setLoading(true);
    const owner = preferences.current;
    const edited = owner.editVersion;
    const locatingSelection = !cursor && !restore && currentView.current.selectedEventId
      && !currentSnapshot.current?.events.some(event => event.eventId === currentView.current.selectedEventId)
      ? currentView.current.selectedEventId : null;
    let first: ThreadSnapshot | null = null;
    let initialRevision: number | null = null;
    let missingAnchor = false, missingSelection = false;
    const readLocated = async (location: EventLocation) => {
      const result = await adapter.call<ThreadSnapshot>('read_trajectory', { binding: token.binding, cursor: null, ...location }, { signal });
      if (signal.aborted || !gate.current.accepts(token, result.binding)) throw new DOMException('Cancelled', 'AbortError');
      return result;
    };
    try {
      const initial = cursor ? { snapshot: await adapter.call<ThreadSnapshot>('read_trajectory', { binding: token.binding, cursor,
        anchorEventId: null, ...(groupId ? { groupId } : {}) }, { signal }), missing: false }
        : groupId ? { snapshot: await readLocated({ anchorEventId: null, groupId }), missing: false }
          : await readAnchorPage(readLocated, eventLocation(currentView.current, currentSnapshot.current));
      const result = initial.snapshot; missingAnchor = initial.missing;
      if (signal.aborted || !gate.current.accepts(token, result.binding)) return;
      if (cursor && currentSnapshot.current && result.generation !== currentSnapshot.current.generation) {
        // 一页历史不足以确认新一代的完整状态，交给串行增量读取器围绕阅读锚点重建。
        changes.current?.retry(); return;
      }
      first = result;
      initialRevision = result.revision;
      if (restore) {
        // 挂载列表前恢复偏好，避免默认的滚动到底部覆盖持久化阅读锚点。
        const saved = await adapter.call<ReadViewStateResult>('read_view_state', { binding: token.binding }, { signal });
        if (signal.aborted || !gate.current.accepts(token, saved.binding) || owner !== preferences.current) return;
        const accepted = owner.restored(saved.preferenceRevision);
        if (accepted && owner.editVersion === edited && !owner.pending) applyView(restoredView(saved.state, saved.preferenceRevision, result));
        const anchorId = currentView.current.anchor?.eventId ?? currentView.current.selectedEventId;
        if (anchorId && !result.events.some(event => event.eventId === anchorId)) {
          const located = await readAnchorPage(readLocated, eventLocation(currentView.current, currentSnapshot.current ?? result, anchorId));
          const anchored = located.snapshot; missingAnchor ||= located.missing;
          if (signal.aborted || !gate.current.accepts(token, anchored.binding)) return;
          if (anchored.generation > result.generation) initialRevision = anchored.revision;
          first = mergeSnapshot(result, anchored);
        }
        const selected = currentView.current.selectedEventId;
        if (selected && !first.events.some(event => event.eventId === selected)) {
          if (selected === anchorId && missingAnchor) missingSelection = true;
          else {
            const selectedPage = await readSelectionPage(readLocated, eventLocation(currentView.current, currentSnapshot.current ?? first, selected), first);
            if (selectedPage.snapshot.generation > first.generation) initialRevision = selectedPage.snapshot.revision;
            first = selectedPage.snapshot; missingSelection = selectedPage.missing;
          }
        }
      }
      applySnapshot(first);
      if (locatingSelection && currentView.current.selectedEventId === locatingSelection
        && currentSnapshot.current?.events.some(event => event.eventId === locatingSelection)) {
        patchView(selectedView(currentView.current, currentSnapshot.current, locatingSelection));
      }
      setError(missingSelection ? translated('未找到所选记录；已保留选择和阅读状态，可选择其他记录。', 'The selected record was not found. Your selection and reading state are preserved; you can choose another record.')
        : missingAnchor ? translated('未找到原阅读锚点；已显示最近记录并保留筛选和跟随状态。', 'The previous reading position was not found. Recent records are shown, with filters and follow state preserved.') : '');
      if (restore && owner.pending) void flushPreferences(token, owner);
    } catch (failure) {
      if (!signal.aborted && first && gate.current.accepts(token)) {
        // 偏好读取失败也保留已成功读取的轨迹，读取状态和内容可用性分别处理。
        if (!owner.loaded && owner.editVersion === edited) applyView(restoredView(currentView.current, 0, first));
        applySnapshot(first);
      }
      if (!signal.aborted) showError(failure, token);
    }
    finally {
      if (!signal.aborted && gate.current.accepts(token)) {
        setLoading(false);
        if (first && confirmedRevision.current === null) {
          confirmedRevision.current = initialRevision ?? first.revision; changes.current?.start();
        } else if (restore && !first && previousConfirmed !== null && currentSnapshot.current) {
          confirmedRevision.current = previousConfirmed; changes.current?.start();
        } else if (confirmedRevision.current !== null && currentSnapshot.current) changes.current?.start();
      }
    }
  }

  async function selectTask(threadId: string) {
    // 保存旧偏好期间用户可能再次切换，只有最后一次选择意图可以创建新绑定。
    const intent = ++selectionIntent.current;
    stopReads();
    const old = gate.current.capture();
    await flushPreferences(old);
    if (intent !== selectionIntent.current || !gate.current.accepts(old) && old.binding) return;
    const token = gate.current.select(threadId);
    resetSelection(); void readSnapshot(token, null, true);
  }
  async function listThreads(append = false) {
    const open = gate.current.open;
    if (!open || !client.current) return;
    const activation = gate.current.activation, serial = ++threadSerial.current;
    // 列表尚未绑定具体任务，用打开会话及请求序号排除旧搜索/旧分页响应。
    const cursor = append ? threadCursor.current : null;
    if (append && !cursor) return;
    try {
      const result = await client.current.call<ListThreadsResult>('list_threads', { viewSessionId: open.viewSessionId,
        query: threadQuery.current, sourceScope: threadScopeRef.current, cursor });
      if (!gate.current.acceptsOpen(activation, result.viewSessionId) || serial !== threadSerial.current) return;
      setThreads(previous => append ? [...new Map([...previous, ...result.threads].map(row => [row.threadId, row])).values()] : result.threads);
      threadCursor.current = result.nextCursor; setThreadsMore(!!result.nextCursor);
    } catch (failure) { if (gate.current.acceptsOpen(activation, open.viewSessionId)) showError(failure); }
  }
  function changeThreadScope(scope: 'user' | 'all') {
    threadScopeRef.current = scope; setThreadScope(scope); threadCursor.current = null; ++threadSerial.current;
    setThreads([]); setThreadsMore(false); void listThreads();
  }
  function searchTasks(query: string) {
    threadQuery.current = query; threadCursor.current = null; ++threadSerial.current;
    if (listTimer.current) clearTimeout(listTimer.current);
    listTimer.current = setTimeout(() => { void listThreads(); }, 200);
  }

  async function loadPart(part: ContentPart, append = false, readAll = false) {
    const token = gate.current.capture(), state = currentSnapshot.current, selected = currentView.current.selectedEventId;
    if (!token.binding || !state || !selected || !client.current) return;
    const event = state.events.find(row => row.eventId === selected);
    if (!event) {
      setError(translated('所选记录不在当前加载范围中；可加载较早记录。', 'The selected record is outside the loaded range. You can load earlier records.')); return;
    }
    const previous = currentDetail.current?.eventId === selected ? currentDetail.current.parts[part] : undefined;
    const versionKey = `${selected}:${part}`;
    const windowKey = JSON.stringify([state.generation, selected, part]);
    // 详情游标绑定冻结版本；事件更新后先刷新当前已显示窗口，再继续读取新字节。
    if (append && detailVersions.current.get(versionKey) !== event.eventRevision) append = false;
    const cursor = append ? previous?.nextCursor : null;
    if (append && !cursor || !readAll && !append && previous && detailVersions.current.get(versionKey) === event.eventRevision) return;
    const requestedBytes = readAll ? Math.min(8_000_000, event.content.find(ref => ref.part === part)?.readableBytes ?? previous?.readableBytes ?? 0) : 0;
    const serial = detailSerial.current, requestKey = `${serial}:${state.generation}:${event.eventRevision}:${selected}:${part}:${cursor ?? ''}`;
    if (detailRequests.current.has(requestKey)) return;
    const pending = partRequests.current.get(part);
    if (pending && pending.revision !== event.eventRevision) pending.controller.abort();
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, selectionAbort.current.signal]);
    partRequests.current.set(part, { revision: event.eventRevision, controller });
    detailRequests.current.add(requestKey); setDetailLoading(true);
    try {
      const adapter = client.current;
      const read = (chunkCursor: string | null) => adapter.call<EventDetail>('read_event', { binding: token.binding,
        generation: state.generation, eventId: selected, part, chunkCursor }, { signal });
      const response = append ? await read(cursor ?? null) : await readDetailWindow(read, Math.max(requestedBytes, detailWindows.current.get(windowKey), previous ? new TextEncoder().encode(previous.text).length : 0));
      // 选择、来源代次或事件版本任一过期，都不能把旧正文拼入当前详情。
      if (signal.aborted || !gate.current.accepts(token, response.binding) || serial !== detailSerial.current || currentView.current.selectedEventId !== response.eventId
        || response.generation !== currentSnapshot.current?.generation) return;
      const currentEvent = currentSnapshot.current.events.find(row => row.eventId === selected);
      if (currentEvent && response.eventRevision < currentEvent.eventRevision) return;
      const next = mergeDetail(currentDetail.current, response, append);
      if (next.parts[part]) detailWindows.current.remember(windowKey, next.parts[part]!.text);
      detailVersions.current.set(versionKey, response.eventRevision);
      currentDetail.current = next; setDetail(next);
    } catch (failure) { if (!signal.aborted && serial === detailSerial.current) showError(failure, token); }
    finally {
      detailRequests.current.delete(requestKey);
      if (partRequests.current.get(part)?.controller === controller) partRequests.current.delete(part);
      if (serial === detailSerial.current) setDetailLoading(detailRequests.current.size > 0);
    }
  }

  useEffect(() => {
    mounted.current = true;
    const reader = new ChangeReader({
      capture: () => {
        // 跟踪暂停由 reader.pause() 取消请求；身份门禁保持有效，才能继续尚未完成的重建。
        const state = currentSnapshot.current, token = gate.current.capture(), signal = selectionAbort.current.signal;
        if (!state || confirmedRevision.current === null || !token.binding || tracking.current.paused) return null;
        return { snapshot: state, afterRevision: confirmedRevision.current,
          isCurrent: () => mounted.current && !signal.aborted && gate.current.accepts(token) && currentSnapshot.current?.generation === state.generation };
      },
      read: (request, signal) => adapter.call<ChangeBatch>('read_changes', { ...request }, { signal }),
      received: timing => recordChangeTiming('received', timing),
      commit: (batch, timing) => {
        const state = currentSnapshot.current;
        if (!state || tracking.current.paused) return;
        const next = mergeChanges(state, batch);
        // 墓碑保留删除发生的 revision，阻止稍晚返回的历史页把已删事件加回来。
        for (const id of batch.removeEventIds) removedEvents.current.set(id, batch.revision);
        for (const event of batch.upsertEvents) removedEvents.current.delete(event.eventId);
        confirmedRevision.current = batch.revision;
        currentSnapshot.current = next; pendingTiming.current = timing; setSnapshot(next);
        recordChangeTiming('committed', timing);
        const recoveredError = transportError.current;
        setError(previous => previous === recoveredError ? '' : previous); transportError.current = '';
        if (currentView.current.selectedEventId && batch.removeEventIds.includes(currentView.current.selectedEventId)) {
          invalidateDetails(); setError(translated('所选记录已从当前来源移除；保留阅读位置，可选择其他记录。', 'The selected record was removed from the current source. Your reading position is preserved; you can choose another record.'));
        }
      },
      reset: async (_batch, signal) => {
        // 单个锚点页不能代表完整已加载范围，尤其子任务锚点页不包含主任务记录。
        // 全部新页及增量追赶成功后才替换界面；中途失败保留原内容和阅读意图。
        const token = gate.current.capture(), before = currentSnapshot.current;
        if (!token.binding || !before) return;
        const rebuilt = await rebuildLoadedSnapshot(before, currentView.current, async target => {
          const result = await adapter.call<ThreadSnapshot>('read_trajectory', { binding: token.binding, ...target }, { signal });
          if (signal.aborted || !gate.current.accepts(token, result.binding)) throw new DOMException('Cancelled', 'AbortError');
          return result;
        }, async request => {
          const result = await adapter.call<ChangeBatch>('read_changes', { ...request }, { signal });
          if (signal.aborted || !gate.current.accepts(token, result.binding)) throw new DOMException('Cancelled', 'AbortError');
          return result;
        });
        if (signal.aborted || !gate.current.accepts(token, rebuilt.snapshot.binding)) return;
        if (currentSnapshot.current !== before) {
          // 已在途的分组/历史页可能先提交；以扩展后的范围重试，不能用旧范围重建结果覆盖它。
          changes.current?.retry(); throw new ChangeProtocolError();
        }
        const selected = currentView.current.selectedEventId;
        const anchor = currentView.current.anchor?.eventId;
        invalidateDetails(); removedEvents.current.clear(); confirmedRevision.current = rebuilt.confirmedRevision;
        currentSnapshot.current = rebuilt.snapshot; setSnapshot(rebuilt.snapshot);
        const missing = selected && !rebuilt.snapshot.events.some(event => event.eventId === selected);
        const missingAnchor = anchor && !rebuilt.snapshot.events.some(event => event.eventId === anchor);
        setError(missing ? translated('重建后未找到所选记录；保留选择和阅读状态，可选择其他记录。', 'The selected record was not found after rebuilding. Your selection and reading state are preserved; you can choose another record.')
          : missingAnchor ? translated('重建后未找到原阅读锚点；已保留筛选、详情选择和跟随状态。', 'The previous reading position was not found after rebuilding. Filters, detail selection and follow state are preserved.') : '');
      },
      failed: failure => {
        const message = failure instanceof Error ? failure.message : translated('增量读取中断，已保留当前内容并等待重试。', 'Incremental reading stopped. Current content is preserved while waiting to retry.');
        const identityRequired = failure && typeof failure === 'object' && 'code' in failure && failure.code === 'identityRequired';
        transportError.current = identityRequired ? translated('观察会话已失效；请从当前聊天的 New Tab 重新打开“执行轨迹”。', 'The observation session has expired. Reopen “Execution trace” from New Tab in the current chat.') : message;
        setError(transportError.current);
        const state = currentSnapshot.current;
        if (state) {
          const next = { ...state, freshness: { ...state.freshness, state: 'stale' as const } };
          currentSnapshot.current = next; setSnapshot(next);
        }
        if (identityRequired) reader.pause();
      },
    });
    changes.current = reader;
    const visibility = () => {
      // 可见性恢复不能解除用户的显式暂停；导出保存有独立的生命周期。
      const visible = document.visibilityState !== 'hidden';
      reader.setVisible(visible); groupReads.current?.setVisible(visible && !tracking.current.paused); exports.current?.setVisible(visible);
    };
    visibility(); document.addEventListener('visibilitychange', visibility);
    const adapter = new TrajectoryClient((open: OpenResult) => {
      if (gate.current.isDuplicateOpen(open)) return;
      const intent = ++selectionIntent.current;
      stopReads();
      const old = gate.current.capture();
      void flushPreferences(old).then(() => {
        if (!mounted.current || intent !== selectionIntent.current) return;
        const token = gate.current.begin(open);
        resetSelection(); setThreads([]); threadCursor.current = null; setThreadsMore(false);
        if (open.identity.state === 'verified') void readSnapshot(token, null, true);
        else { setLoading(false); setError(open.identity.explanation); }
        void listThreads();
      });
    }, context => { document.documentElement.dataset.theme = context.theme === 'dark' ? 'dark' : 'light'; }, showError);
    client.current = adapter;
    const groups = new GroupReader({
      capture: () => {
        const state = currentSnapshot.current, token = gate.current.capture();
        const signal = AbortSignal.any([selectionAbort.current.signal, tracking.current.signal]);
        if (!state || !token.binding || confirmedRevision.current === null || tracking.current.paused) return null;
        return { snapshot: state, view: currentView.current,
          isCurrent: () => mounted.current && !signal.aborted && gate.current.accepts(token)
            && currentSnapshot.current?.generation === state.generation };
      },
      read: async (request, signal) => {
        // 直接读取会使后端增量游标失效；先完成有限的子任务队列，再继续增量分页。
        const token = gate.current.capture(); reader.pause();
        try { return await adapter.call<ThreadSnapshot>('read_trajectory', { ...request }, { signal }); }
        finally {
          if (mounted.current && !tracking.current.paused && confirmedRevision.current !== null && gate.current.accepts(token, request.binding)) reader.start();
        }
      },
      commit: applySnapshot,
      failed: showError,
      rebuild: () => changes.current?.retry(),
    });
    groupReads.current = groups; groups.setVisible(document.visibilityState !== 'hidden' && !tracking.current.paused);
    const exporter = new ExportCoordinator({
      call: (request, signal) => adapter.call<DownloadResult>('download_records', { ...request }, { signal }),
      onChange: feedback => { if (mounted.current) { setExportMessage({ zh: feedback.message, en: feedback.messageEn }); setExporting(feedback.busy); } },
    });
    exports.current = exporter; exporter.setVisible(document.visibilityState !== 'hidden');
    void adapter.connect().then(context => {
      document.documentElement.dataset.theme = context.theme === 'dark' ? 'dark' : 'light';
      if (!gate.current.open) { setLoading(false); setError(translated('宿主已连接，等待执行轨迹入口提供观察会话。', 'Connected to the host. Waiting for the execution trace entry to provide an observation session.')); }
    }).catch(showError);
    return () => {
      mounted.current = false; gate.current.invalidate(); client.current = null;
      exporter.dispose(); exports.current = null;
      groups.dispose(); groupReads.current = null;
      reader.dispose(); changes.current = null; selectionAbort.current.abort();
      for (const entry of partRequests.current.values()) entry.controller.abort();
      document.removeEventListener('visibilitychange', visibility);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      if (listTimer.current) clearTimeout(listTimer.current);
      void adapter.close();
    };
  }, []);

  const exportBinding = JSON.stringify(snapshot?.binding ?? null);
  useEffect(() => { exports.current?.bind(currentSnapshot.current?.binding ?? null); }, [exportBinding]);
  useEffect(() => { groupReads.current?.update(); }, [snapshot, view.agentScope, view.expandedGroupIds]);

  useLayoutEffect(() => {
    const timing = pendingTiming.current;
    if (timing && snapshot?.generation === timing.generation && snapshot.revision >= timing.revision) {
      recordChangeTiming('dom', timing); pendingTiming.current = null;
    }
  }, [snapshot]);

  const search = searchKeys(view, snapshot);
  const searchDisplay = searchVisibility(view.filters.text, search, searchResult);
  useEffect(() => {
    const state = currentSnapshot.current, token = gate.current.capture();
    if (!state || !token.binding || tracking.current.paused) return;
    const serial = ++querySerial.current;
    // 结构化筛选在本地立即生效；正文搜索由服务端查已加载正文，且结果绑定加载范围。
    // 无正文查询时不用旧 ID 集合，避免历史分页新增行被短暂隐藏并扰动阅读锚点。
    if (!currentView.current.filters.text) { setSearchResult(null); return; }
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, selectionAbort.current.signal, tracking.current.signal]);
    const timer = setTimeout(() => {
      void client.current?.call<QueryResult>('query_loaded_events', { binding: token.binding,
        loadedSetId: state.coverage.loadedSetId, scopeRevision: state.coverage.scopeRevision,
        filters: currentView.current.filters, agentScope: currentView.current.agentScope }, { signal }).then(result => {
        if (signal.aborted || !gate.current.accepts(token, result.binding) || serial !== querySerial.current) return;
        const latest = currentSnapshot.current;
        if (!latest || result.generation !== latest.generation || result.revision !== latest.revision || result.loadedSetId !== latest.coverage.loadedSetId
          || result.scopeRevision !== latest.coverage.scopeRevision) return;
        setSearchResult({ ...search, ids: new Set(result.eventIds) });
      }).catch(failure => { if (!signal.aborted && serial === querySerial.current) showError(failure, token); });
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); ++querySerial.current; };
  }, [search.requestKey, trackingPaused]);

  useEffect(() => {
    const state = currentSnapshot.current, selected = currentView.current.selectedEventId;
    if (!state || !selected) return;
    const event = state.events.find(row => row.eventId === selected);
    if (!event) return;
    for (const part of partsForTab(currentView.current.detailTab, event.content.map(value => value.part))) void loadPart(part);
  }, [view.selectedEventId, view.detailTab, snapshot?.revision, snapshot?.generation, snapshot?.binding.viewSessionId, snapshot?.binding.selectionEpoch]);

  return <TraceUI threadScope={threadScope} onThreadScope={changeThreadScope} snapshot={snapshot} view={view} trackingPaused={trackingPaused} detail={detail} threads={threads} threadsMore={threadsMore}
    matchedIds={searchDisplay.matchedIds} searching={searchDisplay.searching} loading={loading} detailLoading={detailLoading} error={displayMessage(error, language)} exportMessage={displayMessage(exportMessage, language)} exporting={exporting}
    onTask={id => { void selectTask(id); }} onTaskSearch={searchTasks} onMoreThreads={() => { void listThreads(true); }}
    onView={patchView} onTrackingPause={pauseTracking} onSelect={selectEvent} onLoadEarlier={groupId => {
      const current = currentSnapshot.current;
      const cursor = groupId ? current?.groups.find(group => group.groupId === groupId)?.nextCursor : current?.coverage.nextCursor;
      if (cursor) void readSnapshot(gate.current.capture(), cursor, false, groupId ?? null);
    }}
    onMoreDetail={(part: ContentPart, readAll = false) => { void loadPart(part, !readAll, readAll); }}
    onExport={() => { void exports.current?.run(); }}
    onRetry={() => { preferences.current.blocked = false; preferences.current.pending = null;
      void readSnapshot(gate.current.capture(), null, true).then(() => groupReads.current?.retry()); void listThreads(); }}/ >;
}

createRoot(document.getElementById('root')!).render(<PreferencesProvider><TrajectoryApp/></PreferencesProvider>);
