import test from 'node:test';
import assert from 'node:assert/strict';
import { buildTurnFlow, logDurationWidth, flowDuration } from '../../frontend/src/components/turn-flow.ts';

function e(id, start, end = null, extra = {}) {
  return { eventId: id, threadId: 'main', turnId: 't1', kind: 'toolCall', order: { ordinal: 0, section: 'timeline' },
    timing: { startMs: start, endMs: end, durationMs: end == null ? null : end - start, precision: end == null ? 'startOnly' : 'exact', basis: 'item 包装层毫秒时间' }, ...extra };
}
const data = events => ({ events, turns: [{ threadId: 'main', turnId: 't1', order: 0 }] });
const at = (flow, id) => flow.nodes.find(n => n.events.some(e => e.eventId === id));

test('one shared log scale is monotonic and preserves raw durations without a hard cap', () => {
  const ds = [0, 1, 10, 100, 1000, 60000, 3600000];
  assert.equal(logDurationWidth(0), 16);
  for (let i = 1; i < ds.length; i++) assert.ok(logDurationWidth(ds[i]) > logDurationWidth(ds[i - 1]));
  const a = e('a', 100, 110), b = e('b', 100, 110, { kind: 'reasoningSummary' });
  const flow = buildTurnFlow(data([a, b]));
  assert.equal(at(flow, 'a').barWidth, at(flow, 'b').barWidth);
  assert.equal(at(flow, 'a').durationMs, 10);
  assert.equal(logDurationWidth(NaN), 0);
});

test('serial lifecycle order uses boundaries even when completion records arrive in a different order', () => {
  const source = data([e('late', 200, 300, { order: { ordinal: 0 } }), e('early', 0, 100, { order: { ordinal: 1 } })]);
  const before = structuredClone(source), flow = buildTurnFlow(source);
  assert.ok(at(flow, 'early').x < at(flow, 'late').x);
  assert.deepEqual(flow.edges, [{ from: 'early', to: 'late' }]);
  assert.deepEqual(source, before);
});

test('a long background operation overlaps shorter work without making the shorter work mutually parallel', () => {
  const flow = buildTurnFlow(data([e('background', 0, 1000), e('first', 10, 20), e('next', 30, 40)]));
  assert.equal(at(flow, 'background').x, at(flow, 'first').x);
  assert.notEqual(at(flow, 'background').row, at(flow, 'first').row);
  assert.ok(at(flow, 'next').x > at(flow, 'first').x);
  assert.deepEqual(flow.edges, [{ from: 'first', to: 'next' }]);
  assert.equal(flow.overlaps.length, 1);
  assert.equal(flow.overlaps[0].nodeKeys.length, 3);
});

test('context and saved-time points never become model stages or lifecycle precedence edges', () => {
  const flow = buildTurnFlow(data([
    e('system', 0, null, { kind: 'systemContext' }), e('dev', 1, null, { kind: 'developerContext' }),
    e('saved-summary', 5, null, { kind: 'reasoningSummary' }), e('model', 10, 20, { kind: 'assistantMessage' }),
  ]));
  assert.equal(flow.nodes.filter(n => n.context).length, 2);
  assert.ok(flow.nodes.every(n => n.events.length === 1));
  assert.equal(at(flow, 'saved-summary').basis, 'recordPosition');
  assert.deepEqual(flow.edges, []);
  assert.equal(flow.nodes.filter(n => n.lane === 1).length, 2);
});

test('model usage evidence never creates a node or an empty turn in the execution flow', () => {
  const usage = e('usage', 5, null, { kind: 'modelUsage', turnId: 'usage-only' });
  const source = data([e('model', 10, 20, { kind: 'assistantMessage' }), usage]);
  const before = structuredClone(source), flow = buildTurnFlow(source);
  assert.deepEqual(flow.nodes.flatMap(n => n.events.map(event => event.eventId)), ['model']);
  assert.deepEqual(flow.turns.map(turn => turn.turnId), ['t1']);
  assert.deepEqual(source, before);
  assert.deepEqual(buildTurnFlow(data([usage])).nodes, []);
});

test('zero, missing duration and duration-only evidence remain distinct', () => {
  const timed = e('duration-only', null, null, { timing: { startMs: null, endMs: null, durationMs: 250, precision: 'durationOnly', basis: 'duration' } });
  const flow = buildTurnFlow(data([e('zero', 0, 0), e('missing', 1), timed]));
  assert.equal(at(flow, 'zero').durationMs, 0);
  assert.equal(at(flow, 'missing').durationMs, null);
  assert.equal(at(flow, 'duration-only').durationMs, 250);
  assert.equal(at(flow, 'duration-only').basis, 'unknown');
  assert.ok(at(flow, 'duration-only').barWidth > 0);
  assert.deepEqual(flow.edges, []);
  assert.equal(flowDuration(timed), 250);
});

test('equal timestamps do not manufacture a strict order or a positive overlap', () => {
  const flow = buildTurnFlow(data([e('a', 10, 10), e('b', 10, 10)]));
  assert.deepEqual(flow.edges, []);
  assert.deepEqual(flow.overlaps, []);
});

test('turn segmentation is keyed by thread and turn and does not connect different turns', () => {
  const source = { events: [e('main', 100, 200), e('child', 0, 90, { threadId: 'child' }), e('next', 300, 400, { turnId: 't2' })],
    turns: [{ threadId: 'main', turnId: 't1', order: 0 }, { threadId: 'child', turnId: 't1', order: 0 }, { threadId: 'main', turnId: 't2', order: 1 }] };
  const flow = buildTurnFlow(source);
  assert.equal(flow.turns.length, 3);
  assert.equal(new Set(flow.turns.map(t => t.key)).size, 3);
  assert.deepEqual(flow.edges, []);
});

test('ten thousand records produce finite layout without spreading unbounded arrays', () => {
  const flow = buildTurnFlow(data(Array.from({ length: 10000 }, (_, i) => e(String(i), i * 2, i * 2 + 1, { order: { ordinal: i } }))));
  assert.equal(flow.nodes.length, 10000);
  assert.equal(flow.edges.length, 9999);
  assert.ok(Number.isFinite(flow.width));
});
