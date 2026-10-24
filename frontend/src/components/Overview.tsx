import { useEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import { usePreferences } from '../preferences';
import Icon from './icons';
import { clockTime, localizedKindLabels, overviewFrame, overviewMarks, recordedEnd, relativeTime, scopedEvents, selectedAgentIds, type OverviewMark } from './model';
import { useTimelineViewport } from './useTimelineViewport';
import TimelineCoverage from './TimelineCoverage';

/** 概览呈现 Agent 范围内的证据；图内导航与列表筛选分别维护，事件选择仍交给上层。 */
export default function Overview(props: TraceUIProps) {
  const { snapshot, view, onView } = props;
  const { language, locale, t } = usePreferences();
  const kindLabels = localizedKindLabels(language);
  const [cluster, setCluster] = useState<{ key: string; page: number } | null>(null);
  const [width, setWidth] = useState(1000);
  const svg = useRef<SVGSVGElement>(null);
  const chooser = useRef<HTMLElement>(null);
  // 概览保留当前 Agent 的全貌；列表筛选不裁剪时间轴，缩放/平移也不改变列表内容。
  const events = useMemo(() => snapshot ? scopedEvents(snapshot, view) : [], [snapshot, view.agentScope]);
  const frame = useMemo(() => overviewFrame(snapshot, events, language), [snapshot?.timelineOrigin, events, language]);
  const { origin, ordered, min: fullMin, max: fullMax } = frame;
  const range = view.filters.timeRange;
  const resetKey = useMemo(() => ({}), [view.agentScope, ordered, origin]);
  const navigation = useTimelineViewport({ svg, domain: { min: fullMin, max: fullMax }, minimumSpan: 1, resetKey,
    enabled: view.overviewMode === 'duration', onInteract: () => { setCluster(null); if (view.follow !== 'paused') onView({ follow: 'paused' }); } });
  const { min, max } = navigation.viewport;
  const zoomed = min > fullMin || max < fullMax;
  const scale = (fullMax - fullMin) / (max - min);
  const marks = useMemo(() => overviewMarks(events, { min, max, mode: ordered ? 'order' : 'time', selectedEventId: view.selectedEventId, bins: Math.floor(width / 24) }), [events, min, max, ordered, view.selectedEventId, width]);
  const selectedCluster = cluster ? marks.find(mark => mark.key === cluster.key) : null;
  const clusterPage = Math.min(cluster?.page ?? 0, Math.max(0, Math.ceil((selectedCluster?.events.length ?? 0) / 20) - 1));
  const agents = snapshot ? [
    { groupId: snapshot.rootGroupId, name: t('主 Agent', 'Main Agent') },
    ...snapshot.groups.filter(group => group.groupId !== snapshot.rootGroupId),
  ] : [];
  const selectedAgents = snapshot ? selectedAgentIds(snapshot, view) : new Set<string>();
  function toggleAgent(groupId: string) {
    const next = new Set(selectedAgents);
    if (next.has(groupId)) next.delete(groupId); else next.add(groupId);
    onView({ follow: 'paused', agentScope: { kind: 'selected', groupIds: [...next] },
      expandedGroupIds: next.has(groupId) ? [...new Set([...view.expandedGroupIds, groupId])] : view.expandedGroupIds });
  }
  useEffect(() => {
    if (!svg.current) return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(1, entries[0].contentRect.width)));
    observer.observe(svg.current);
    return () => observer.disconnect();
  }, [view.overviewMode]);
  useEffect(() => { setCluster(null); }, [resetKey]);
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
  function openMark(mark: OverviewMark) {
    if (mark.events.length === 1) { setCluster(null); props.onSelect(mark.events[0].eventId); }
    else { setCluster({ key: mark.key, page: 0 }); onView({ follow: 'paused' }); }
  }
  function closeCluster() {
    const key = cluster?.key; setCluster(null);
    requestAnimationFrame(() => svg.current?.querySelector<SVGGElement>(`[data-overview-key="${CSS.escape(key ?? '')}"]`)?.focus());
  }
  function zoomCluster(mark: OverviewMark) {
    const clusterSpan = Math.max(1, mark.end - mark.start);
    const padding = clusterSpan / 10;
    navigation.show({ min: mark.start - padding, max: Math.max(mark.start + 1, mark.end + padding) });
    setCluster(null);
  }
  const coordinate = (time: number) => (time - min) / Math.max(1, max - min) * 1000;
  const axisTime = (time: number) => {
    const value = Math.round(time);
    // 放大后仍能区分相邻刻度，不用重复的整秒标签暗示没有移动。
    if (max - min < 10000) return origin == null
      ? `${clockTime(value, language)}.${String(((value % 1000) + 1000) % 1000).padStart(3, '0')}`
      : `${value < origin ? '−' : ''}${relativeTime(Math.abs(value - origin))}.${String(Math.abs(value - origin) % 1000).padStart(3, '0')}`;
    return origin == null ? clockTime(value, language) : relativeTime(value - origin);
  };
  // 精细刻度文字更长；窄屏减少刻度，标签仍与对应网格线对齐。
  const detailedAxis = max - min < 10000;
  const ticks = max - min < 4 || width < (detailedAxis ? 440 : 300) ? [0, 1]
    : width < (detailedAxis ? 760 : 600) ? [0, .5, 1] : [0, .25, .5, .75, 1];
  const gestureHint = t('滚轮或双指捏合缩放 · 拖动或横向滚动平移 · 双击恢复全貌。键盘 + / − 缩放，左右箭头平移，Home 恢复。', 'Wheel or pinch to zoom. Drag or scroll horizontally to pan. Double-click to reset. Keyboard: + / − to zoom, arrows to pan, Home to reset.');
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
  return <section className="cx-overview" aria-label={t('执行概览', 'Execution overview')}>
    <div className="cx-agent-bar">
      <span className="cx-agent-label">Agent <small>{t('多选', 'Multi-select')}</small></span>
      <div className="cx-agent-buttons" role="group" aria-label={t('Agent 查看范围，可多选', 'Agent scope, multiple selections allowed')}>
        {agents.map(agent => <button key={agent.groupId} className="cx-agent-button" aria-pressed={selectedAgents.has(agent.groupId)} title={agent.name} onClick={() => toggleAgent(agent.groupId)}>
          <Icon name={selectedAgents.has(agent.groupId) ? 'check' : agent.groupId === snapshot?.rootGroupId ? 'model' : 'branch'} /><span>{agent.name}</span>
        </button>)}
        {!snapshot && <span className="cx-muted">{t('选择任务后显示 Agent', 'Select a task to see agents')}</span>}
      </div>
    </div>
    <div className="cx-overview-toolbar">
      <div className="cx-modes" aria-label={t('概览方式', 'Overview mode')}>{([['duration', t('时间', 'Timeline')], ['turns', t('轮次', 'Turns')], ['calls', t('调用', 'Calls')]] as const).map(([mode, label]) =>
        <button key={mode} aria-pressed={view.overviewMode === mode} onClick={() => { onView({ overviewMode: mode }); setCluster(null); }}>{label}</button>)}</div>
      <div className="cx-range">{range && <><span title={`${t('当前时间区间；', 'Current time range; ')}${frame.explanation}`}>{ordered ? t('时间筛选', 'Time filter') : `${axisTime(range.startMs)}–${axisTime(range.endMs)}`}</span><button className="cx-icon" aria-label={t('取消区间筛选', 'Clear range filter')} onClick={() => onView({ filters: { ...view.filters, timeRange: null } })}><Icon name="close" /></button></>}</div>
      {view.overviewMode === 'duration' && <>
        <span className="cx-gesture-hint" title={gestureHint}>{t('滚轮 / 捏合缩放 · 拖动平移', 'Wheel / pinch to zoom · drag to pan')}</span>
        <div className="cx-zoom-controls" role="group" aria-label={t('时间轴缩放', 'Timeline zoom')}>
          <button className="cx-icon cx-zoom" aria-label={t('缩小时间轴', 'Zoom out timeline')} title={t('缩小（−）', 'Zoom out (−)')} disabled={!events.length || !zoomed} onClick={() => navigation.zoom(2)}><Icon name="zoomOut" /></button>
          <span className="cx-zoom-level" aria-label={t(`当前缩放 ${Number(scale.toFixed(1))} 倍`, `Current zoom ${Number(scale.toFixed(1))} times`)}>{Number(scale.toFixed(1))}×</span>
          <button className="cx-icon cx-zoom" aria-label={t('放大时间轴', 'Zoom in timeline')} title={t('放大（+）', 'Zoom in (+)')} disabled={!events.length || max - min <= 1} onClick={() => navigation.zoom(.5)}><Icon name="zoomIn" /></button>
          <button className="cx-button cx-reset-zoom" aria-label={t('恢复全部概览范围', 'Show full overview')} title={t('恢复全貌（双击 / Home）', 'Reset overview (double-click / Home)')} disabled={!zoomed} onClick={navigation.reset}>{t('全貌', 'Reset')}</button>
        </div>
      </>}
    </div>
    {view.overviewMode === 'duration' ? <div className="cx-chart">
      <div className="cx-lane-labels"><span>{t('输入', 'Input')}</span><span>{t('模型', 'Model')}</span><span>{t('工具', 'Tools')}</span></div>
      <svg ref={svg} className={`cx-timeline ${ordered ? 'ordered' : ''} ${navigation.dragging ? 'dragging' : ''}`} viewBox="0 0 1000 90" preserveAspectRatio="none" role="group" tabIndex={0} aria-label={`${frame.explanation}${gestureHint}`} data-viewport-min={min} data-viewport-max={max}
        {...navigation.svgHandlers} onDoubleClick={event => { event.preventDefault(); navigation.reset(); }}>
        {ticks.map(fraction => <line key={fraction} x1={fraction * 1000} x2={fraction * 1000} y1="0" y2="90" className="cx-gridline" />)}
        {marks.map(mark => {
          const event = mark.events[0], count = mark.events.length, grouped = count > 1;
          const position = Math.max(0, coordinate(mark.start));
          // 聚合簇宽度覆盖多条记录的范围，不能解释为某一次操作的持续时间。
          const extent = Math.max(3, Math.min(1000, coordinate(mark.end)) - position);
          const isChild = !grouped && event.groupId !== snapshot?.rootGroupId;
          const y = mark.lane * 30 + (mark.lane === 2 && isChild ? 17 : 7);
          const label = grouped ? t(`${count} 条记录的聚合簇，点击选择具体记录`, `Cluster of ${count} records; click to select a record`) : `${event.toolName ?? kindLabels[event.kind]}: ${event.preview}`;
          return <g key={mark.key} role="button" tabIndex={0} data-overview-key={mark.key} data-event-id={grouped ? undefined : event.eventId} aria-label={label} aria-haspopup={grouped ? 'dialog' : undefined} aria-expanded={grouped ? cluster?.key === mark.key : undefined} className={`cx-svg-mark lane-${mark.lane} ${grouped ? 'cluster' : event.status.value} ${isChild ? 'child' : ''} ${mark.selected ? 'selected' : ''}`}
            onClick={() => openMark(mark)} onKeyDown={key => { if (key.key === 'Enter' || key.key === ' ') { key.preventDefault(); openMark(mark); } }}>
            <title>{grouped ? t(`${count} 条记录；可能包含主任务与子任务，范围不表示单次操作时长`, `${count} records; may include main and child tasks. The range does not indicate one operation’s duration.`) : `${event.preview} · ${ordered ? t('时间未记录，仅显示顺序', 'Time not recorded; showing order only') : event.timing.durationMs == null ? t('时长未记录', 'Duration not recorded') : `${event.timing.durationMs} ms`}`}</title>
            <rect x={position - 1} y={mark.lane * 30} width={Math.max(7, extent + 2)} height="30" fill="transparent" stroke="none" />
            <rect className="cx-visible-mark" x={position} y={y} width={extent} height={grouped ? 16 : mark.lane === 2 ? 9 : 14} rx="2" />
            {grouped && <text transform={`translate(${Math.min(990, position + 3)} ${mark.lane * 30 + 19}) scale(${1000 / width} 1)`}>{count}</text>}
          </g>;
        })}
      </svg>
      {!events.length && <span className="cx-chart-empty">{snapshot ? selectedAgents.size ? t('暂无可展示的记录', 'No records to display') : t('选择上方 Agent 查看轨迹', 'Select an agent above to view its trace') : t('选择任务查看轨迹', 'Select a task to view its trace')}</span>}
      <div className={`cx-axis ${ordered ? 'ordered' : ''}`} title={ordered ? frame.explanation : `${frame.explanation} ${new Date(min).toLocaleString(locale)}–${new Date(max).toLocaleString(locale)}`}>{ordered ? <><span>{t('记录顺序 · 时间未记录', 'Record order · time not recorded')}</span><span>{events.length ? `#${Math.ceil(min)}–#${Math.floor(max)}` : t('无记录', 'No records')}</span></> : ticks.map(fraction => <span key={fraction} style={{ left: `${fraction * 100}%`, transform: fraction === 0 ? 'none' : fraction === 1 ? 'translateX(-100%)' : 'translateX(-50%)' }}>{fraction === 0 ? origin == null ? t('绝对 ', 'Abs ') : t('创建后 ', 'Since creation ') : ''}{axisTime(min + (max - min) * fraction)}</span>)}</div>
      {!ordered && events.length > 0 && <TimelineCoverage events={events} domain={{ min: fullMin, max: fullMax }} viewport={{ min, max }} width={width} formatTime={axisTime} />}
      {zoomed && <input className="cx-timeline-scroll" type="range" aria-label={ordered ? t('平移记录顺序', 'Pan record order') : t('平移时间轴', 'Pan timeline')} min={fullMin} max={fullMax - (max - min)} step="any" value={min} onChange={event => { const start = Number(event.target.value); navigation.show({ min: start, max: start + max - min }); }} />}
    </div> : view.overviewMode === 'turns' ? <div className="cx-turns" aria-label={t('已加载轮次', 'Loaded turns')}>{turnRows.length ? turnRows.map(turn => {
      const items = turnItems.get(`${turn.threadId}:${turn.turnId}`)!;
      const times = items.flatMap(event => event.timing.startMs == null ? [] : [event.timing.startMs, recordedEnd(event) ?? event.timing.startMs]);
      return <button key={`${turn.threadId}:${turn.turnId}`} title={turn.turnId} onClick={() => { if (times.length) setRange(Math.min(...times), Math.max(...times)); else if (items[0]) props.onSelect(items[0].eventId); }}>{t(`第 ${turn.order + 1} 轮`, `Turn ${turn.order + 1}`)}<span>{t(`${items.length} 条`, `${items.length} records`)}</span></button>;
    }) : <span className="cx-muted">{t('尚无可展示轮次', 'No turns to display yet')}</span>}</div> : <div className="cx-calls"><div>{t('工具调用', 'Tool calls')} <b>{new Set(events.filter(event => event.kind === 'toolCall').map(event => event.callKey ?? event.eventId)).size}</b></div><div>{t('模型请求', 'Model requests')} <b>{requestKeys.size || (view.agentScope.kind === 'all' ? snapshot?.statistics.modelRequests.value ?? t('未记录', 'Not recorded') : t('未记录', 'Not recorded'))}</b></div><span>{t('当前 Agent 范围 · 已加载记录', 'Current agent scope · loaded records')}</span></div>}
    {selectedCluster && <section ref={chooser} className="cx-cluster-picker" role="dialog" aria-label={t('选择聚合簇内的记录', 'Select a record in the cluster')} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeCluster(); } }}>
      <div className="cx-popover-head">{t(`${selectedCluster.events.length} 条记录 · 聚合显示`, `${selectedCluster.events.length} records · clustered`)}<button className="cx-icon" aria-label={t('关闭聚合记录', 'Close clustered records')} onClick={closeCluster}><Icon name="close" /></button></div>
      <p className="cx-cluster-note">{ordered ? t('位置仅表示已加载顺序，不表示持续时间。', 'Position only indicates loaded record order, not duration.') : t('聚合范围不表示单次操作时长；选择记录定位列表。', 'The cluster range does not indicate one operation’s duration. Select a record to locate it in the list.')}</p>
      <div className="cx-cluster-options">{selectedCluster.events.slice(clusterPage * 20, clusterPage * 20 + 20).map(event => <button key={event.eventId} onClick={() => { setCluster(null); props.onSelect(event.eventId); }}><span>{event.toolName ?? kindLabels[event.kind]} · {event.preview}</span><small>{ordered ? t('顺序', 'Order') : clockTime(event.timing.startMs, language)}</small></button>)}</div>
      <div className="cx-cluster-actions"><button className="cx-button" disabled={clusterPage === 0} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage - 1 })}>{t('上一页', 'Previous')}</button><span>{clusterPage + 1} / {Math.ceil(selectedCluster.events.length / 20)}</span><button className="cx-button" disabled={(clusterPage + 1) * 20 >= selectedCluster.events.length} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage + 1 })}>{t('下一页', 'Next')}</button><button className="cx-button" onClick={() => zoomCluster(selectedCluster)}>{t('放大此簇', 'Zoom cluster')}</button></div>
    </section>}
  </section>;
}
