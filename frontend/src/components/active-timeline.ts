/**
 * 用完整快照建立运行坐标；可选紧凑显示还会折叠轮次内没有记录覆盖的长空档。
 * 仅投影坐标：原事件时间、选择与筛选仍由上游持有；缺少边界时明确降级为观察范围，
 * 不使用当前时间补齐未结束轮次，也不把创建时间或上下文记录扩展为执行区间。
 */
import type { Timing, TraceEvent } from '../../../contracts/types';

type TimelineTiming = Pick<Timing, 'startMs' | 'endMs' | 'durationMs' | 'precision'>;
export type ActiveTimelineSnapshot = {
  events: ReadonlyArray<{
    threadId: string; turnId: string | null; timing: TimelineTiming;
    order?: Pick<TraceEvent['order'], 'section'>;
  }>;
  turns: ReadonlyArray<{
    threadId: string; turnId: string; executionIntervals?: readonly TimelineTiming[];
  }>;
};
/** offset 是该墙钟区间起点对应的累计毫秒；segments 只包含正宽且不相交的并集。 */
export type ActiveTimelineSegment = { start: number; end: number; offset: number };
/** 墙钟区间跨越已压缩空档时，返回多段；原墙钟边界供工具提示和定位使用。 */
export type ActiveTimelineRange = { start: number; end: number; wallStart: number; wallEnd: number };
/** 单轮自己的墙钟片段；保留明确中断后的恢复片段和已记录的零时长点。 */
export type TurnExecutionSpan = { start: number; end: number };
export type TimelineSpacing = 'runtime' | 'compact';
export const DEFAULT_TIMELINE_SPACING: TimelineSpacing = 'runtime';
export type ActiveTimeline = {
  mode: 'active' | 'observed' | 'order';
  spacing: TimelineSpacing;
  segments: ActiveTimelineSegment[];
  /** 当前显示坐标长度；compact 下不代表实际运行耗时。 */
  durationMs: number;
  runtimeDurationMs: number;
  foldedUnrecordedMs: number;
  /** 只含紧凑显示剪去的墙钟片段；offset 是该片段对应的显示接缝。 */
  folds: ActiveTimelineSegment[];
  /** 只计原轮次执行包络之间的间隔，不包含紧凑显示折叠。 */
  excludedMs: number;
  /** JSON.stringify([threadId,turnId]) 为键；缺少可定位证据的轮次对应空数组。 */
  turnSpans: ReadonlyMap<string, TurnExecutionSpan[]>;
  toActive: (wall: number) => number;
  toWall: (active: number, edge?: 'start' | 'end') => number;
  projectRange: (start: number, end: number) => ActiveTimelineRange[];
  /** 显示窗口对应的原运行跨度；接缝起点取右侧、终点取左侧。 */
  elapsedBetween: (min: number, max: number) => number;
  explanation: string;
};

type Span = { start: number; end: number };
function recordedTime(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
}

function observedEnd(timing: TimelineTiming): number | null {
  if (!recordedTime(timing.startMs)) return null;
  if (recordedTime(timing.endMs) && timing.endMs >= timing.startMs) return timing.endMs;
  if (timing.precision === 'exact' && recordedTime(timing.durationMs)
    && recordedTime(timing.startMs + timing.durationMs)) return timing.startMs + timing.durationMs;
  return timing.startMs;
}

function union(spans: Span[]): Span[] {
  const sorted = spans.filter(span => span.end > span.start).sort((a, b) => a.start - b.start || a.end - b.end);
  const result: Span[] = [];
  for (const span of sorted) {
    const previous = result.at(-1);
    if (previous && span.start <= previous.end) previous.end = Math.max(previous.end, span.end);
    else result.push({ ...span });
  }
  return result;
}

/** 给有序且不相交的正区间分配累计坐标，不修改墙钟边界。 */
function locatedSegments(spans: Span[]): ActiveTimelineSegment[] {
  let offset = 0;
  return spans.map(span => { const segment = { ...span, offset }; offset += span.end - span.start; return segment; });
}
function segmentDuration(segments: ActiveTimelineSegment[]) {
  const last = segments.at(-1);
  return last ? last.offset + (last.end - last.start) : 0;
}

/**
 * 只剪原运行包络内部没有记录覆盖的间隔。所有 Agent 的正事件区间共同保护，
 * 事件点及轮次边界也作为锚点；元数据包络本身不能保护空档，否则无法折叠同轮断连。
 * 每段超过 1 秒的空档保留两端各 500ms，剪中央部分，保持其余片段的真实时间比例。
 */
