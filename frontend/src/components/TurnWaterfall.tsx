import { useEffect, useMemo, useRef, useState } from 'react';
import type { TraceUIProps } from '../ui-types';
import type { TimeRange } from '../../../contracts/types';
import { usePreferences } from '../preferences';
import Icon from './icons';
import { buildTurnFlow, FLOW_ROW, type FlowNode } from './turn-flow';
import { clockTime, duration, localizedKindLabels, scopedEvents } from './model';
import { useTimelineViewport } from './useTimelineViewport';
import TimelineScrollbar from './TimelineScrollbar';

type Props = TraceUIProps & { highlightedEvents: ReadonlySet<string> | null; onRangeCommit: (range: TimeRange) => void };

/** 横向坐标只表示偏序布局，耗时条独立编码；不将布局坐标写回时间筛选。 */
export default function TurnWaterfall(props: Props) {
  const { snapshot, view, onView } = props;
  const { language, showSpanSummary, t } = usePreferences();
  const labels = localizedKindLabels(language);
  const svg = useRef<SVGSVGElement>(null), canvas = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);
  const events = useMemo(() => snapshot ? scopedEvents(snapshot, view) : [], [snapshot, view.agentScope]);
  const flow = useMemo(() => buildTurnFlow(snapshot ?? { events: [], turns: [] }, events), [snapshot?.turns, events]);
  const domainWidth = Math.max(flow.width, width);
  const resetKey = JSON.stringify([snapshot?.binding.threadId, view.agentScope]);
  const navigation = useTimelineViewport({ svg, wheelTarget: canvas, domain: { min: 0, max: domainWidth }, minimumSpan: 84, initialSpan: Math.max(280, width),
    resetKey, enabled: true, onInteract: () => { if (view.follow !== 'paused') onView({ follow: 'paused' }); } });
  const { min, max } = navigation.viewport, span = max - min;
  const progress = useRef({ key: resetKey, count: 0, width: 0 });
  useEffect(() => {
    const previous = progress.current;
    progress.current = { key: resetKey, count: flow.nodes.length, width: flow.width };
    if (previous.key === resetKey && previous.count > 0 && flow.nodes.length > previous.count && flow.width > previous.width && flow.width > span && view.follow === 'following') {
      navigation.show({ min: Math.max(0, flow.width - span), max: flow.width }, false);
    }
  }, [flow.width, flow.nodes.length, resetKey, view.follow]);
  const zoomed = min > 0 || max < domainWidth;
  const scale = domainWidth / span;
  // 分支各占一行；分支密集时使用小色块，超出宿主高度可滚动，不把多条记录合并。
  const laneStep = flow.laneRows.map(rows => rows > 4 ? 10 : FLOW_ROW);
  const laneHeight = flow.laneRows.map((rows, lane) => rows * laneStep[lane]);
  const laneTop = [24, 24 + laneHeight[0] + 8, 24 + laneHeight[0] + laneHeight[1] + 16];
  const height = laneTop[2] + laneHeight[2] + 8;
  const x = (position: number) => (position - min) / span * 1000;
  const y = (n: Pick<FlowNode, 'lane' | 'row'>) => laneTop[n.lane] + n.row * laneStep[n.lane];
  const explanation = t('横轴按 Turn 分段；位置是执行顺序布局，不是时间刻度。每条记录独立显示，重叠活动分行保留；空间不足时只显示色块，悬浮或点击查看原记录。记录位置、缺少时序和未知耗时均明确标记。节点内条长统一按 16 + 24 × log10(1 + 毫秒 / 10) 缩放，原始耗时保留。没有补画模型阶段。', 'The axis is segmented by Turn; positions represent execution-order layout, not elapsed time. Each record has its own node; overlapping activities use separate rows. Compact nodes show only color; hover or select for the original record. Record positions, unknown order and missing durations are marked. Inner bars use 16 + 24 × log10(1 + milliseconds / 10), retaining original durations. No model stages are invented.');
  const marks = useMemo(() => flow.nodes.filter(n => n.x <= max && n.x + n.width >= min), [flow, min, max]);
  useEffect(() => {
    if (!svg.current) return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(1, entries[0].contentRect.width)));
    observer.observe(svg.current); return () => observer.disconnect();
  }, []);
  const visibleTurns = flow.turns.filter(turn => turn.end >= min && turn.x <= max);
  const labelledTurns = new Set<string>();
  let labelRight = -Infinity;
  visibleTurns.forEach((turn, index) => {
    const left = Math.max(0, (turn.x - min) / span * width);
    const next = visibleTurns[index + 1];
    // 窄屏全貌优先显示 Turn 编号；上下文标签不能挤掉首轮，分段边界始终保留。
    if (turn.context && next && (next.x - min) / span * width < left + 45) return;
    if (left < labelRight) return;
    labelledTurns.add(turn.key);
    labelRight = left + (turn.context ? 45 : 76);
  });
  return <div className="cx-turn-flow">
    <div className="cx-flow-toolbar">
      <span className="cx-flow-legend" title={explanation}>{t('条内长度：对数耗时 · ∥：区间重叠', 'Inner bar: log duration · ∥: overlapping intervals')}</span>
      <div className="cx-zoom-controls" role="group" aria-label={t('顺序图缩放', 'Sequence zoom')}>
        <button className="cx-icon cx-zoom" aria-label={t('缩小顺序图', 'Zoom out sequence')} disabled={!zoomed} onClick={() => navigation.zoom(2)}><Icon name="zoomOut" /></button>
        <span className="cx-zoom-level">{Number(scale.toFixed(1))}×</span>
        <button className="cx-icon cx-zoom" aria-label={t('放大顺序图', 'Zoom in sequence')} disabled={!events.length || span <= 84} onClick={() => navigation.zoom(.5)}><Icon name="zoomIn" /></button>
        <button className="cx-button" aria-label={t('恢复全部概览范围', 'Show full overview')} disabled={!zoomed} onClick={navigation.reset}>{t('全貌', 'Reset')}</button>
      </div>
    </div>
    <div className="cx-chart cx-flow-chart">
      <div className="cx-flow-lanes" style={{ height }}>{[t('输入', 'Input'), t('模型', 'Model'), t('工具', 'Tools')].map((label, i) => <span key={i} style={{ top: laneTop[i] + 6 }}>{label}</span>)}</div>
      <div ref={canvas} className="cx-timeline-canvas">
        <svg ref={svg} className={`cx-timeline cx-sequence-svg ${navigation.dragging ? 'dragging' : ''}`} style={{ height }} viewBox={`0 0 1000 ${height}`} preserveAspectRatio="none" tabIndex={0} role="group" aria-label={explanation}
          data-axis-mode="turn-sequence" data-duration-scale="log1p" data-layout-width={domainWidth} data-viewport-min={min} data-viewport-max={max}
          {...navigation.svgHandlers} onDoubleClick={event => { if (!(event.target as Element).closest('[data-overview-key]')) navigation.reset(); }}>
          {visibleTurns.map(turn => <g key={turn.key} className="cx-flow-turn" data-turn-id={turn.turnId ?? 'context'}>
            <title>{turn.context ? t('上下文', 'Context') : turn.order == null ? t('Turn · 未编号', 'Turn · unnumbered') : `Turn ${turn.order + 1}`}</title>
            <line className="cx-gridline" x1={x(turn.x)} x2={x(turn.x)} y1="0" y2={height} />
            {labelledTurns.has(turn.key) && <text x={Math.max(0, x(turn.x)) + 4} y="14" transform={`translate(${Math.max(0, x(turn.x)) + 4} 14) scale(${1000 / width} 1) translate(${-Math.max(0, x(turn.x)) - 4} -14)`}>{turn.context ? t('上下文', 'Context') : turn.order == null ? t('Turn · 未编号', 'Turn · unnumbered') : `Turn ${turn.order + 1}`}</text>}
          </g>)}
          {marks.map(n => {
            const e = n.events[0], selected = e.eventId === view.selectedEventId, highlighted = props.highlightedEvents?.has(e.eventId);
            const left = x(n.x), extent = n.width / span * 1000, top = y(n), pixelWidth = extent * width / 1000, nodeHeight = laneStep[n.lane] - 5;
            const label = (e.toolName ?? labels[e.kind]) + (n.parallel ? ' ∥' : '');
            const rawDuration = n.durationMs == null ? t('时长未记录', 'Duration not recorded') : duration(n.durationMs);
            const value = n.durationMs == null ? n.basis === 'unknown' ? t('时序未知', 'No order') : t('未记录', 'Unknown') : rawDuration;
            // 字宽按保守值估算；容纳不下完整名称和耗时时，整条仅保留色块。
            const labelWidth = [...label].reduce((sum, char) => sum + (char.charCodeAt(0) > 255 ? 10 : 6), 0);
            const compact = nodeHeight < 25 || pixelWidth < Math.max(58, labelWidth + 8, value.length * 9 + 16);
            const basis = n.basis === 'recordPosition' ? t('记录位置', 'Record position') : n.basis === 'unknown' ? t('时序未确认', 'Order unconfirmed') : n.basis === 'callInterval' ? t('调用至返回', 'Call to return') : t('条目起止', 'Item lifecycle');
            return <g key={n.key} role="button" tabIndex={0} data-overview-key={n.key} data-event-id={e.eventId} data-order-basis={n.basis} data-node-duration-ms={n.durationMs ?? undefined} data-parallel={n.parallel}
              aria-label={`${label} · ${rawDuration} · ${basis}`}
              className={`cx-flow-node lane-${n.lane} ${compact ? 'compact' : ''} ${selected ? 'selected' : ''} ${highlighted ? 'highlighted' : ''} ${e.status.value}`}
              onClick={() => props.onSelect(e.eventId)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); props.onSelect(e.eventId); } }}>
              <title>{`${e.toolName ?? labels[e.kind]} · ${e.preview}\n${rawDuration}${n.durationMs == null ? '' : ` (${n.durationMs} ms)`} · ${basis}\n${e.timing.basis}${n.parallel ? '\n' + t('与其他已记录区间有重叠', 'Overlaps other recorded intervals') : ''}`}</title>
              <rect className="cx-flow-node-shell" data-log-width={compact ? n.barWidth : undefined} x={left} y={top} width={extent} height={nodeHeight} rx={compact ? 1 : 3} />
              {!compact && n.durationMs != null && n.durationMs > 0 && <rect className="cx-flow-duration" data-log-width={n.barWidth} x={left + 4 / span * 1000} y={top + 18} width={n.barWidth / span * 1000} height="4" rx="1" />}
              {!compact && (n.durationMs == null || n.durationMs === 0) && <path className="cx-flow-point" d={n.durationMs === 0 ? `M${left + 5 / span * 1000} ${top + 17}v5` : `M${left + 4 / span * 1000} ${top + 19}h${5 / span * 1000}`} />}
              {!compact && <>
                <text transform={`translate(${left + 4 / width * 1000} ${top + 11}) scale(${1000 / width} 1)`}>{label}</text>
                <text className="cx-flow-value" transform={`translate(${left + 11 / width * 1000} ${top + 22}) scale(${1000 / width} 1)`}>{value}</text>
              </>}
            </g>;
          })}
        </svg>
      </div>
      {!events.length && <span className="cx-chart-empty">{snapshot ? t('此 Agent 暂无可展示的记录', 'No records for this agent') : t('选择任务查看轨迹', 'Select a task to view its trace')}</span>}
    </div>
    <div className="cx-flow-caption"><span>{t('Turn 顺序 · 内条：对数耗时', 'Turn sequence · inner bar: log duration')}</span><span>{t('∥：区间重叠', '∥: overlapping intervals')}</span></div>
    {showSpanSummary && <TimelineScrollbar domain={{ min: 0, max: domainWidth }} viewport={{ min, max }} width={width} label={t('平移执行顺序图', 'Pan execution sequence')} valueText={t(`显示 ${Math.round(span / domainWidth * 100)}% 的顺序范围`, `Showing ${Math.round(span / domainWidth * 100)}% of the sequence`)} title={t('布局位置不是时间值', 'Layout positions are not time values')} onChange={navigation.show} />}
    {view.turnRange && <div className="cx-turn-range-note"><span>{t('保留的原时间高亮', 'Retained original-time highlight')} · {clockTime(view.turnRange.startMs, language)}–{clockTime(view.turnRange.endMs, language)}</span><button className="cx-icon" aria-label={t('清除时间选区', 'Clear time selection')} onClick={() => onView({ turnRange: null })}><Icon name="close" /></button></div>}
  </div>;
}
