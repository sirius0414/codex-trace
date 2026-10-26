import { useEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import { usePreferences } from '../preferences';
import Icon from './icons';
import { clockTime, localizedKindLabels, overviewMarks, recordedEnd, relativeTime, scopedEvents, selectedAgentIds, turnRangeFromActive, turnRangeKeys, type OverviewMark, type OverviewPosition } from './model';
import { useTimelineViewport } from './useTimelineViewport';
import TimelineCoverage from './TimelineCoverage';
import { buildActiveTimeline } from './active-timeline';
import TimelineScrollbar from './TimelineScrollbar';
import TimelineSelection from './TimelineSelection';
import { clampViewport, type Viewport } from './viewport';
import type { TimeRange } from '../../../contracts/types';

function runtimeTime(milliseconds: number, precise = milliseconds > 0 && milliseconds < 1000) {
  const value = Math.max(0, Math.round(milliseconds));
  const seconds = Math.floor(value / 1000), hours = Math.floor(seconds / 3600);
  const clock = hours ? `${String(hours).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}` : relativeTime(value);
  return precise ? `${clock}.${String(value % 1000).padStart(3, '0')}` : clock;
}

/** 概览的视口、时间高亮和事件详情各自独立；标尺／框选拥有选区手势，主图默认导航。 */
export default function Overview(props: TraceUIProps & { highlightedEvents: ReadonlySet<string> | null; onRangeCommit: (range: TimeRange) => void }) {
  const { snapshot, view, onView } = props;
  const { language, locale, showSpanSummary, t } = usePreferences();
  const kindLabels = localizedKindLabels(language);
  const [cluster, setCluster] = useState<{ key: string; page: number } | null>(null);
  const [width, setWidth] = useState(1000);
  const [selecting, setSelecting] = useState(false);
  const svg = useRef<SVGSVGElement>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const gestureRange = useRef<TimeRange | null>(null);
  const selectingGesture = useRef(false);
  const chooser = useRef<HTMLElement>(null);
  // 概览保留当前 Agent 的全貌；列表筛选不裁剪时间轴，缩放/平移也不改变列表内容。
  const events = useMemo(() => snapshot ? scopedEvents(snapshot, view) : [], [snapshot, view.agentScope]);
  // 全部已加载 Agent 共用同一条运行坐标，切换查看范围不改变彼此的时间位置。
  const spacing = view.timelineSpacing ?? 'compact';
  const compact = spacing === 'compact';
  const timeline = useMemo(() => buildActiveTimeline(snapshot, spacing), [snapshot?.events, snapshot?.turns, spacing]);
  const mappingKey = useMemo(() => JSON.stringify(timeline.segments.map(segment => [segment.start, segment.end])), [timeline]);
  const ordered = timeline.mode === 'order';
  const canSelect = !ordered && timeline.durationMs > 0 && events.length > 0;
  const fullMin = ordered ? 1 : 0, fullMax = ordered ? Math.max(2, events.length) : Math.max(1, timeline.durationMs);
  const explanation = ordered ? t('没有可定位的轮次运行时间；仅显示记录顺序。', 'No turn execution times are available; showing record order only.')
    : (compact ? t('紧凑横轴：没有持续记录覆盖的间隔最多保留 1 秒显示宽度，其余折叠；断点可查看原始间隔。保留事件时间点和已记录的持续区间，紧凑坐标不是实际耗时，也不代表已确认断网。', 'Compact axis: gaps with no recorded interval retain at most one second of display width; the rest is folded. Breaks show their original intervals. Event anchors and recorded durations are retained. Compact coordinates are not elapsed time or proof of a network outage.') : '')
      + t('运行跨度合并所有已加载 Agent 的轮次区间，并行只计一次；轮次结束或明确中断后至下一次启动的间隔已排除。开放区间只延伸至最近已记录事件，不按当前时间累计。', 'Runtime spans merge turn intervals across all loaded agents, counting parallel intervals once. Gaps after completion or explicit interruption and before the next start are excluded. Open intervals end at the latest recorded event, not the current time.')
      + (timeline.mode === 'observed' ? t(' 部分轮次缺少边界，按已加载记录范围估算；无法识别未记录的轮次内暂停。', ' Some turn boundaries are missing; their loaded record ranges are used as estimates. Unrecorded pauses within those turns cannot be identified.') : '');
  const range = view.filters.timeRange;
  // 运行坐标由全部 Agent 共用，切换查看范围保留视口；顺序降级的坐标则依赖当前 Agent。
  const scopeKey = JSON.stringify(view.agentScope);
  const resetKey = JSON.stringify([timeline.mode, spacing, ordered ? scopeKey : null]);
  const navigation = useTimelineViewport({ svg, wheelTarget: canvas, domain: { min: fullMin, max: fullMax }, minimumSpan: 1, resetKey,
    isSelectionActive: () => selectingGesture.current,
    enabled: view.overviewMode === 'duration', onInteract: () => {
      setCluster(null); if (view.follow !== 'paused') onView({ follow: 'paused' });
    } });
  const { min, max } = navigation.viewport;
  const zoomed = min > fullMin || max < fullMax;
  const scale = (fullMax - fullMin) / (max - min);
  // 指针在累计坐标中绘制，偏好存墙钟边界；加载更早记录后仍定位原来的运行片段。
  const projectedSelection = view.turnRange && !ordered ? { min: timeline.toActive(view.turnRange.startMs), max: timeline.toActive(view.turnRange.endMs) } : null;
  const selectedTurnCount = useMemo(() => {
    const keys = snapshot ? turnRangeKeys(snapshot, view.turnRange, timeline) : null;
    return keys ? new Set(events.filter(event => event.turnId && keys.has(JSON.stringify([event.threadId, event.turnId]))).map(event => JSON.stringify([event.threadId, event.turnId]))).size : 0;
  }, [snapshot, timeline, events, view.turnRange]);
  const highlightedEventCount = useMemo(() => events.reduce((count, event) => count + Number(props.highlightedEvents?.has(event.eventId) ?? false), 0), [events, props.highlightedEvents]);
  // 原选区可能完全落在新折叠的空档中；保留一对可抓取指针，不改写墙钟选区。
  const selection = projectedSelection && (projectedSelection.max > projectedSelection.min || selectedTurnCount > 0)
    ? clampViewport(projectedSelection, { min: fullMin, max: fullMax }, 1) : null;
  function wallRange(next: Viewport): TimeRange | null {
    if (!canSelect) return null;
    const selected = clampViewport(next, { min: 0, max: timeline.durationMs }, 1);
    const start = Math.round(selected.min), end = Math.max(start + 1, Math.round(selected.max));
    // 反解接缝边界时同时包含落在选区内的零时长轮次，不把邻轮边界误选进来。
    return turnRangeFromActive(timeline, start, end);
  }
  function selectRange(next: Viewport) {
    const turnRange = wallRange(next);
    if (!turnRange) return;
    setCluster(null);
    onView({ follow: 'paused', turnRange });
  }
  function commitRange(next: Viewport) {
    const range = wallRange(next);
    if (range) props.onRangeCommit(range);
  }
  const positions = useMemo(() => new Map<string, OverviewPosition>(events.flatMap<[string, OverviewPosition]>(event => {
    const start = event.timing.startMs, end = recordedEnd(event);
    return start == null || !Number.isFinite(start) ? [] : [[event.eventId, { start: timeline.toActive(start), end: timeline.toActive(end ?? start) }]];
  })), [events, timeline]);
  const marks = useMemo(() => overviewMarks(events, { min, max, mode: ordered ? 'order' : 'time', selectedEventId: view.selectedEventId, bins: Math.floor(width / 24), positions }), [events, min, max, ordered, view.selectedEventId, width, positions]);
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
  useEffect(() => { setCluster(null); }, [resetKey, scopeKey]);
  useEffect(() => { if (!canSelect || view.overviewMode !== 'duration') setSelecting(false); }, [canSelect, view.overviewMode]);
  useEffect(() => { if (cluster) chooser.current?.querySelector<HTMLButtonElement>('button')?.focus(); }, [cluster?.key]);
  useEffect(() => {
    if (!cluster) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as Element).closest('.cx-cluster-picker,[data-overview-key]')) setCluster(null);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [cluster]);
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
  const axisTime = (time: number) => runtimeTime(time, max - min < 10000);
  const wallTime = (time: number) => new Date(time).toLocaleString(locale);
  const seams = useMemo(() => {
    const bins = Math.max(1, Math.floor(width / 80));
    const folds = new Set(timeline.folds.map(fold => `${fold.start}:${fold.end}`));
    const grouped = new Map<string, { kind: 'idle' | 'turn'; offset: number; start: number; end: number; skipped: number; count: number }>();
    for (let index = 1; index < timeline.segments.length; index++) {
      const current = timeline.segments[index], previous = timeline.segments[index - 1];
      if (current.offset < min || current.offset > max || current.start <= previous.end) continue;
      const bin = Math.floor((current.offset - min) / Math.max(1, max - min) * bins);
      const kind = folds.has(`${previous.end}:${current.start}`) ? 'idle' : 'turn';
      const key = `${kind}:${bin}`, item = grouped.get(key);
      if (item) { item.end = current.start; item.skipped += current.start - previous.end; item.count++; }
      else grouped.set(key, { kind, offset: current.offset, start: previous.end, end: current.start, skipped: current.start - previous.end, count: 1 });
    }
    return [...grouped.values()];
  }, [timeline, min, max, width]);
  // 精细刻度文字更长；窄屏减少刻度，标签仍与对应网格线对齐。
  const detailedAxis = max - min < 10000;
  const ticks = max - min < 4 || width < (detailedAxis ? 440 : 300) ? [0, 1]
    : width < (detailedAxis ? 760 : 600) ? [0, .5, 1] : [0, .25, .5, .75, 1];
  const gestureHint = t('平移模式：拖动平移，双击图中空白恢复全貌，点击记录查看详情。顶部标尺始终可单击或拖动建立时间高亮；框选模式可直接在图中选择。滚轮或双指捏合缩放，横向滚动平移；拖动期间忽略滚轮。键盘 + / − 缩放，左右箭头平移，Home 恢复。Esc 取消当前框选，再按退出框选模式。', 'Pan mode: drag to pan, double-click empty chart space to reset, click records for details. Click or drag the top ruler to highlight a time range; Select mode also selects in the chart. Wheel or pinch to zoom, horizontal scroll to pan; wheel is ignored during a drag. Keyboard: + / − to zoom, arrows to pan, Home to reset. Escape cancels a selection gesture; press again to leave Select mode.');
  const turnItems = useMemo(() => {
    const map = new Map<string, typeof events>();
    for (const event of events) if (event.turnId) {
      const key = JSON.stringify([event.threadId, event.turnId]);
      const current = map.get(key); if (current) current.push(event); else map.set(key, [event]);
    }
    return map;
  }, [events]);
  const turnRows = snapshot?.turns.filter(turn => turnItems.has(JSON.stringify([turn.threadId, turn.turnId]))) ?? [];
  const requestKeys = new Set(events.map(event => event.modelRequestKey).filter(Boolean));
  return <section className="cx-overview" aria-label={t('执行概览', 'Execution overview')} onKeyDown={event => {
    if (event.key === 'Escape' && selecting) { event.preventDefault(); event.stopPropagation(); setSelecting(false); }
  }}>
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
      <div className="cx-range">{range && <><span title={`${wallTime(range.startMs)}–${wallTime(range.endMs)}`}>{t('时间筛选 ', 'Time filter ')}{clockTime(range.startMs, language)}–{clockTime(range.endMs, language)}</span><button className="cx-icon" aria-label={t('取消区间筛选', 'Clear range filter')} onClick={() => onView({ filters: { ...view.filters, timeRange: null } })}><Icon name="close" /></button></>}</div>
      {view.overviewMode === 'duration' && <>
        <div className="cx-chart-tools" role="group" aria-label={t('图中拖动方式', 'Chart drag mode')}>
          <button className="cx-button cx-select-turns" aria-label={t('平移时间轴模式', 'Pan timeline mode')} aria-pressed={!selecting}
            onClick={() => { setSelecting(false); setCluster(null); }}>{t('平移', 'Pan')}</button>
          <button className="cx-button cx-select-turns" aria-label={t('在图中框选记录', 'Select records in chart')} aria-pressed={selecting} disabled={!canSelect}
            title={canSelect ? t('单击或拖动建立时间高亮；Esc 返回平移。顶部标尺始终可框选。', 'Click or drag to highlight a time range; Escape returns to Pan. The top ruler always supports selection.') : t('运行时间未记录，无法按时间框选记录', 'Execution times are unavailable; time selection is disabled')}
            onClick={() => { setSelecting(true); setCluster(null); }}>{t('框选', 'Select')}</button>
        </div>
        <span className="cx-gesture-hint" title={gestureHint}>{selecting ? t('拖动框选条目 · 滚轮缩放', 'Drag to select records · wheel to zoom') : t('滚轮 / 捏合缩放 · 拖动平移', 'Wheel / pinch to zoom · drag to pan')}</span>
        <div className="cx-zoom-controls" role="group" aria-label={t('时间轴缩放', 'Timeline zoom')}>
          <button className="cx-icon cx-zoom" aria-label={t('缩小时间轴', 'Zoom out timeline')} title={t('缩小（−）', 'Zoom out (−)')} disabled={!events.length || !zoomed} onClick={() => navigation.zoom(2)}><Icon name="zoomOut" /></button>
          <span className="cx-zoom-level" aria-label={t(`当前缩放 ${Number(scale.toFixed(1))} 倍`, `Current zoom ${Number(scale.toFixed(1))} times`)}>{Number(scale.toFixed(1))}×</span>
          <button className="cx-icon cx-zoom" aria-label={t('放大时间轴', 'Zoom in timeline')} title={t('放大（+）', 'Zoom in (+)')} disabled={!events.length || max - min <= 1} onClick={() => navigation.zoom(.5)}><Icon name="zoomIn" /></button>
          <button className="cx-button cx-reset-zoom" aria-label={t('恢复全部概览范围', 'Show full overview')} title={t('恢复全貌（平移模式双击空白 / Home）', 'Reset overview (double-click empty space in Pan mode / Home)')} disabled={!zoomed} onClick={navigation.reset}>{t('全貌', 'Reset')}</button>
        </div>
      </>}
    </div>
    {view.overviewMode === 'duration' ? <div className={`cx-chart ${canSelect ? 'cx-chart-selectable' : ''}`}>
      <div className="cx-lane-labels"><span>{t('输入', 'Input')}</span><span>{t('模型', 'Model')}</span><span>{t('工具', 'Tools')}</span></div>
      <div ref={canvas} className={`cx-timeline-canvas ${canSelect ? 'cx-with-turn-ruler' : ''}`}>
      <svg ref={svg} className={`cx-timeline ${ordered ? 'ordered' : ''} ${navigation.dragging ? 'dragging' : ''}`} viewBox="0 0 1000 90" preserveAspectRatio="none" role="group" tabIndex={0} aria-label={`${explanation}${gestureHint}`} data-axis-mode={timeline.mode} data-axis-spacing={spacing} data-active-duration-ms={timeline.runtimeDurationMs} data-display-duration-ms={timeline.durationMs} data-folded-unrecorded-ms={timeline.foldedUnrecordedMs} data-excluded-ms={timeline.excludedMs} data-viewport-min={min} data-viewport-max={max}
        {...navigation.svgHandlers} onDoubleClick={event => {
          if (selecting || (event.target as Element).closest('[data-overview-key]')) return;
          event.preventDefault(); navigation.reset();
        }}>
        {ticks.map(fraction => <line key={fraction} x1={fraction * 1000} x2={fraction * 1000} y1="0" y2="90" className="cx-gridline" />)}
        {marks.map(mark => {
          const event = mark.events[0], count = mark.events.length, grouped = count > 1;
          const position = Math.max(0, coordinate(mark.start));
          // 聚合簇宽度覆盖多条记录的范围，不能解释为某一次操作的持续时间。
          const extent = Math.max(3, Math.min(1000, coordinate(mark.end)) - position);
          const isChild = !grouped && event.groupId !== snapshot?.rootGroupId;
          const y = mark.lane * 30 + (mark.lane === 2 && isChild ? 17 : 7);
          const label = grouped ? t(`${count} 条记录的聚合簇，点击选择具体记录`, `Cluster of ${count} records; click to select a record`) : `${event.toolName ?? kindLabels[event.kind]}: ${event.preview}`;
          return <g key={mark.key} role="button" tabIndex={selecting ? -1 : 0} data-overview-key={mark.key} data-event-id={grouped ? undefined : event.eventId} aria-label={label} aria-haspopup={grouped ? 'dialog' : undefined} aria-expanded={grouped ? cluster?.key === mark.key : undefined} className={`cx-svg-mark lane-${mark.lane} ${grouped ? 'cluster' : event.status.value} ${isChild ? 'child' : ''} ${mark.selected ? 'selected' : ''}`}
            onClick={() => openMark(mark)} onKeyDown={key => { if (key.key === 'Enter' || key.key === ' ') { key.preventDefault(); openMark(mark); } }}>
            <title>{grouped ? t(`${count} 条记录；可能包含主任务与子任务，范围不表示单次操作时长`, `${count} records; may include main and child tasks. The range does not indicate one operation’s duration.`) : `${event.preview} · ${ordered ? t('时间未记录，仅显示顺序', 'Time not recorded; showing order only') : event.timing.durationMs == null ? t('时长未记录', 'Duration not recorded') : `${event.timing.durationMs} ms`}`}</title>
            <rect x={position - 1} y={mark.lane * 30} width={Math.max(7, extent + 2)} height="30" fill="transparent" stroke="none" />
            <rect className="cx-visible-mark" x={position} y={y} width={extent} height={grouped ? 16 : mark.lane === 2 ? 9 : 14} rx="2" />
            {grouped && <text transform={`translate(${Math.min(990, position + 3)} ${mark.lane * 30 + 19}) scale(${1000 / width} 1)`}>{count}</text>}
          </g>;
        })}
        {!ordered && seams.map(seam => <g key={`${seam.kind}:${seam.offset}`} className={`cx-time-break ${seam.kind === 'idle' ? 'cx-idle-break' : ''}`} data-break-kind={seam.kind} aria-label={seam.kind === 'idle' ? t(`已折叠 ${runtimeTime(seam.skipped)} 的无记录间隔`, `Folded unrecorded gap ${runtimeTime(seam.skipped)}`) : t(`已排除 ${runtimeTime(seam.skipped)} 的轮次间隔`, `Excluded turn gap ${runtimeTime(seam.skipped)}`)}>
          <title>{`${seam.kind === 'idle' ? t(`折叠 ${seam.count} 段无记录间隔，共 `, `Folded ${seam.count} unrecorded gaps, total `) : t(`排除 ${seam.count} 段轮次间隔，共 `, `Excluded ${seam.count} turn gaps, total `)}${runtimeTime(seam.skipped)} · ${wallTime(seam.start)}–${wallTime(seam.end)}`}</title>
          <line x1={coordinate(seam.offset)} x2={coordinate(seam.offset)} y1="8" y2="90" />
          <path d={`M${coordinate(seam.offset) - 4} 1l4 3-4 3 4 3`} />
        </g>)}
      </svg>
      {canSelect && <TimelineSelection domain={{ min: fullMin, max: fullMax }} viewport={{ min, max }} selection={selection} mappingKey={mappingKey}
        selecting={selecting} onGestureChange={active => {
          // 由真正接管手势的组件冻结墙钟选区；触摸的非主指针也可能开始一次新框选。
          if (active) gestureRange.current = view.turnRange ?? null;
          selectingGesture.current = active;
        }} onChange={(range, restoring) => {
          if (restoring) onView({ turnRange: gestureRange.current });
          else if (range) selectRange(range);
          else onView({ turnRange: null });
        }}
        onCommit={commitRange} onNavigate={navigation.show} formatValue={value => runtimeTime(value, true)} />}
      </div>
      {!events.length && <span className="cx-chart-empty">{snapshot ? selectedAgents.size ? t('暂无可展示的记录', 'No records to display') : t('选择上方 Agent 查看轨迹', 'Select an agent above to view its trace') : t('选择任务查看轨迹', 'Select a task to view its trace')}</span>}
      <div className={`cx-axis ${ordered ? 'ordered' : ''}`} title={explanation}>{ordered ? <><span>{t('记录顺序 · 运行时间未记录', 'Record order · execution time unavailable')}</span><span>{events.length ? `#${Math.ceil(min)}–#${Math.floor(max)}` : t('无记录', 'No records')}</span></> : ticks.map(fraction => {
        const elapsed = min + (max - min) * fraction;
        return <span key={fraction} title={wallTime(timeline.toWall(elapsed, fraction === 1 ? 'end' : 'start'))} style={{ left: `${fraction * 100}%`, transform: fraction === 0 ? 'none' : fraction === 1 ? 'translateX(-100%)' : 'translateX(-50%)' }}>{fraction === 0 ? compact ? t('紧凑 ', 'Compact ') : t('运行 ', 'Active ') : ''}{axisTime(elapsed)}</span>;
      })}</div>
      {showSpanSummary && !ordered && <div className="cx-runtime-note" title={explanation}>
        <span>{t('原运行跨度 ', 'Runtime span ')}{runtimeTime(timeline.runtimeDurationMs)}</span>
        {compact && <span>{t('折叠空白 ', 'Folded gaps ')}{runtimeTime(timeline.foldedUnrecordedMs)}</span>}
        <span>{t('轮次间隔 ', 'Between turns ')}{runtimeTime(timeline.excludedMs)}</span>
        {timeline.mode === 'observed' && <span className="cx-runtime-estimate">{t('按记录估算', 'Estimated from records')}</span>}
      </div>}
      {showSpanSummary && !ordered && timeline.durationMs > 0 && events.length > 0 && <TimelineCoverage events={events} domain={{ min: fullMin, max: fullMax }} viewport={{ min, max }} width={width} formatTime={axisTime} positions={positions} compact={compact} elapsedBetween={timeline.elapsedBetween} />}
      {showSpanSummary && zoomed && <TimelineScrollbar domain={{ min: fullMin, max: fullMax }} viewport={{ min, max }} width={width}
        label={ordered ? t('平移记录顺序', 'Pan record order') : t('平移时间轴', 'Pan timeline')}
        valueText={`${ordered ? `#${Math.ceil(min)}–#${Math.floor(max)}` : `${runtimeTime(min)}–${runtimeTime(max)}`} · ${t(`当前显示全部范围的 ${Number((100 / scale).toFixed(2))}%`, `Showing ${Number((100 / scale).toFixed(2))}% of the full range`)}`}
        title={t('拖动平移；滑块长度表示当前可见范围', 'Drag to pan; thumb length represents the visible range')} onChange={navigation.show} />}
    </div> : view.overviewMode === 'turns' ? <div className="cx-turns" aria-label={t('已加载轮次', 'Loaded turns')}>{turnRows.length ? turnRows.map(turn => {
      const key = JSON.stringify([turn.threadId, turn.turnId]), items = turnItems.get(key)!;
      const spans = timeline.turnSpans.get(key) ?? [];
      const start = spans.length ? Math.min(Number.MAX_SAFE_INTEGER - 1, Math.round(spans[0].start)) : null;
      const end = start == null ? null : Math.min(Number.MAX_SAFE_INTEGER, spans.reduce((last, span) => Math.max(last, Math.round(span.end)), start + 1));
      return <button key={key} disabled={start == null || end == null} title={start == null ? t('时间未记录，无法按时间范围高亮', 'Time unavailable; cannot highlight by time range') : t('高亮此时间范围内的条目，列表内容保持不变', 'Highlight records in this time range without changing list contents')}
        onClick={() => { if (start != null && end != null) { const range = { startMs: start, endMs: end }; onView({ follow: 'paused', turnRange: range }); props.onRangeCommit(range); } }}>{t(`第 ${turn.order + 1} 轮`, `Turn ${turn.order + 1}`)}<span>{t(`${items.length} 条`, `${items.length} records`)}</span></button>;
    }) : <span className="cx-muted">{t('尚无可展示轮次', 'No turns to display yet')}</span>}</div> : <div className="cx-calls"><div>{t('工具调用', 'Tool calls')} <b>{new Set(events.filter(event => event.kind === 'toolCall').map(event => event.callKey ?? event.eventId)).size}</b></div><div>{t('模型请求', 'Model requests')} <b>{requestKeys.size || (view.agentScope.kind === 'all' ? snapshot?.statistics.modelRequests.value ?? t('未记录', 'Not recorded') : t('未记录', 'Not recorded'))}</b></div><span>{t('当前 Agent 范围 · 已加载记录', 'Current agent scope · loaded records')}</span></div>}
    {showSpanSummary && (canSelect || view.turnRange) && <div className="cx-turn-range-note" title={t('拖动只预览高亮；完成选择后定位首条命中记录，必要时展开其所在轮次与分组。其他条目继续显示，列表顺序不变。', 'Dragging previews highlights. Completing a selection reveals the first matching record, expanding its turn and groups when necessary. Other records remain visible in their original order.')}>
      {view.turnRange ? <>
      <span>{t('时间选区 ', 'Time selection ')}{projectedSelection && projectedSelection.max === projectedSelection.min && selectedTurnCount > 0 ? t('折叠空档内', 'Inside a folded gap') : selection ? `${runtimeTime(selection.min, true)}–${runtimeTime(selection.max, true)}` : `${clockTime(view.turnRange.startMs, language)}–${clockTime(view.turnRange.endMs, language)}`}<b>{t(`${highlightedEventCount} 条高亮`, `${highlightedEventCount} highlighted`)}</b></span>
      {canSelect && selection && <button className="cx-button" onClick={() => { onView({ overviewMode: 'duration' }); const padding = Math.max(1, selection.max - selection.min) * .1; navigation.show({ min: selection.min - padding, max: selection.max + padding }); }}>{t('定位选区', 'Reveal')}</button>}
      <button className="cx-icon" aria-label={t('清除时间选区', 'Clear time selection')} title={t('清除时间选区', 'Clear time selection')} onClick={() => onView({ turnRange: null })}><Icon name="close" /></button>
      </> : <span>{t('选择范围后定位首条高亮记录，其他条目继续保留', 'Select a range to reveal its first highlighted record; other records stay visible')}</span>}
    </div>}
    {selectedCluster && <section ref={chooser} className="cx-cluster-picker" role="dialog" aria-label={t('选择聚合簇内的记录', 'Select a record in the cluster')} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeCluster(); } }}>
      <div className="cx-popover-head">{t(`${selectedCluster.events.length} 条记录 · 聚合显示`, `${selectedCluster.events.length} records · clustered`)}<button className="cx-icon" aria-label={t('关闭聚合记录', 'Close clustered records')} onClick={closeCluster}><Icon name="close" /></button></div>
      <p className="cx-cluster-note">{ordered ? t('位置仅表示已加载顺序，不表示持续时间。', 'Position only indicates loaded record order, not duration.') : t('聚合范围不表示单次操作时长；选择记录定位列表。', 'The cluster range does not indicate one operation’s duration. Select a record to locate it in the list.')}</p>
      <div className="cx-cluster-options">{selectedCluster.events.slice(clusterPage * 20, clusterPage * 20 + 20).map(event => <button key={event.eventId} onClick={() => { setCluster(null); props.onSelect(event.eventId); }}><span>{event.toolName ?? kindLabels[event.kind]} · {event.preview}</span><small>{ordered ? t('顺序', 'Order') : clockTime(event.timing.startMs, language)}</small></button>)}</div>
      <div className="cx-cluster-actions"><button className="cx-button" disabled={clusterPage === 0} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage - 1 })}>{t('上一页', 'Previous')}</button><span>{clusterPage + 1} / {Math.ceil(selectedCluster.events.length / 20)}</span><button className="cx-button" disabled={(clusterPage + 1) * 20 >= selectedCluster.events.length} onClick={() => setCluster({ key: selectedCluster.key, page: clusterPage + 1 })}>{t('下一页', 'Next')}</button><button className="cx-button" onClick={() => zoomCluster(selectedCluster)}>{t('放大此簇', 'Zoom cluster')}</button></div>
    </section>}
  </section>;
}
