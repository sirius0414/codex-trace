/** 将已有事件区间的并集铺满可见时间轴；空档单独归为未归因，不反推模型/工具耗时。 */
import { useId, useMemo } from 'react';
import type { TraceEvent } from '../../../contracts/types';
import { usePreferences } from '../preferences';
import Icon from './icons';
import { duration, overviewLane, recordedEnd } from './model';
import { coverageMarks, coverageTotals, timelineCoverage } from './timeline-coverage';

type Props = {
  events: TraceEvent[];
  domain: { min: number; max: number };
  viewport: { min: number; max: number };
  width: number;
  formatTime: (value: number) => string;
};

export default function TimelineCoverage({ events, domain, viewport, width, formatTime }: Props) {
  const { t } = usePreferences();
  const id = useId();
  const coverage = useMemo(() => timelineCoverage(events.flatMap(event => {
    const start = event.timing.startMs, end = recordedEnd(event);
    // 保存时间点和仅有时长的记录都不能构成可定位的持续区间。
    return start != null && end != null && end > start ? [{ start, end, lane: overviewLane(event) }] : [];
  }), domain.min, domain.max), [events, domain.min, domain.max]);
  const marks = useMemo(() => coverageMarks(coverage.segments, viewport.min, viewport.max, Math.floor(width / 3)), [coverage, viewport.min, viewport.max, width]);
  const { recordedMs: recorded, unattributedMs: unknown } = useMemo(() => coverageTotals(coverage.segments, viewport.min, viewport.max), [coverage, viewport.min, viewport.max]);
  const explanation = t('彩色表示有起止时间或明确时长的记录覆盖；重叠区间只计一次。斜线表示未归因，可能包含等待、调度、未记录或尚未加载的时间，不能归为模型耗时。点纹表示密集区间聚合；聚合中含未归因时仍保留斜线。创建时间不代表本轮执行开始。输入等瞬时记录不占持续时间；聚合簇的外框也不算连续执行。', 'Colors show recorded intervals with start/end times or an explicit duration; overlapping intervals count once. Hatching is unattributed and may include waiting, scheduling, unrecorded or unloaded time. It is not model compute time. Dots indicate aggregated intervals; mixed intervals still show hatching when they include unattributed time. Creation time is not the start of this turn. Instantaneous inputs and cluster envelopes do not count as duration.');
  const summary = t(`当前范围：有时间记录 ${duration(recorded)}，未归因 ${duration(unknown)}`, `Visible range: recorded intervals ${duration(recorded)}, unattributed ${duration(unknown)}`);
  const label = (mask: number) => mask === 0 ? t('未归因（等待 / 未记录）', 'Unattributed (waiting / unrecorded)')
    : [1, 2, 4].filter(bit => mask & bit).map(bit => bit === 1 ? t('输入记录', 'Input records') : bit === 2 ? t('模型记录', 'Model records') : t('工具记录', 'Tool records')).join(' + ');
  const position = (time: number) => (time - viewport.min) / (viewport.max - viewport.min) * 1000;
  const fill = (mask: number) => mask === 1 ? 'var(--cx-blue)' : mask === 2 ? 'var(--cx-purple)' : mask === 4 ? 'var(--cx-green)' : `url(#${id}-${mask === 0 ? 'unknown' : `overlap-${mask}`})`;

  return <div className="cx-coverage" data-recorded-ms={recorded} data-unattributed-ms={unknown}>
    <span className="cx-coverage-label">{t('覆盖', 'Span')}</span>
    <div className="cx-coverage-body">
      <svg className="cx-coverage-track" viewBox="0 0 1000 10" preserveAspectRatio="none" role="img" aria-label={summary}>
        <defs>
          <pattern id={`${id}-unknown`} width="12" height="10" patternUnits="userSpaceOnUse"><rect width="12" height="10" fill="var(--cx-line)" /><path d="M-3 10 7 0M9 10 19 0" stroke="var(--cx-muted)" strokeWidth="1" /></pattern>
          <pattern id={`${id}-mixed`} width="12" height="10" patternUnits="userSpaceOnUse"><rect width="12" height="10" fill="var(--cx-line)" /><circle cx="3" cy="3" r="1.2" fill="var(--cx-ink)" /><circle cx="9" cy="8" r="1.2" fill="var(--cx-ink)" /></pattern>
          <pattern id={`${id}-mixed-unknown`} width="12" height="10" patternUnits="userSpaceOnUse"><rect width="12" height="10" fill={`url(#${id}-unknown)`} /><circle cx="3" cy="3" r="1.2" fill="var(--cx-ink)" /><circle cx="9" cy="8" r="1.2" fill="var(--cx-ink)" /></pattern>
          {[3, 5, 6, 7].map(mask => {
            const lanes = [1, 2, 4].filter(bit => mask & bit);
            return <pattern key={mask} id={`${id}-overlap-${mask}`} width="12" height="10" patternUnits="userSpaceOnUse">{lanes.map((lane, index) => <rect key={lane} x="0" y={index * 10 / lanes.length} width="12" height={10 / lanes.length} fill={fill(lane)} />)}</pattern>;
          })}
        </defs>
        {marks.map(mark => <rect key={mark.start} x={position(mark.start)} y="0" width={position(mark.end) - position(mark.start)} height="10" fill={mark.masks.length === 1 ? fill(mark.masks[0]) : `url(#${id}-${mark.masks.includes(0) ? 'mixed-unknown' : 'mixed'})`} data-coverage-masks={mark.masks.join(',')}>
          <title>{`${formatTime(mark.start)}–${formatTime(mark.end)} · ${mark.masks.length > 1 ? t('多阶段聚合，放大查看：', 'Aggregated phases; zoom to inspect: ') : ''}${mark.masks.map(label).join(' / ')}${mark.masks.some(mask => mask !== 0 && (mask & (mask - 1)) !== 0) ? t('（区间重叠）', ' (overlapping intervals)') : ''}`}</title>
        </rect>)}
      </svg>
      <div className="cx-coverage-caption"><span>{t('斜线：未归因 · 点纹：聚合', 'Hatching: unattributed · dots: grouped')}</span><span title={summary}>{t('未归因 ', 'Unattributed ')}{duration(unknown)}</span><span className="cx-coverage-info" tabIndex={0} role="note" title={explanation} aria-label={explanation}><Icon name="info" /></span></div>
    </div>
  </div>;
}
