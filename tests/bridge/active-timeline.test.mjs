/** 合成轮次验证活动时间的并集、跨空档投影及缺失边界降级，不读取真实任务。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildActiveTimeline } from '../../frontend/src/components/active-timeline.ts';

function timing(startMs, endMs = null, durationMs = endMs == null ? null : endMs - startMs, precision = endMs == null ? 'startOnly' : 'exact') {
  return { startMs, endMs, durationMs, precision };
}
function event(threadId, turnId, start, end = null, section = 'timeline') {
  return { threadId, turnId, order: { section }, timing: timing(start, end) };
}
function turn(threadId, turnId, ...intervals) { return { threadId, turnId, executionIntervals: intervals }; }

test('two one-minute turns separated by ten idle hours become a two-minute axis', () => {
  const epoch = 1792713600000, minute = 60000, gap = 10 * 60 * minute;
  const axis = buildActiveTimeline({ events: [], turns: [
    turn('main', 'one', timing(epoch, epoch + minute)),
    turn('main', 'two', timing(epoch + minute + gap, epoch + 2 * minute + gap)),
  ] });
  assert.equal(axis.mode, 'active');
  assert.equal(axis.durationMs, 2 * minute);
  assert.equal(axis.excludedMs, gap);
  assert.deepEqual(axis.segments, [
    { start: epoch, end: epoch + minute, offset: 0 },
    { start: epoch + minute + gap, end: epoch + 2 * minute + gap, offset: minute },
  ]);
  assert.equal(axis.toActive(epoch + minute + gap + 15000), 75000);
});

test('interrupted and resumed intervals in the same turn retain the interruption gap', () => {
  const axis = buildActiveTimeline({
    events: [event('main', 'same', 0), event('main', 'same', 1200)],
    turns: [turn('main', 'same', timing(0, 100), timing(1000, 1200))],
  });
  assert.equal(axis.mode, 'active');
  assert.equal(axis.durationMs, 300);
  assert.equal(axis.excludedMs, 900);
  assert.deepEqual(axis.projectRange(50, 1100), [
    { start: 50, end: 100, wallStart: 50, wallEnd: 100 },
    { start: 100, end: 200, wallStart: 1000, wallEnd: 1100 },
  ]);
});

test('model/tool waiting inside a running turn remains on the activity axis', () => {
  const axis = buildActiveTimeline({
    events: [event('main', 'one', 10), event('main', 'one', 20), event('main', 'one', 900)],
    turns: [turn('main', 'one', timing(0, 1000))],
  });
  assert.equal(axis.durationMs, 1000);
  assert.equal(axis.excludedMs, 0);
  assert.equal(axis.toActive(500), 500);
  assert.deepEqual(axis.projectRange(20, 900), [{ start: 20, end: 900, wallStart: 20, wallEnd: 900 }]);
});

test('parallel parent and child turns share one union rather than adding their durations', () => {
  const axis = buildActiveTimeline({ events: [], turns: [
    turn('main', 'one', timing(0, 100)), turn('child', 'one', timing(50, 150)), turn('other', 'one', timing(150, 200)),
  ] });
  assert.equal(axis.mode, 'active');
  assert.equal(axis.durationMs, 200);
  assert.deepEqual(axis.segments, [{ start: 0, end: 200, offset: 0 }]);
});

test('open intervals advance only to their own last observed event, never another turn or context', () => {
  const axis = buildActiveTimeline({ events: [
    event('main', 'open', 100, 150), event('main', 'open', 180),
    event('main', 'open', 999999, null, 'context'), event('child', 'open', 999999), event('main', 'other', 1000000),
  ], turns: [turn('main', 'open', timing(100)), turn('main', 'empty', timing(2000000))] });
  assert.equal(axis.mode, 'active');
  assert.equal(axis.durationMs, 80);
  assert.equal(axis.excludedMs, 0);
  assert.deepEqual(axis.segments, [{ start: 100, end: 180, offset: 0 }]);
  assert.equal(axis.toActive(999999), 80);
  assert.equal(axis.toWall(1000000), 180);
});

test('missing turn metadata falls back to each thread and turn observed envelope', () => {
  const axis = buildActiveTimeline({ events: [
    event('a', 'shared-id', 10), event('a', 'shared-id', 40),
    event('b', 'shared-id', 100), event('b', 'shared-id', 150),
    event('a', 'another', 1000), event('a', 'another', 1010),
  ], turns: [] });
  assert.equal(axis.mode, 'observed');
  assert.equal(axis.durationMs, 90);
  assert.equal(axis.excludedMs, 910);
  assert.deepEqual(axis.segments, [
    { start: 10, end: 40, offset: 0 }, { start: 100, end: 150, offset: 30 }, { start: 1000, end: 1010, offset: 80 },
  ]);
  assert.match(axis.explanation, /不代表精确执行时长/);
});

test('point-only or fully covered fallback turns do not downgrade known active intervals', () => {
  const axis = buildActiveTimeline({ events: [
    event('point-child', 'one', 10000), event('child', 'one', 20), event('child', 'one', 80),
  ], turns: [turn('main', 'one', timing(0, 100))] });
  assert.equal(axis.mode, 'active');
  assert.equal(axis.durationMs, 100);
  assert.equal(axis.excludedMs, 0);
  assert.equal(axis.toActive(10000), 100);
});

test('context points and null-turn records never create an execution envelope or leading padding', () => {
  const events = [event('main', null, 0), event('main', 'one', 10, 20, 'context')];
  const axis = buildActiveTimeline({ events, turns: [turn('main', 'one', timing(1000000, 1000100))] });
  assert.equal(axis.durationMs, 100);
  assert.equal(axis.excludedMs, 0);
  assert.equal(axis.toActive(0), 0);
  assert.deepEqual(axis.projectRange(10, 10), [{ start: 0, end: 0, wallStart: 10, wallEnd: 10 }]);
  const contextOnly = buildActiveTimeline({ events, turns: [] });
  assert.equal(contextOnly.mode, 'order');
  assert.equal(contextOnly.durationMs, 0);
  assert.deepEqual(contextOnly.segments, []);
});

test('mapping is nonlinear at idle gaps and inverse seam choices preserve both wall boundaries', () => {
  const axis = buildActiveTimeline({ events: [], turns: [turn('main', 'one', timing(100, 200), timing(1000, 1200))] });
  assert.equal(axis.toActive(0), 0);
  assert.equal(axis.toActive(150), 50);
  assert.equal(axis.toActive(200), 100);
  assert.equal(axis.toActive(700), 100);
  assert.equal(axis.toActive(1000), 100);
  assert.equal(axis.toActive(1100), 200);
  assert.equal(axis.toActive(9999), 300);
  assert.equal(axis.toWall(100, 'start'), 1000);
  assert.equal(axis.toWall(100, 'end'), 200);
  for (const wall of [100, 110, 150, 1001, 1100, 1200]) assert.equal(axis.toWall(axis.toActive(wall)), wall);
  assert.equal(axis.toWall(-1), 100);
  assert.equal(axis.toWall(Infinity), 1200);
  assert.deepEqual(axis.projectRange(0, 9999), [
    { start: 0, end: 100, wallStart: 100, wallEnd: 200 }, { start: 100, end: 300, wallStart: 1000, wallEnd: 1200 },
  ]);
  assert.deepEqual(axis.projectRange(300, 900), []);
  assert.deepEqual(axis.projectRange(700, 700), [{ start: 100, end: 100, wallStart: 700, wallEnd: 700 }]);
});

test('observed exact durations extend known evidence but missing start times stay unplaced', () => {
  const exact = event('main', 'one', 100);
  exact.timing = timing(100, null, 50, 'exact');
  const axis = buildActiveTimeline({ events: [exact, event('main', 'one', null), event('main', 'one', NaN)], turns: [] });
  assert.equal(axis.mode, 'observed');
  assert.equal(axis.durationMs, 50);
  assert.deepEqual(axis.segments, [{ start: 100, end: 150, offset: 0 }]);
  assert.deepEqual(axis.projectRange(NaN, 100), []);
  assert.deepEqual(axis.projectRange(100, Infinity), []);
  assert.deepEqual(axis.projectRange(200, 100), []);
});

test('relative mapping subtracts large wall timestamps before adding cumulative offsets', () => {
  const epoch = Number.MAX_SAFE_INTEGER - 1000;
  const axis = buildActiveTimeline({ events: [], turns: [
    turn('main', 'one', timing(epoch, epoch + 600), timing(epoch + 800, epoch + 1000)),
  ] });
  assert.equal(axis.durationMs, 800);
  assert.equal(axis.toWall(700), epoch + 900);
  assert.equal(axis.toWall(600, 'start'), epoch + 800);
  assert.equal(axis.toWall(600, 'end'), epoch + 600);
  assert.deepEqual(axis.projectRange(epoch + 800, epoch + 1000), [
    { start: 600, end: 800, wallStart: epoch + 800, wallEnd: epoch + 1000 },
  ]);
});

test('empty and point-only timelines remain finite without invented duration', () => {
  for (const snapshot of [null, { events: [], turns: [] }, { events: [event('main', 'one', null)], turns: [] }]) {
    const axis = buildActiveTimeline(snapshot);
    assert.equal(axis.mode, 'order');
    assert.equal(axis.durationMs, 0);
    assert.equal(axis.excludedMs, 0);
    assert.equal(axis.toActive(NaN), 0);
    assert.equal(axis.toWall(NaN), 0);
  }
  const point = buildActiveTimeline({ events: [event('main', 'one', 123)], turns: [] });
  assert.equal(point.mode, 'observed');
  assert.equal(point.durationMs, 0);
  assert.deepEqual(point.segments, []);
  assert.equal(point.toWall(0), 123);
  assert.deepEqual(point.projectRange(123, 123), [{ start: 0, end: 0, wallStart: 123, wallEnd: 123 }]);
});

test('building and projecting never mutate source timing, turn intervals, or event order', () => {
  const snapshot = { events: [event('main', 'one', 50), event('main', 'one', 10)], turns: [
    turn('main', 'one', timing(100, 200), timing(0, 60)),
  ] };
  const before = structuredClone(snapshot);
  const axis = buildActiveTimeline(snapshot);
  axis.toActive(100); axis.toWall(20); axis.projectRange(10, 150);
  assert.deepEqual(snapshot, before);
});

test('per-turn spans expose the same normalized metadata, resume boundaries, points, and fallback envelopes', () => {
  const axis = buildActiveTimeline({ events: [
    event('main', 'resumed', 0), event('main', 'resumed', 1100),
    event('child', 'fallback', 300), event('child', 'fallback', 350),
  ], turns: [
    turn('main', 'resumed', timing(1000), timing(0, 100), timing(0, 100), timing(500, 500)),
    turn('main', 'unplaced'), turn('main', 'not-yet-observed', timing(2000)),
  ] });
  assert.deepEqual(axis.turnSpans.get(JSON.stringify(['main', 'resumed'])), [
    { start: 0, end: 100 }, { start: 500, end: 500 }, { start: 1000, end: 1100 },
  ]);
  assert.deepEqual(axis.turnSpans.get(JSON.stringify(['child', 'fallback'])), [{ start: 300, end: 350 }]);
  assert.deepEqual(axis.turnSpans.get(JSON.stringify(['main', 'unplaced'])), []);
  assert.deepEqual(axis.turnSpans.get(JSON.stringify(['main', 'not-yet-observed'])), [{ start: 2000, end: 2000 }]);
  assert.equal(axis.durationMs, 250);
});