function compactRanges(runtime: Span[], evidence: Span[]): { retained: Span[]; cuts: Span[] } {
  const sorted = [...evidence].sort((a, b) => a.start - b.start || a.end - b.end);
  const protectedRanges: Span[] = [];
  for (const span of sorted) {
    const previous = protectedRanges.at(-1);
    if (previous && span.start <= previous.end) previous.end = Math.max(previous.end, span.end);
    else protectedRanges.push({ ...span });
  }
  const retained: Span[] = [], cuts: Span[] = [];
  let first = 0;
  for (const span of runtime) {
    while (first < protectedRanges.length && protectedRanges[first].end < span.start) first++;
    let cursor = span.start, retainedStart = span.start;
    const gap = (end: number) => {
      if (end - cursor > 1000) {
        const cut = { start: cursor + 500, end: end - 500 };
        if (cut.start > retainedStart) retained.push({ start: retainedStart, end: cut.start });
        cuts.push(cut); retainedStart = cut.end;
      }
    };
    for (let index = first; index < protectedRanges.length && protectedRanges[index].start <= span.end; index++) {
      const item = protectedRanges[index], start = Math.max(span.start, item.start), end = Math.min(span.end, item.end);
      if (start > cursor) gap(start);
      cursor = Math.max(cursor, end);
    }
    if (cursor < span.end) gap(span.end);
    if (retainedStart < span.end) retained.push({ start: retainedStart, end: span.end });
  }
  return { retained, cuts };
}

/** 两种轴共用一比一保留片段；空档全部映射到接缝，逆向按端点选择接缝两侧。 */
function timelineProjection(segments: ActiveTimelineSegment[], emptyReference: number) {
  const toActive = (wall: number) => {
    if (!segments.length || Number.isNaN(wall)) return 0;
    let low = 0, high = segments.length;
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2);
      if (segments[middle].start <= wall) low = middle + 1;
      else high = middle;
    }
    if (!low) return 0;
    const segment = segments[low - 1];
    return segment.offset + Math.min(segment.end - segment.start, wall - segment.start);
  };
  const toWall = (active: number, edge: 'start' | 'end' = 'start') => {
    if (!segments.length) return emptyReference;
    const position = Number.isNaN(active) ? 0 : Math.max(0, Math.min(segmentDuration(segments), active));
    let low = 0, high = segments.length;
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2), segment = segments[middle];
      const before = edge === 'end' ? segment.offset + (segment.end - segment.start) < position : segment.offset <= position;
      if (before) low = middle + 1;
      else high = middle;
    }
    const segment = segments[edge === 'end' ? Math.min(segments.length - 1, low) : Math.max(0, low - 1)];
    return segment.start + (position - segment.offset);
  };
  const projectRange = (start: number, end: number): ActiveTimelineRange[] => {
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return [];
    if (start === end) {
      const position = toActive(start);
      return [{ start: position, end: position, wallStart: start, wallEnd: end }];
    }
    const result: ActiveTimelineRange[] = [];
    let low = 0, high = segments.length;
    while (low < high) {
      const middle = low + Math.floor((high - low) / 2);
      if (segments[middle].end <= start) low = middle + 1;
      else high = middle;
    }
    for (let index = low; index < segments.length && segments[index].start < end; index++) {
      const segment = segments[index];
      const wallStart = Math.max(start, segment.start), wallEnd = Math.min(end, segment.end);
      result.push({ start: segment.offset + (wallStart - segment.start), end: segment.offset + (wallEnd - segment.start), wallStart, wallEnd });
    }
    return result;
  };
  return { toActive, toWall, projectRange };
}

/**
 * 始终接收全快照以共享 Agent 坐标。轮次元数据优先；没有可用元数据的轮次只使用
 * 自己已加载事件的观察包络，并以 observed 提醒边界不足，不能据此宣称精确执行时长。
 * 空档中的点映射到同一接缝；toWall 的 start 选择接缝右侧，end 选择左侧。
 */
