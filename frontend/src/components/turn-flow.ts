/** Turn 内的生命周期偏序布局。坐标是布局单位，绝不反解为时间。 */
import type { ThreadSnapshot, TraceEvent } from '../../../contracts/types';
import { overviewLane, recordedEnd } from './model.ts';

export const FLOW_ROW = 30;
export const FLOW_PAD = 14;
const contexts = new Set(['systemContext', 'developerContext', 'environment']);
const valid = (n: number | null | undefined): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= Number.MAX_SAFE_INTEGER;
export function logDurationWidth(durationMs: number): number {
  return valid(durationMs) ? 16 + 24 * Math.log10(1 + durationMs / 10) : 0;
}
export function flowDuration(event: TraceEvent): number | null {
  if (valid(event.timing.durationMs)) return event.timing.durationMs;
  const start = event.timing.startMs, end = event.timing.endMs;
  return valid(start) && valid(end) && end >= start ? end - start : null;
}
export type FlowNode = {
  key: string; events: TraceEvent[]; lane: number; x: number; width: number; row: number;
  durationMs: number | null; barWidth: number; context: boolean;
  basis: 'lifecycle' | 'callInterval' | 'recordPosition' | 'unknown';
  start: number | null; end: number | null; parallel: boolean;
};
export type FlowTurn = { key: string; threadId: string; turnId: string | null; order: number | null; x: number; end: number; context: boolean };
export type FlowOverlap = { key: string; start: number; end: number; nodeKeys: string[] };
export type FlowEdge = { from: string; to: string };
export type TurnFlow = { nodes: FlowNode[]; turns: FlowTurn[]; overlaps: FlowOverlap[]; edges: FlowEdge[]; width: number; laneRows: number[] };

function node(e: TraceEvent): FlowNode {
  const context = contexts.has(e.kind), durationMs = flowDuration(e);
  const start = valid(e.timing.startMs) ? e.timing.startMs : null;
  const end = recordedEnd(e);
  const interval = !context && e.kind !== 'modelUsage' && start != null && end != null && end >= start && e.timing.precision === 'exact';
  const basis = interval ? /call_id/.test(e.timing.basis) ? 'callInterval' : 'lifecycle' : start == null ? 'unknown' : 'recordPosition';
  const barWidth = durationMs != null && durationMs > 0 ? logDurationWidth(durationMs) : 0;
  return { key: e.eventId, events: [e], lane: overviewLane(e),
    x: 0, width: Math.max(84, barWidth + 16), row: 0, durationMs, barWidth, context, basis, start, end, parallel: false };
}

/** 最小堆只用于已记录结束边界；偏序层级不会添加未知等待阶段。 */
function push(heap: FlowNode[], item: FlowNode) {
  heap.push(item); let i = heap.length - 1;
  while (i > 0) { const p = (i - 1) >> 1; if (heap[p].end! <= item.end!) break; heap[i] = heap[p]; i = p; }
  heap[i] = item;
}
function pop(heap: FlowNode[]): FlowNode {
  const first = heap[0], tail = heap.pop()!;
  if (heap.length) {
    let i = 0;
    while (i * 2 + 1 < heap.length) {
      let c = i * 2 + 1; if (c + 1 < heap.length && heap[c + 1].end! < heap[c].end!) c++;
      if (heap[c].end! >= tail.end!) break; heap[i] = heap[c]; i = c;
    }
    heap[i] = tail;
  }
  return first;
}

