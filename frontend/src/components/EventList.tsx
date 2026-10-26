import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import type { TraceEvent } from '../../../contracts/types';
import Icon, { type IconName } from './icons';
import { clockTime, duration, localizedKindLabels, makeRows, readingAnchor, localizedStatusLabels, virtualWindow, type ListRow } from './model';
import { bulkExpansion } from './expansion';
import { usePreferences } from '../preferences';
import UsageBadge from './UsageBadge';

export function eventIcon(event: TraceEvent): IconName {
  if (event.status.value === 'failed') return 'alert';
  if (event.kind === 'userMessage') return 'user';
  if (event.kind === 'assistantMessage' || event.kind === 'reasoningSummary' || event.kind === 'modelUsage') return 'model';
  if (event.kind === 'compaction') return 'compaction';
  if (event.kind === 'collaboration') return 'branch';
  if (event.kind === 'toolCall' || event.kind === 'toolResult') return 'tool';
  return 'context';
}
export function eventColor(event: TraceEvent) {
  return event.status.value === 'failed' ? 'error' : ['assistantMessage', 'reasoningSummary', 'compaction', 'modelUsage'].includes(event.kind) ? 'model' : ['toolCall', 'toolResult', 'collaboration'].includes(event.kind) ? 'tool' : event.kind === 'userMessage' ? 'input' : 'context';
}
/** 虚拟列表只挂载可见行；阅读位置以事件 ID 和行内偏移保存，分页/更新后再定位。 */
export default function EventList(props: TraceUIProps & { visible: TraceEvent[]; highlightedEvents: ReadonlySet<string> | null; onCount: (count: number) => void }) {
  const { snapshot, view, visible } = props;
  const { language, t } = usePreferences();
  const kindLabels = localizedKindLabels(language);
  const statusLabels = localizedStatusLabels(language);
  const rangeContext = useMemo(() => {
    const turns = new Set<string>(), groupsInRange = new Set<string>();
    if (!snapshot || !props.highlightedEvents) return { turns, groups: groupsInRange };
    const groups = new Map(snapshot.groups.map(group => [group.groupId, group]));
    const createdBy = new Map<string, string[]>();
    for (const group of snapshot.groups) if (group.creationEventId) {
      const ids = createdBy.get(group.creationEventId);
      if (ids) ids.push(group.groupId); else createdBy.set(group.creationEventId, [group.groupId]);
    }
    const markGroup = (id: string) => {
      let group = groups.get(id);
      while (group && !groupsInRange.has(group.groupId)) { groupsInRange.add(group.groupId); group = group.parentGroupId ? groups.get(group.parentGroupId) : undefined; }
    };
    for (const event of visible) if (props.highlightedEvents.has(event.eventId)) {
      if (event.turnId != null) turns.add(JSON.stringify([event.threadId, event.turnId]));
      // 折叠内容不增删行；在其分组和祖先上保留入口提示，防止范围命中被折叠树藏住。
      markGroup(event.groupId);
      for (const id of createdBy.get(event.eventId) ?? []) markGroup(id);
    }
    return { turns, groups: groupsInRange };
  }, [snapshot, visible, props.highlightedEvents]);
  const rows = useMemo(() => snapshot ? makeRows(snapshot, view, visible, language) : [], [snapshot, visible, view.expandedTurnIds, view.expandedGroupIds, view.agentScope, language]);
  const bulk = useMemo(() => snapshot ? {
    expanded: bulkExpansion(snapshot, view, visible, true),
    collapsed: bulkExpansion(snapshot, view, visible, false),
  } : null, [snapshot, visible, view.expandedTurnIds, view.expandedGroupIds, view.agentScope]);
  const canExpand = bulk && (bulk.expanded.expandedTurnIds.length > view.expandedTurnIds.length || bulk.expanded.expandedGroupIds.length > view.expandedGroupIds.length);
  const canCollapse = bulk && bulk.collapsed.expandedTurnIds.length < view.expandedTurnIds.length;
  const scroll = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState({ top: 0, height: 300 });
  const automatic = useRef(false);
  const scrollGeneration = useRef(0);
  const anchor = useRef(view.anchor);
  const lastTop = useRef(0);
  const lastThread = useRef<string | undefined>(undefined);
  const lastSelected = useRef<string | null>(null);
  const receivedAnchor = useRef(view.anchor);
  const pendingFocus = useRef<string | null>(null);
  const window = virtualWindow(rows, viewport.top, viewport.height);
  const rowId = (row: ListRow) => row.type === 'event' ? row.event.eventId : row.type === 'group' ? row.group.creationEventId : null;

  function moveTo(top: number) {
    const element = scroll.current;
    if (!element) return;
    const generation = ++scrollGeneration.current;
    // 标记程序滚动，避免 onScroll 把恢复锚点或自动跟随误认为用户主动阅读。
    automatic.current = true;
    element.scrollTop = top;
    lastTop.current = element.scrollTop;
    setViewport({ top: element.scrollTop, height: element.clientHeight });
    requestAnimationFrame(() => { if (scrollGeneration.current === generation) automatic.current = false; });
  }
  function captureAnchor() {
    const element = scroll.current;
    if (!element) return anchor.current;
    return readingAnchor(rows, element.scrollTop) ?? anchor.current;
  }
  useLayoutEffect(() => {
    if (!scroll.current) return;
    const observer = new ResizeObserver(() => {
      const element = scroll.current!;
      setViewport({ top: element.scrollTop, height: element.clientHeight });
    });
    observer.observe(scroll.current);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    // 偏好可能晚于首屏返回；同任务下也要接受外部锚点。
    // 本地滚动已持有同一锚点时不重复定位，避免反馈循环。
    const previous = receivedAnchor.current;
    receivedAnchor.current = view.anchor;
    if (previous?.eventId === view.anchor?.eventId && previous?.offsetPx === view.anchor?.offsetPx && previous?.groupId === view.anchor?.groupId) return;
    const current = anchor.current;
    if (current?.eventId === view.anchor?.eventId && current?.offsetPx === view.anchor?.offsetPx && current?.groupId === view.anchor?.groupId) return;
    anchor.current = view.anchor;
    if (view.follow === 'paused' && view.anchor) {
      const saved = rows.find(row => rowId(row) === view.anchor!.eventId);
      if (saved) moveTo(saved.top + view.anchor.offsetPx);
    }
  }, [view.anchor, view.follow, rows]);
  useLayoutEffect(() => {
    props.onCount(rows.filter(row => row.type === 'event' || row.type === 'group').length);
    const element = scroll.current;
    if (!element) return;
    const newThread = lastThread.current !== snapshot?.binding.threadId;
    if (newThread) {
      lastThread.current = snapshot?.binding.threadId;
      anchor.current = view.anchor;
      lastSelected.current = view.selectedEventId;
    }
    const selected = rows.find(row => rowId(row) === view.selectedEventId);
    let capture = false;
    if (!newThread && view.selectedEventId && view.selectedEventId !== lastSelected.current && selected) {
      lastSelected.current = view.selectedEventId;
      if (selected.top < element.scrollTop) moveTo(selected.top);
      else if (selected.top + selected.height > element.scrollTop + element.clientHeight) moveTo(selected.top + selected.height - element.clientHeight);
      capture = true;
    } else if (view.follow === 'following') {
      moveTo(element.scrollHeight);
      capture = true;
    } else if (anchor.current) {
      const same = rows.find(row => rowId(row) === anchor.current!.eventId);
      if (same) moveTo(same.top + anchor.current.offsetPx);
      // 分页过渡时“加载较早”行可能先消失，目标位置可暂时为负。
      // 浏览器会钳制实际 scrollTop，但这里保留原锚点和带符号偏移，下一页才能精确恢复。
    } else { if (newThread) moveTo(0); capture = true; }
    if (!view.selectedEventId) lastSelected.current = null;
    if (capture) anchor.current = captureAnchor();
  }, [rows, snapshot?.binding.threadId, view.follow, view.selectedEventId]);
  useLayoutEffect(() => {
    if (pendingFocus.current) {
      // 键盘目标可能尚未挂载；等待虚拟窗口移动后再聚焦，preventScroll 避免二次滚动。
      const button = scroll.current?.querySelector<HTMLButtonElement>(`[data-event-id="${CSS.escape(pendingFocus.current)}"]`);
      if (button) { button.focus({ preventScroll: true }); pendingFocus.current = null; }
    }
  }, [viewport, rows]);
  function onScroll() {
    const element = scroll.current!;
    setViewport({ top: element.scrollTop, height: element.clientHeight });
    if (!automatic.current) {
      const next = captureAnchor();
      anchor.current = next;
      // 只有用户向上阅读才暂停跟随；不改变后台读取和任务执行状态。
      props.onView({ anchor: next, ...(element.scrollTop < lastTop.current - 1 ? { follow: 'paused' as const } : {}) });
    }
    lastTop.current = element.scrollTop;
  }
  function rowKey(event: React.KeyboardEvent, id: string) {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const entries = rows.filter(row => row.type === 'event');
    const index = entries.findIndex(row => row.type === 'event' && row.event.eventId === id);
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1 : Math.max(0, Math.min(entries.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)));
    const next = entries[nextIndex];
    if (next?.type !== 'event') return;
    pendingFocus.current = next.event.eventId;
    props.onSelect(next.event.eventId);
    moveTo(Math.max(0, Math.min(next.top, scroll.current!.scrollHeight - scroll.current!.clientHeight)));
  }
  function renderRow(row: ListRow) {
    const style = { position: 'absolute' as const, top: row.top, height: row.height, left: 0, right: 0 };
    if (row.type === 'earlier') return <button key={row.key} style={style} className="cx-load" disabled={props.loading || props.trackingPaused} title={props.trackingPaused ? t('继续跟踪后可加载较早记录', 'Resume tracking to load earlier records') : undefined} aria-label={row.group ? t(`加载 ${row.group.name} 的较早记录`, `Load earlier records for ${row.group.name}`) : t('加载更早记录', 'Load earlier records')} onClick={() => {
      const saved = captureAnchor(); anchor.current = saved;
      props.onView({ follow: 'paused', anchor: saved });
      props.onLoadEarlier(row.group?.groupId);
    }}><Icon name="up" />{props.loading ? t('正在加载较早记录…', 'Loading earlier records…') : row.group ? t('加载子任务较早记录', 'Load earlier subtask records') : t('加载更早记录', 'Load earlier records')}</button>;
    if (row.type === 'turn') {
      const highlighted = row.turnKey != null && rangeContext.turns.has(row.turnKey);
      return <button key={row.key} style={style} className={`cx-turn ${highlighted ? 'in-range' : ''}`} data-turn-key={row.turnKey ?? undefined} data-range-highlighted={highlighted || undefined} aria-expanded={row.expanded} disabled={row.id === '__context__'} onClick={() => props.onView({ follow: 'paused', expandedTurnIds: row.expanded ? view.expandedTurnIds.filter(id => id !== row.id) : [...view.expandedTurnIds, row.id] })}><Icon name={row.expanded ? 'down' : 'right'} /><span className="cx-turn-label">{row.label}</span>{highlighted && <span className="cx-range-tag">{t('选区', 'Range')}</span>}{row.id !== '__context__' && <UsageBadge values={row.usage} turn />}<small>{t(`${row.count} 条匹配`, `${row.count} matches`)}</small></button>;
    }
    if (row.type === 'warning') return <div key={row.key} style={style} className="cx-group-warning"><Icon name="alert" /><span title={row.group.readIssue ?? ''}>{row.group.readIssue} · {t(`保留 ${row.group.loadedEventCount} 条已读记录`, `${row.group.loadedEventCount} loaded records retained`)}</span></div>;
    if (row.type === 'group') return <div key={row.key} style={style} className={`cx-subtask ${rangeContext.groups.has(row.group.groupId) ? 'in-range' : ''} ${view.selectedEventId === row.group.creationEventId ? 'selected' : ''}`} data-range-highlighted={rangeContext.groups.has(row.group.groupId) || undefined}>
      <button className="cx-group-toggle" aria-expanded={row.expanded} onClick={() => props.onView({ follow: 'paused', expandedGroupIds: row.expanded ? view.expandedGroupIds.filter(id => id !== row.group.groupId) : [...view.expandedGroupIds, row.group.groupId] })}><Icon name={row.expanded ? 'down' : 'right'} /><Icon name="branch" /><span>{row.group.name}</span>{rangeContext.groups.has(row.group.groupId) && <span className="cx-range-tag">{t('选区', 'Range')}</span>}</button>
      <span className="cx-group-count" title={t('当前已读取范围', 'Currently loaded records')}>{t(`${row.group.loadedEventCount} 条 · ${row.group.loadedToolCallCount} 调用`, `${row.group.loadedEventCount} records · ${row.group.loadedToolCallCount} calls`)}</span><span className={`cx-state ${row.group.status.value}`}>{statusLabels[row.group.status.value]}</span>
      {row.group.creationEventId && <button className="cx-icon cx-group-detail" aria-label={t(`查看 ${row.group.name} 创建与来源`, `View creation and source for ${row.group.name}`)} onClick={() => props.onSelect(row.group.creationEventId)}><Icon name="info" /></button>}
    </div>;
    const event = row.event;
    const highlighted = !!props.highlightedEvents?.has(event.eventId);
    return <button key={row.key} style={style} className={`cx-row kind-${event.kind} ${highlighted ? 'in-range' : ''} ${event.status.value === 'failed' ? 'is-error' : ''} ${row.child ? 'child' : ''} ${event.eventId === view.selectedEventId ? 'selected' : ''}`} data-event-id={event.eventId} data-range-highlighted={highlighted || undefined} aria-pressed={event.eventId === view.selectedEventId} aria-label={`${highlighted ? t('时间选区命中，', 'In timeline range, ') : ''}${kindLabels[event.kind]}${event.toolName ? ` ${event.toolName}` : ''}：${event.preview}，${statusLabels[event.status.value]}`}
      onClick={() => props.onSelect(event.eventId)} onKeyDown={key => rowKey(key, event.eventId)}>
      <span className="cx-operation"><span className={`cx-event-icon ${eventColor(event)}`}><Icon name={eventIcon(event)} /></span><span className="cx-preview" title={event.preview}>{event.toolName ? <>{(event.kind === 'toolCall' || event.kind === 'toolResult') && <span className="cx-kind-label">{event.kind === 'toolCall' ? t('调用', 'Call') : t('结果', 'Result')}</span>}<code>{event.toolName}</code></> : <b>{kindLabels[event.kind]}</b>}{event.preview.replace(/\s+/g, ' ')}</span></span>
      <UsageBadge values={event.usage} /><span className="cx-time">{clockTime(event.timing.startMs, language)}</span><span className="cx-duration" title={event.timing.basis}>{event.status.value === 'running' && event.timing.durationMs == null ? '…' : duration(event.timing.durationMs)}</span><span className={`cx-state ${event.status.value}`}>{event.status.value === 'failed' ? `× ${statusLabels.failed}` : event.status.value === 'running' ? `● ${statusLabels.running}` : statusLabels[event.status.value]}</span>
    </button>;
  }
  const selectedHidden = view.selectedEventId && !rows.some(row => rowId(row) === view.selectedEventId);
  const noAgentsSelected = view.agentScope.kind === 'selected' && view.agentScope.groupIds.length === 0;
  return <section className="cx-ledger" aria-label={t('操作记录', 'Operation records')}>
    <div className="cx-column-head"><div className="cx-list-actions"><span>{t('操作', 'Operation')}</span><div className="cx-expand-actions" role="group" aria-label={t('批量展开和折叠轮次', 'Expand and collapse all turns')}>
      <button type="button" disabled={!canExpand} title={t('展开当前筛选结果中的全部已加载轮次', 'Expand all loaded turns in the current results')} onClick={() => bulk && props.onView(bulk.expanded)}><Icon name="down" />{t('展开全部', 'Expand all')}</button>
      <button type="button" disabled={!canCollapse} title={t('折叠当前筛选结果中的全部已加载轮次', 'Collapse all loaded turns in the current results')} onClick={() => bulk && props.onView(bulk.collapsed)}><Icon name="right" />{t('折叠全部', 'Collapse all')}</button>
    </div></div><span className="cx-entry-usage" title={t('输入 / 输出 token；悬浮查看缓存和精确值', 'Input / output tokens; hover for cached tokens and exact values')}>{t('Token 入 / 出', 'Tokens In / Out')}</span><span className="cx-time">{t('时间', 'Time')}</span><span className="cx-duration">{t('耗时', 'Duration')}</span><span>{t('状态', 'Status')}</span></div>
    {selectedHidden && <div className="cx-selection-note" role="status">{t('选中记录不在当前列表中，详情已保留。', 'The selected record is outside the current list. Its details are preserved.')}</div>}
    <div ref={scroll} className="cx-scroll" aria-label={t('可滚动的操作列表', 'Scrollable operation list')} onScroll={onScroll} tabIndex={rows.length ? -1 : 0}>
      <div className="cx-virtual-content" style={{ height: window.totalHeight }}>{rows.slice(window.start, window.end).map(renderRow)}</div>
      {!visible.length && <div className="cx-empty"><Icon name="context" /><strong>{noAgentsSelected ? t('尚未选择 Agent', 'No agents selected') : props.loading ? t('正在读取轨迹…', 'Loading trace…') : props.searching ? t('正在检索已加载正文…', 'Searching loaded content…') : !snapshot ? t('选择一个任务查看执行轨迹', 'Select a task to view its trace') : snapshot.events.length ? t('已加载记录中无匹配结果', 'No matches in loaded records') : t('暂无可展示事件', 'No events to display')}</strong><p>{noAgentsSelected ? t('点击上方 Agent 按钮选择要展示的记录。', 'Use the Agent buttons above to choose records to display.') : props.searching ? t('检索完成后显示当前条件的匹配记录。', 'Matching records will appear when the search is complete.') : snapshot?.events.length ? t('搜索和筛选只覆盖当前已加载的可读内容。', 'Search and filters cover only the readable content currently loaded.') : snapshot?.coverage.explanation ?? t('选择任务后，只读展示已有记录。', 'Select a task to view its saved records.')}{!noAgentsSelected && snapshot && !snapshot.events.length && t(' 可从顶部导出可读取的原始记录。', ' Export readable raw records from the top toolbar.')}</p>{props.error && <button className="cx-button" onClick={props.onRetry}>{t('重试读取', 'Retry loading')}</button>}</div>}
    </div>
  </section>;
}
