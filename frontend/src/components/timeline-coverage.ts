/**
 * 将上游已确认的持续区间投影为完整时间轴覆盖，供概览绘制连续阶段。
 * 只合并输入证据；时间点、缺失范围与轨道空档不会被推断为执行时长。
 */
/** lane 仅接受 0/1/2；调用方负责保证区间来自已记录的持续时间。 */
export type CoverageSpan = { start: number; end: number; lane: number };
/** mask 的 bit 1/2/4 对应三条轨道；0 表示该区间没有持续时间归因。 */
export type CoverageSegment = { start: number; end: number; mask: number };

function validDomain(min: number, max: number) {
  return Number.isFinite(min) && Number.isFinite(max) && max > min && Number.isFinite(max - min);
}

function intersectingRange(segments: CoverageSegment[], min: number, max: number) {
  // 分区有序且不相交；二分跳过视口两侧的记录，缩放后无需扫描或复制全轴。
  let low = 0, high = segments.length;
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2);
    if (segments[middle].end <= min) low = middle + 1;
    else high = middle;
  }
  const first = low;
  high = segments.length;
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2);
    if (segments[middle].start < max) low = middle + 1;
    else high = middle;
  }
  return { first, last: low };
}

/**
 * 对 timelineCoverage 的原始有序分区统计视口内的精确覆盖，不创建裁剪副本。
 * 视口内没有持续时间证据的部分计入 unattributedMs；无效视口各项为 0。
 * 紧凑坐标可传入原运行跨度 spanMs，使被压缩的空档仍按原时间计入未归因。
 * 已记录区间与重叠仍按未压缩的正区间统计；无效跨度退回显示坐标跨度。
 */
export function coverageTotals(segments: CoverageSegment[], min: number, max: number, spanMs?: number): {
  recordedMs: number; unattributedMs: number; overlapMs: number;
} {
  if (!validDomain(min, max)) return { recordedMs: 0, unattributedMs: 0, overlapMs: 0 };
  const { first, last } = intersectingRange(segments, min, max);
  let recordedMs = 0, overlapMs = 0;
  for (let index = first; index < last; index++) {
    const segment = segments[index];
    const duration = Math.min(max, segment.end) - Math.max(min, segment.start);
    if (segment.mask) recordedMs += duration;
    if (segment.mask & (segment.mask - 1)) overlapMs += duration;
  }
  const elapsed = spanMs != null && Number.isFinite(spanMs) && spanMs >= 0 ? spanMs : max - min;
  return { recordedMs, unattributedMs: Math.max(0, elapsed - recordedMs), overlapMs };
}

/**
 * 在 [min,max) 内按所有轨道边界切分，并保留 mask=0 的空档。
 * recordedMs 为有证据区间的并集；overlapMs 仅计算不同轨道同时覆盖的并集，
 * 同轨道的并行区间只计一次。无效时间轴返回空结果，不反转或补造边界。
 */
export function timelineCoverage(spans: CoverageSpan[], min: number, max: number): {
  segments: CoverageSegment[]; recordedMs: number; unattributedMs: number; overlapMs: number;
} {
  const segments: CoverageSegment[] = [];
  if (!validDomain(min, max)) return { segments, recordedMs: 0, unattributedMs: 0, overlapMs: 0 };
  const edges: Array<{ time: number; lane: number; delta: number }> = [];
  for (const span of spans) {
    if (!Number.isFinite(span.start) || !Number.isFinite(span.end) || span.end <= span.start
      || !Number.isInteger(span.lane) || span.lane < 0 || span.lane > 2) continue;
    const start = Math.max(min, span.start), end = Math.min(max, span.end);
    if (end <= start) continue;
    edges.push({ time: start, lane: span.lane, delta: 1 }, { time: end, lane: span.lane, delta: -1 });
  }
  edges.sort((a, b) => a.time - b.time);
  const active = [0, 0, 0];
  let cursor = min, mask = 0;
  const append = (end: number) => {
    if (end <= cursor) return;
    const previous = segments.at(-1);
    if (previous?.mask === mask && previous.end === cursor) previous.end = end;
    else segments.push({ start: cursor, end, mask });
    cursor = end;
  };
  for (let index = 0; index < edges.length;) {
    const time = edges[index].time;
    append(time);
    // 同时开始／结束必须一次生效，不能生成零宽假空档或依赖排序的假重叠。
    do {
      const edge = edges[index++];
      active[edge.lane] += edge.delta;
    } while (index < edges.length && edges[index].time === time);
    mask = (active[0] > 0 ? 1 : 0) | (active[1] > 0 ? 2 : 0) | (active[2] > 0 ? 4 : 0);
  }
  append(max);
  return { segments, ...coverageTotals(segments, min, max) };
}

/**
 * 接收 timelineCoverage 的有序分区，用最多 1..400 个绘制标记展示。
 * 分区足够少时保留精确边界；过密时每格列出所有相交 mask（包含 0），
 * 不能把混合格解释为某一轨道连续占用，统计仍使用原始分区。
 */
export function coverageMarks(segments: CoverageSegment[], min: number, max: number, bins: number): Array<{
  start: number; end: number; masks: number[];
}> {
  if (!validDomain(min, max)) return [];
  const budget = Number.isFinite(bins) ? Math.max(1, Math.min(400, Math.floor(bins))) : 1;
  const { first, last } = intersectingRange(segments, min, max);
  const marks: Array<{ start: number; end: number; masks: number[] }> = [];
  if (last - first <= budget) {
    for (let index = first; index < last; index++) {
      const segment = segments[index];
      marks.push({ start: Math.max(min, segment.start), end: Math.min(max, segment.end), masks: [segment.mask] });
    }
    return marks;
  }

  const width = (max - min) / budget;
  let cursor = first;
  for (let bin = 0; bin < budget; bin++) {
    const start = min + bin * width, end = bin === budget - 1 ? max : min + (bin + 1) * width;
    if (end <= start) continue;
    while (cursor < last && segments[cursor].end <= start) cursor++;
    let seen = 0;
    for (let index = cursor; index < last && segments[index].start < end; index++) {
      if (segments[index].end > start) seen |= 1 << segments[index].mask;
    }
    const masks: number[] = [];
    for (let mask = 0; mask < 8; mask++) if (seen & (1 << mask)) masks.push(mask);
    marks.push({ start, end, masks });
  }
  return marks;
}