export function buildTurnFlow(snapshot: Pick<ThreadSnapshot, 'events' | 'turns'>, events: TraceEvent[] = snapshot.events): TurnFlow {
  const result: TurnFlow = { nodes: [], turns: [], overlaps: [], edges: [], width: 0, laneRows: [1, 1, 1] };
  const groups = new Map<string, { threadId: string; turnId: string | null; events: TraceEvent[]; order: number | null; first: number }>();
  const metadata = new Map(snapshot.turns.map(t => [JSON.stringify([t.threadId, t.turnId]), t.order]));
  for (const e of events) {
    const key = JSON.stringify([e.threadId, e.turnId]);
    let group = groups.get(key);
    if (!group) { group = { threadId: e.threadId, turnId: e.turnId, events: [], order: e.turnId == null ? null : metadata.get(key) ?? null, first: e.order.ordinal }; groups.set(key, group); }
    group.events.push(e);
    group.first = Math.min(group.first, e.order.ordinal);
  }
  const ordered = [...groups.entries()].sort((a, b) => Number(a[1].turnId != null) - Number(b[1].turnId != null)
    || (a[1].order ?? Number.MAX_SAFE_INTEGER) - (b[1].order ?? Number.MAX_SAFE_INTEGER)
    || a[1].first - b[1].first || a[0].localeCompare(b[0]));
  let cursor = 0;
  for (const [key, group] of ordered) {
    const turnStart = cursor;
    cursor += FLOW_PAD;
    const contextEvents = group.events.filter(e => contexts.has(e.kind)).sort((a, b) => a.order.ordinal - b.order.ordinal);
    for (const e of contextEvents) { const n = node(e); n.x = cursor; result.nodes.push(n); cursor += n.width + FLOW_PAD; }
    const entries = group.events.filter(e => !contexts.has(e.kind)).map(node);
    const timed = entries.filter(n => n.start != null).sort((a, b) => a.start! - b.start! || a.events[0].order.ordinal - b.events[0].order.ordinal || a.key.localeCompare(b.key));
    const unknown = entries.filter(n => n.start == null).sort((a, b) => a.events[0].order.ordinal - b.events[0].order.ordinal);
    // 相交的正生命周期区间形成重叠组；只有记录点时不会声称并行执行。
    const blocks: Array<{ nodes: FlowNode[]; end: number; overlap: boolean }> = [];
    for (const n of timed) {
      const strong = n.basis === 'lifecycle' || n.basis === 'callInterval';
      const last = blocks.at(-1);
      if (last && n.start! < last.end) {
        last.nodes.push(n);
        if (strong && n.end! > n.start!) { last.overlap = true; last.end = Math.max(last.end, n.end!); }
      } else blocks.push({ nodes: [n], end: strong && n.end! > n.start! ? n.end! : n.start!, overlap: false });
    }
    let previous: FlowNode | null = null;
    for (const block of blocks) {
      const blockStart = cursor, levels = new Map<string, number>(), heap: FlowNode[] = [];
      let finished: FlowNode | null = null, finishedLevel = -1;
      for (const n of block.nodes) {
        while (heap.length && heap[0].end! < n.start!) {
          const prior = pop(heap), level = levels.get(prior.key)!;
          if (level > finishedLevel) { finishedLevel = level; finished = prior; }
        }
        const level = finishedLevel + 1;
        levels.set(n.key, level);
        const strong = n.basis === 'lifecycle' || n.basis === 'callInterval';
        if (strong) {
          if (finished) result.edges.push({ from: finished.key, to: n.key });
          push(heap, n);
        }
      }
      const columns = new Map<number, FlowNode[]>();
      for (const n of block.nodes) { const l = levels.get(n.key)!; const list = columns.get(l) ?? []; list.push(n); columns.set(l, list); }
      for (const [, column] of [...columns].sort((a, b) => a[0] - b[0])) {
        const rows = [0, 0, 0];
        for (const n of column) {
          n.x = cursor; n.row = rows[n.lane]++;
          n.parallel = block.overlap && (n.basis === 'lifecycle' || n.basis === 'callInterval') && n.end! > n.start!;
          result.laneRows[n.lane] = Math.max(result.laneRows[n.lane], n.row + 1);
          result.nodes.push(n);
        }
        cursor += column.reduce((max, n) => Math.max(max, n.width), 0) + FLOW_PAD;
      }
      const strongNodes = block.nodes.filter(n => n.basis === 'lifecycle' || n.basis === 'callInterval');
      if (previous && strongNodes.length && previous.end! < strongNodes[0].start!) result.edges.push({ from: previous.key, to: strongNodes[0].key });
      previous = strongNodes.reduce<FlowNode | null>((p, n) => !p || n.end! > p.end! ? n : p, previous);
      if (block.overlap) result.overlaps.push({ key: `overlap:${key}:${blockStart}`, start: blockStart - 4, end: cursor - FLOW_PAD + 4,
        nodeKeys: block.nodes.filter(n => n.parallel).map(n => n.key) });
    }
    // 无定位时间的记录另列，不用 ordinal 伪造真实开始时刻或因果边。
    for (const n of unknown) { n.x = cursor; result.nodes.push(n); cursor += n.width + FLOW_PAD; }
    result.turns.push({ key, threadId: group.threadId, turnId: group.turnId, order: group.order, x: turnStart, end: cursor, context: group.turnId == null });
    cursor += 20;
  }
  result.width = Math.max(1, cursor);
  return result;
}
