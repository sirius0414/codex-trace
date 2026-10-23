import { useEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import Icon from './icons';
import { clockTime, kindLabels, latestUsage, metric, overviewFrame, overviewMarks, recordedEnd, relativeTime, scopedEvents, type OverviewMark } from './model';

export default function Overview(props: TraceUIProps & { onUsage: () => void; usageOpen: boolean }) {
  const { snapshot, view, onView } = props;
  const [zoomWindow, setZoomWindow] = useState<{ min: number; max: number } | null>(null);
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const [cluster, setCluster] = useState<{ key: string; page: number } | null>(null);
  const [width, setWidth] = useState(1000);
  const svg = useRef<SVGSVGElement>(null);
  const chooser = useRef<HTMLElement>(null);
  const events = useMemo(() => snapshot ? scopedEvents(snapshot, view) : [], [snapshot, view.agentScope]);
  const frame = useMemo(() => overviewFrame(snapshot, events), [snapshot?.timelineOrigin, events]);
  const { origin, ordered, min: fullMin, max: fullMax } = frame;
  const range = view.filters.timeRange;
  const min = zoomWindow?.min ?? fullMin, max = zoomWindow?.max ?? fullMax;
  const marks = useMemo(() => overviewMarks(events, { min, max, mode: ordered ? 'order' : 'time', selectedEventId: view.selectedEventId, bins: Math.floor(width / 24) }), [events, min, max, ordered, view.selectedEventId, width]);
  const selectedCluster = cluster ? marks.find(mark => mark.key === cluster.key) : null;
  const clusterPage = Math.min(cluster?.page ?? 0, Math.max(0, Math.ceil((selectedCluster?.events.length ?? 0) / 20) - 1));
  const usage = latestUsage(snapshot?.mainUsage ?? []);
  useEffect(() => {
    if (!svg.current) return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(1, entries[0].contentRect.width)));
    observer.observe(svg.current);
    return () => observer.disconnect();
  }, [view.overviewMode]);
  useEffect(() => { setZoomWindow(null); setCluster(null); }, [view.agentScope.kind, view.agentScope.groupId, ordered, origin]);
  useEffect(() => { if (cluster) chooser.current?.querySelector<HTMLButtonElement>('button')?.focus(); }, [cluster?.key]);
  useEffect(() => {
    if (!cluster) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as Element).closest('.cx-cluster-picker,[data-overview-key]')) setCluster(null);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [cluster]);
  const setRange = (start: number, end: number) => onView({ follow: 'paused', filters: { ...view.filters, timeRange: { startMs: Math.round(Math.min(start, end)), endMs: Math.round(Math.max(start, end)) } } });
  const x = (event: React.PointerEvent) => {
    const bounds = svg.current!.getBoundingClientRect();
    return min + Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)) * (max - min);
  };
  function startDrag(event: React.PointerEvent<SVGSVGElement>) {
    if (ordered || (event.target as Element).closest('[data-overview-key]')) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const value = x(event); setDrag([value, value]);
  }
  function endDrag(event: React.PointerEvent<SVGSVGElement>) {
    if (!drag || ordered) return;
    const end = x(event);
    if (Math.abs(end - drag[0]) > (max - min) / 200) setRange(drag[0], end);
    setDrag(null);
  }
  function openMark(mark: OverviewMark) {
    if (mark.events.length === 1) { setCluster(null); props.onSelect(mark.events[0].eventId); }
    else { setCluster({ key: mark.key, page: 0 }); onView({ follow: 'paused' }); }
  }
  function closeCluster() {
    const key = cluster?.key; setCluster(null);
    requestAnimationFrame(() => svg.current?.querySelector<SVGGElement>(`[data-overview-key="${CSS.escape(key ?? '')}"]`)?.focus());
  }
  function zoomCluster(mark: OverviewMark) {
    const minimum = ordered ? 1 : 1;
    const clusterSpan = Math.max(minimum, mark.end - mark.start);
    const padding = clusterSpan / 10;
    setZoomWindow({ min: Math.max(fullMin, mark.start - padding), max: Math.min(fullMax, Math.max(mark.start + minimum, mark.end + padding)) });
    setCluster(null);
  }
  const coordinate = (time: number) => (time - min) / Math.max(1, max - min) * 1000;
  const axisTime = (time: number) => origin == null ? clockTime(time) : relativeTime(time - origin);
  const brush = ordered ? null : drag ? { startMs: Math.min(...drag), endMs: Math.max(...drag) } : range;
  const turnItems = useMemo(() => {
    const map = new Map<string, typeof events>();
    for (const event of events) if (event.turnId) {
      const key = `${event.threadId}:${event.turnId}`;
      const current = map.get(key); if (current) current.push(event); else map.set(key, [event]);
    }
    return map;
  }, [events]);
  const turnRows = snapshot?.turns.filter(turn => turnItems.has(`${turn.threadId}:${turn.turnId}`)) ?? [];
  const requestKeys = new Set(events.map(event => event.modelRequestKey).filter(Boolean));
  return <section className="cx-overview" aria-label="执行概览">
    <div className="cx-overview-toolbar">
      <div className="cx-modes" aria-label="概览方式">{([['duration', '时长'], ['turns', '轮次'], ['calls', '调用']] as const).map(([mode, label]) =>
        <button key={mode} aria-pressed={view.overviewMode === mode} onClick={() => { onView({ overviewMode: mode }); setCluster(null); }}>{label}</button>)}</div>
      <select className="cx-agent-scope" aria-label="Agent 查看范围" value={view.agentScope.kind === 'group' ? view.agentScope.groupId ?? 'all' : view.agentScope.kind}
        onChange={event => onView({ follow: 'paused', agentScope: event.target.value === 'all' || event.target.value === 'main' ? { kind: event.target.value, groupId: null } : { kind: 'group', groupId: event.target.value } })}>
        <option value="all">整个任务</option><option value="main">主 Agent</option>
        {snapshot?.groups.filter(group => group.kind !== 'main').map(group => <option key={group.groupId} value={group.groupId}>{group.name}</option>)}
      </select>
      <div className="cx-range">{range && <><span title={`当前时间区间；${frame.explanation}`}>{ordered ? '时间筛选' : `${axisTime(range.startMs)}–${axisTime(range.endMs)}`}</span><button className="cx-icon" aria-label="取消区间筛选" onClick={() => onView({ filters: { ...view.filters, timeRange: null } })}><Icon name="close" /></button></>}</div>
      <button className="cx-usage-trigger" aria-expanded={props.usageOpen} aria-label="查看主任务 token 用量" onClick={props.onUsage}>
        <span className="cx-usage-wide">主任务 · 输入 <b>{metric(usage?.input)}</b> / 输出 <b>{metric(usage?.output)}</b> / 缓存 <b>{metric(usage?.cachedInput)}</b></span>
        <span className="cx-usage-short"><Icon name="usage" />用量</span>
      </button>
      {view.overviewMode === 'duration' && <button className="cx-icon cx-zoom" aria-label={zoomWindow ? '恢复全部概览范围' : ordered ? '放大记录顺序' : '放大时间范围'} disabled={!events.length} onClick={() => { setCluster(null); setZoomWindow(zoomWindow ? null : !ordered && range ? { min: range.startMs, max: Math.max(range.startMs + 1, range.endMs) } : { min: fullMin + (fullMax - fullMin) / 2, max: fullMax }); }}><Icon name={zoomWindow ? 'zoomOut' : 'zoomIn'} /></button>}
    </div>
    {view.overviewMode === 'duration' ? <div className="cx-chart">
      <div className="cx-lane-labels"><span>输入</span><span>模型</span><span>工具</span></div>
      <svg ref={svg} className={`cx-timeline ${ordered ? 'ordered' : ''}`} viewBox="0 0 1000 36" preserveAspectRatio="none" role="group" aria-label={`${frame.explanation}${ordered ? '' : '拖动选择区间，无准确时长只标起点。'}`}
        onPointerDown={startDrag} onPointerMove={event => drag && setDrag([drag[0], x(event)])} onPointerUp={endDrag} onPointerCancel={() => setDrag(null)}>
        {[0, 250, 500, 750, 1000].map(position => <line key={position} x1={position} x2={position} y1="0" y2="36" className="cx-gridline" />)}
        {brush && <rect x={Math.max(0, coordinate(brush.startMs))} width={Math.max(0, Math.min(1000, coordinate(brush.endMs)) - Math.max(0, coordinate(brush.startMs)))} y="0" height="36" className="cx-range-brush" />}
        {marks.map(mark => {
          const event = mark.events[0], count = mark.events.length, grouped = count > 1;
          const position = Math.max(0, coordinate(mark.start));
          // A cluster extent denotes a range of records, never one operation's duration.
          const extent = Math.max(3, Math.min(1000, coordinate(mark.end)) - position);
          const isChild = !grouped && event.groupId !== snapshot?.rootGroupId;
          const y = mark.lane * 12 + (mark.lane === 2 && isChild ? 7 : 3);
          const label = grouped ? `${count} 条记录的聚合簇，点击选择具体记录` : `${event.toolName ?? kindLabels[event.kind]}：${event.preview}`;
          return <g key={mark.key} role="button" tabIndex={0} data-overview-key={mark.key} data-event-id={grouped ? undefined : event.eventId} aria-label={label} aria-haspopup={grouped ? 'dialog' : undefined} aria-expanded={grouped ? cluster?.key === mark.key : undefined} className={`cx-svg-mark lane-${mark.lane} ${grouped ? 'cluster' : event.status.value} ${isChild ? 'child' : ''} ${mark.selected ? 'selected' : ''}`}
            onClick={() => openMark(mark)} onKeyDown={key => { if (key.key === 'Enter' || key.key === ' ') { key.preventDefault(); openMark(mark); } }}>
            <title>{grouped ? `${count} 条记录；可能包含主任务与子任务，范围不表示单次操作时长` : `${event.preview} · ${ordered ? '时间未记录，仅显示顺序' : event.timing.durationMs == null ? '时长未记录' : `${event.timing.durationMs} ms`}`}</title>
            <rect x={position - 1} y={mark.lane * 12} width={Math.max(7, extent + 2)} height="12" fill="transparent" stroke="none" />
            <rect className="cx-visible-mark" x={position} y={y} width={extent} height={grouped ? 8 : mark.lane === 2 ? 4 : 6} rx="1" />
            {grouped && <text transform={`translate(${Math.min(990, position + 3)} ${mark.lane * 12 + 10}) scale(${1000 / width} 1)`} style={{ fontSize: '9px' }}>{count}</text>}
          </g>;
        })}
      </svg>
      <div className={`cx-axis ${ordered ? 'ordered' : ''}`} title={frame.explanation}>{ordered ? <><span>记录顺序 · 时间未记录</span><span>{events.length ? `#${Math.ceil(min)}–#${Math.ceil(max)}` : '无记录'}</span></> : origin == null ? <><span>绝对时间 · 创建时间未记录</span><span title={`${new Date(min).toLocaleString()}–${new Date(max).toLocaleString()}`}>{clockTime(min)}–{clockTime(max)}</span></> : [0, .25, .5, .75, 1].map(fraction => <span key={fraction}>{fraction === 0 ? '创建后 ' : ''}{axisTime(min + (max - min) * fraction)}</span>)}</div>
    </div> : view.overviewMode === 'turns' ? <div className="cx-turns" aria-label="已加载轮次">{turnRows.length ? turnRows.map(turn => {
      const items = turnItems.get(`${turn.threadId}:${turn.turnId}`)!;
      const times = items.flatMap(event => event.timing.startMs == null ? [] : [event.timing.startMs, recordedEnd(event) ?? event.timing.startMs]);
      return <button key={`${turn.threadId}:${turn.turnId}`} title={turn.turnId} onClick={() => { if (times.length) setRange(Math.min(...times), Math.max(...times)); else if (items[0]) props.onSelect(items[0].eventId); }}>第 {turn.order + 1} 轮<span>{items.length} 条</span></button>;
    }) : <span className="cx-muted">尚无可展示轮次</span>}</div> : <div className="cx-calls"><div>工具调用 <b>{new Set(events.filter(event => event.kind === 'toolCall').map(event => event.callKey ?? event.eventId)).size}</b></div><div>模型请求 <b>{requestKeys.size || (view.agentScope.kind === 'all' ? snapshot?.statistics.modelRequests.value ?? '未记录' : '未记录')}</b></div><span>当前 Agent 范围 · 已加载记录</span></div>}
    {selectedCluster && <section ref={chooser} className="cx-cluster-picker" role="dialog" aria-label="选择聚合簇内的记录" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeCluster(); } }}>
      <div className="cx-popover-head">{selectedCluster.events.length} 条记录 · 聚合显示<button className="cx-icon" aria-label="关闭聚合记录" onClick={closeCluster}><Icon name="close" /></button></div>
      <p className="cx-cluster-note">{ordered ? '位置仅表示已加载顺序，不表示持续时间。' : '聚合范围不表示单次操作时长；选择记录定位列表。'}</p>
      <div className="cx-cluster-options">{selectedCluster.events.slice(clusterPage * 20, clusterPage * 20 + 20).map(event => <button key={event.eventId} onClick={() => { setCluster(null); props.onSelect(event.eventId); }}><span>{event.toolName ?? kindLabels[event.kind]} · {event.preview}</span><small>{ordered ? '顺序' : clockTime(event.timing.startMs)}</small></button>)}</div>
      <div className="cx-cluster-actions"><button className="cx-button" disabled={clusterPage === 0} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage - 1 })}>上一页</button><span>{clusterPage + 1} / {Math.ceil(selectedCluster.events.length / 20)}</span><button className="cx-button" disabled={(clusterPage + 1) * 20 >= selectedCluster.events.length} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage + 1 })}>下一页</button><button className="cx-button" onClick={() => zoomCluster(selectedCluster)}>放大此簇</button></div>
    </section>}
  </section>;
}