export function buildActiveTimeline(snapshot: ActiveTimelineSnapshot | null, spacing: TimelineSpacing = DEFAULT_TIMELINE_SPACING): ActiveTimeline {
  const observed = new Map<string, Span>();
  const protectedSpans: Span[] = [];
  const metadata = new Map<string, TimelineTiming[]>();
  const key = (threadId: string, turnId: string) => JSON.stringify([threadId, turnId]);
  let reference = Infinity;
  for (const event of snapshot?.events ?? []) {
    if (!event.turnId || event.order?.section === 'context' || !recordedTime(event.timing.startMs)) continue;
    const end = observedEnd(event.timing)!;
    protectedSpans.push({ start: event.timing.startMs, end });
    const id = key(event.threadId, event.turnId), previous = observed.get(id);
    if (previous) {
      previous.start = Math.min(previous.start, event.timing.startMs);
      previous.end = Math.max(previous.end, end);
    } else observed.set(id, { start: event.timing.startMs, end });
    reference = Math.min(reference, event.timing.startMs);
  }
  for (const turn of snapshot?.turns ?? []) {
    const id = key(turn.threadId, turn.turnId);
    const intervals = metadata.get(id) ?? [];
    intervals.push(...turn.executionIntervals ?? []);
    metadata.set(id, intervals);
  }

  const known: Span[] = [], fallback: Span[] = [];
  const turnSpans = new Map<string, TurnExecutionSpan[]>();
  for (const id of new Set([...observed.keys(), ...metadata.keys()])) {
    const envelope = observed.get(id);
    const spans: TurnExecutionSpan[] = [];
    let hasMetadata = false;
    for (const timing of metadata.get(id) ?? []) {
      if (!recordedTime(timing.startMs)) continue;
      const start = timing.startMs;
      let end: number;
      if (timing.endMs != null) {
        if (!recordedTime(timing.endMs) || timing.endMs < start) continue;
        end = timing.endMs;
      } else if (timing.precision === 'exact' && timing.durationMs != null) {
        if (!recordedTime(timing.durationMs) || !recordedTime(start + timing.durationMs)) continue;
        end = start + timing.durationMs;
      } else {
        // 开放轮次只能推进到本轮已观察边界，不能借其他线程／轮次的记录或 Date.now。
        end = Math.max(start, envelope?.end ?? start);
      }
      hasMetadata = true;
      reference = Math.min(reference, start);
      known.push({ start, end });
      spans.push({ start, end });
    }
    // 已记录的中断／恢复片段之间必须保持空档，不能再用整轮事件包络填回来。
    if (!hasMetadata && envelope) {
      fallback.push(envelope);
      spans.push({ ...envelope });
    }
    // 轮次筛选复用与坐标轴相同的边界，不重建一个会跨过中断的整轮包络。
    // 只去掉重复片段；相邻恢复片段与零时长点仍保留其原始分段语义。
    spans.sort((a, b) => a.start - b.start || a.end - b.end);
    turnSpans.set(id, spans.filter((span, index) => !index || span.start !== spans[index - 1].start || span.end !== spans[index - 1].end));
    for (const span of spans) protectedSpans.push({ start: span.start, end: span.start }, { start: span.end, end: span.end });
  }

  const knownUnion = union(known), combined = union([...knownUnion, ...fallback]);
  const runtimeSegments = locatedSegments(combined);
  const runtimeDurationMs = segmentDuration(runtimeSegments);
  const excludedMs = combined.reduce((sum, span, index) => sum + (index ? span.start - combined[index - 1].end : 0), 0);
  const compact = spacing === 'compact' ? compactRanges(combined, protectedSpans) : { retained: combined, cuts: [] };
  const segments = compact.cuts.length ? locatedSegments(compact.retained) : runtimeSegments;
  const durationMs = segmentDuration(segments);
  const foldedUnrecordedMs = compact.cuts.reduce((sum, span) => sum + (span.end - span.start), 0);
  const knownMs = knownUnion.reduce((total, span) => total + (span.end - span.start), 0);
  // 缺少边界的子轮次若完全落在已知父轮次内，不会给全局并集增加不确定时长。
  const mode = runtimeDurationMs > 0 ? runtimeDurationMs > knownMs ? 'observed' : 'active' : reference < Infinity ? 'observed' : 'order';
  const emptyReference = Number.isFinite(reference) ? reference : 0;

  const projection = timelineProjection(segments, emptyReference);
  const runtimeProjection = segments === runtimeSegments ? projection : timelineProjection(runtimeSegments, emptyReference);
  const folds = compact.cuts.map(span => ({ ...span, offset: projection.toActive(span.start) }));
  const elapsedBetween = (min: number, max: number) => {
    if (Number.isNaN(min) || Number.isNaN(max) || max <= min) return 0;
    const start = Math.max(0, Math.min(durationMs, min)), end = Math.max(0, Math.min(durationMs, max));
    if (end <= start) return 0;
    return Math.max(0, runtimeProjection.toActive(projection.toWall(end, 'end'))
      - runtimeProjection.toActive(projection.toWall(start, 'start')));
  };
  return {
    mode, spacing, segments, durationMs, runtimeDurationMs, foldedUnrecordedMs, folds, excludedMs, turnSpans, ...projection, elapsedBetween,
    explanation: spacing === 'compact' && foldedUnrecordedMs > 0 ? '紧凑坐标折叠超过 1 秒且没有记录覆盖的间隔，仅保留间隔两端各 500 毫秒；不代表精确运行耗时，也不推断断网原因。'
      : mode === 'active' ? '按已记录轮次执行区间累计，轮次间隔已排除；未结束轮次只推进到本轮最后已记录时间。'
      : mode === 'observed' ? '缺少完整轮次执行边界；按同轮已观察记录范围显示，不代表精确执行时长。'
        : '没有可定位的轮次时间，按记录顺序显示。',
  };
}
