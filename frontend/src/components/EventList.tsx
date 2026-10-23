import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import type { TraceEvent } from '../../../contracts/types';
import Icon, { type IconName } from './icons';
import { clockTime, duration, kindLabels, makeRows, readingAnchor, statusLabels, virtualWindow, type ListRow } from './model';
import { bulkExpansion } from './expansion';

export function eventIcon(event: TraceEvent): IconName {
  if (event.status.value === 'failed') return 'alert';
  if (event.kind === 'userMessage') return 'user';
  if (event.kind === 'assistantMessage' || event.kind === 'reasoningSummary') return 'model';
  if (event.kind === 'compaction') return 'compaction';
  if (event.kind === 'collaboration') return 'branch';
  if (event.kind === 'toolCall' || event.kind === 'toolResult') return 'tool';
  return 'context';
}
export function eventColor(event: TraceEvent) {
  return event.status.value === 'failed' ? 'error' : ['assistantMessage', 'reasoningSummary', 'compaction'].includes(event.kind) ? 'model' : ['toolCall', 'toolResult', 'collaboration'].includes(event.kind) ? 'tool' : event.kind === 'userMessage' ? 'input' : 'context';
}
/** 虚拟列表只挂载可见行；阅读位置以事件 ID 和行内偏移保存，分页/更新后再定位。 */
export default function EventList(props: TraceUIProps & { visible: TraceEvent[]; onCount: (count: number) => void }) {
  const { snapshot, view, visible } = props;
  const rows = useMemo(() => snapshot ? makeRows(snapshot, view, visible) : [], [snapshot, visible, view.expandedTurnIds, view.expandedGroupIds, view.agentScope]);
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
    if (row.type === 'earlier') return <button key={row.key} style={style} className="cx-load" disabled={props.loading} aria-label={row.group ? `加载 ${row.group.name} 的较早记录` : '加载更早记录'} onClick={() => {
      const saved = captureAnchor(); anchor.current = saved;
      props.onView({ follow: 'paused', anchor: saved });
      props.onLoadEarlier(row.group?.groupId);
    }}><Icon name="up" />{props.loading ? '正在加载较早记录…' : row.group ? '加载子任务较早记录' : '加载更早记录'}</button>;
    if (row.type === 'turn') return <button key={row.key} style={style} className="cx-turn" aria-expanded={row.expanded} disabled={row.id === '__context__'} onClick={() => props.onView({ follow: 'paused', expandedTurnIds: row.expanded ? view.expandedTurnIds.filter(id => id !== row.id) : [...view.expandedTurnIds, row.id] })}><Icon name={row.expanded ? 'down' : 'right'} />{row.label}<small>{row.count} 条匹配</small></button>;
    if (row.type === 'warning') return <div key={row.key} style={style} className="cx-group-warning"><Icon name="alert" /><span title={row.group.readIssue ?? ''}>{row.group.readIssue} · 保留 {row.group.loadedEventCount} 条已读记录</span></div>;
    if (row.type === 'group') return <div key={row.key} style={style} className={`cx-subtask ${view.selectedEventId === row.group.creationEventId ? 'selected' : ''}`}>
      <button className="cx-group-toggle" aria-expanded={row.expanded} onClick={() => props.onView({ follow: 'paused', expandedGroupIds: row.expanded ? view.expandedGroupIds.filter(id => id !== row.group.groupId) : [...view.expandedGroupIds, row.group.groupId] })}><Icon name={row.expanded ? 'down' : 'right'} /><Icon name="branch" /><span>{row.group.name}</span></button>
      <span className="cx-group-count" title="当前已读取范围">{row.group.loadedEventCount} 条 · {row.group.loadedToolCallCount} 调用</span><span className={`cx-state ${row.group.status.value}`}>{statusLabels[row.group.status.value]}</span>
      {row.group.creationEventId && <button className="cx-icon cx-group-detail" aria-label={`查看 ${row.group.name} 创建与来源`} onClick={() => props.onSelect(row.group.creationEventId)}><Icon name="info" /></button>}
    </div>;
    const event = row.event;
    return <button key={row.key} style={style} className={`cx-row kind-${event.kind} ${event.status.value === 'failed' ? 'is-error' : ''} ${row.child ? 'child' : ''} ${event.eventId === view.selectedEventId ? 'selected' : ''}`} data-event-id={event.eventId} aria-pressed={event.eventId === view.selectedEventId} aria-label={`${kindLabels[event.kind]}${event.toolName ? ` ${event.toolName}` : ''}：${event.preview}，${statusLabels[event.status.value]}`}
      onClick={() => props.onSelect(event.eventId)} onKeyDown={key => rowKey(key, event.eventId)}>
      <span className="cx-operation"><span className={`cx-event-icon ${eventColor(event)}`}><Icon name={eventIcon(event)} /></span><span className="cx-preview" title={event.preview}>{event.toolName ? <>{(event.kind === 'toolCall' || event.kind === 'toolResult') && <span className="cx-kind-label">{event.kind === 'toolCall' ? '调用' : '结果'}</span>}<code>{event.toolName}</code></> : <b>{kindLabels[event.kind]}</b>}{event.preview.replace(/\s+/g, ' ')}</span></span>
      <span className="cx-time">{clockTime(event.timing.startMs)}</span><span className="cx-duration" title={event.timing.basis}>{event.status.value === 'running' && event.timing.durationMs == null ? '…' : duration(event.timing.durationMs)}</span><span className={`cx-state ${event.status.value}`}>{event.status.value === 'failed' ? '× 失败' : event.status.value === 'running' ? '● 执行中' : statusLabels[event.status.value]}</span>
    </button>;
  }
  const selectedHidden = view.selectedEventId && !rows.some(row => rowId(row) === view.selectedEventId);
  const noAgentsSelected = view.agentScope.kind === 'selected' && view.agentScope.groupIds.length === 0;
  return <section className="cx-ledger" aria-label="操作记录">
    <div className="cx-column-head"><div className="cx-list-actions"><span>操作</span><div className="cx-expand-actions" role="group" aria-label="批量展开和折叠轮次">
      <button type="button" disabled={!canExpand} title="展开当前筛选结果中的全部已加载轮次" onClick={() => bulk && props.onView(bulk.expanded)}><Icon name="down" />展开全部</button>
      <button type="button" disabled={!canCollapse} title="折叠当前筛选结果中的全部已加载轮次" onClick={() => bulk && props.onView(bulk.collapsed)}><Icon name="right" />折叠全部</button>
    </div></div><span className="cx-time">时间</span><span className="cx-duration">耗时</span><span>状态</span></div>
    {selectedHidden && <div className="cx-selection-note" role="status">选中记录不在当前列表中，详情已保留。</div>}
    <div ref={scroll} className="cx-scroll" aria-label="可滚动的操作列表" onScroll={onScroll} tabIndex={rows.length ? -1 : 0}>
      <div className="cx-virtual-content" style={{ height: window.totalHeight }}>{rows.slice(window.start, window.end).map(renderRow)}</div>
      {!visible.length && <div className="cx-empty"><Icon name="context" /><strong>{noAgentsSelected ? '尚未选择 Agent' : props.loading ? '正在读取轨迹…' : props.searching ? '正在检索已加载正文…' : !snapshot ? '选择一个任务查看执行轨迹' : snapshot.events.length ? '已加载记录中无匹配结果' : '暂无可展示事件'}</strong><p>{noAgentsSelected ? '点击上方 Agent 按钮选择要展示的记录。' : props.searching ? '检索完成后显示当前条件的匹配记录。' : snapshot?.events.length ? '搜索和筛选只覆盖当前已加载的可读内容。' : snapshot?.coverage.explanation ?? '选择任务后，只读展示已有记录。'}{!noAgentsSelected && snapshot && !snapshot.events.length && ' 可从顶部导出可读取的原始记录。'}</p>{props.error && <button className="cx-button" onClick={props.onRetry}>重试读取</button>}</div>}
    </div>
  </section>;
}
