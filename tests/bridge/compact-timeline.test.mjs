/** 紧凑轴只折叠没有记录覆盖的长空档；源耗时、轮次身份与墙钟边界均保持独立。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildActiveTimeline } from '../../frontend/src/components/active-timeline.ts';

const timing = (startMs, endMs = null, durationMs = endMs == null ? null : endMs - startMs, precision = endMs == null ? 'startOnly' : 'exact') => ({ startMs, endMs, durationMs, precision });
const event = (start, end = null, threadId = 'main', turnId = 'one', section = 'timeline') => ({ threadId, turnId, order: { section }, timing: timing(start, end) });
const turn = (start, end = null, threadId = 'main', turnId = 'one') => ({ threadId, turnId, executionIntervals: [timing(start, end)] });
const compact = snapshot => buildActiveTimeline(snapshot, 'compact');

test('a one-hour unrecorded gap inside one turn retains only one second of display space', () => {
  const data = { events: [event(0, 1000), event(3601000, 3602000)], turns: [turn(0, 3602000)] };
  const runtime = buildActiveTimeline(data), axis = compact(data);
  assert.equal(runtime.spacing, 'runtime');
  assert.equal(runtime.durationMs, 3602000);
  assert.equal(runtime.foldedUnrecordedMs, 0);
  assert.deepEqual(runtime.folds, []);
  assert.equal(axis.spacing, 'compact');
  assert.equal(axis.runtimeDurationMs, 3602000);
  assert.equal(axis.durationMs, 3000);
  assert.equal(axis.foldedUnrecordedMs, 3599000);
  assert.equal(axis.excludedMs, 0);
  assert.deepEqual(axis.folds, [{ start: 1500, end: 3600500, offset: 1500 }]);
  assert.deepEqual(axis.turnSpans, runtime.turnSpans);
  assert.equal(axis.toActive(3601000), 2000);
  assert.match(axis.explanation, /不推断断网原因/);
});

test('gaps of at most one second retain their full duration, and a 1001ms gap folds only one millisecond', () => {
  for (const gap of [0, 10, 1000, 1001]) {
    const axis = compact({ events: [event(0, 100), event(100 + gap, 200 + gap)], turns: [turn(0, 200 + gap)] });
    assert.equal(axis.durationMs, 200 + Math.min(1000, gap));
    assert.equal(axis.foldedUnrecordedMs, Math.max(0, gap - 1000));
    assert.equal(axis.runtimeDurationMs, axis.durationMs + axis.foldedUnrecordedMs);
  }
});

test('a recorded long operation remains full width even when its endpoints are hours apart', () => {
  const axis = compact({ events: [event(0, 7200000)], turns: [turn(0, 7200000)] });
  assert.equal(axis.durationMs, 7200000);
  assert.equal(axis.runtimeDurationMs, 7200000);
  assert.equal(axis.foldedUnrecordedMs, 0);
  assert.deepEqual(axis.folds, []);
  assert.equal(axis.toActive(3600000), 3600000);
});

test('an exact recorded duration without endMs protects its entire operation', () => {
  const operation = event(0);
  operation.timing = timing(0, null, 7200000, 'exact');
  const axis = compact({ events: [operation], turns: [turn(0, 7200000)] });
  assert.equal(axis.durationMs, 7200000);
  assert.equal(axis.foldedUnrecordedMs, 0);
});

test('recorded operations from parallel Agents protect the shared axis without adding durations', () => {
  const axis = compact({ events: [event(0, 1000), event(3601000, 3602000), event(1000, 3601000, 'child')],
    turns: [turn(0, 3602000), turn(1000, 3601000, 'child')] });
  assert.equal(axis.durationMs, 3602000);
  assert.equal(axis.runtimeDurationMs, 3602000);
  assert.equal(axis.foldedUnrecordedMs, 0);
});

test('start-only points within a fallback turn remain distinct and invertible anchors', () => {
  const axis = compact({ events: [event(0), event(3600000), event(7200000)], turns: [] });
  assert.equal(axis.mode, 'observed');
  assert.equal(axis.durationMs, 2000);
  assert.equal(axis.runtimeDurationMs, 7200000);
  assert.equal(axis.foldedUnrecordedMs, 7198000);
  assert.deepEqual([0, 3600000, 7200000].map(wall => axis.toActive(wall)), [0, 1000, 2000]);
  assert.deepEqual([0, 1000, 2000].map(position => axis.toWall(position)), [0, 3600000, 7200000]);
});

test('turn boundaries remain anchors even when no event is loaded for that turn', () => {
  const axis = compact({ events: [], turns: [turn(0, 10000), turn(4000, 6000, 'child')] });
  assert.equal(axis.durationMs, 3000);
  assert.equal(axis.toActive(4000), 1000);
  assert.equal(axis.toActive(6000), 2000);
  assert.equal(axis.toWall(1000), 4000);
  assert.equal(axis.toWall(2000), 6000);
  assert.deepEqual(axis.turnSpans.get(JSON.stringify(['child', 'one'])), [{ start: 4000, end: 6000 }]);
});

test('open turns stop at their own latest event, and context and null-turn records do not protect gaps', () => {
  const axis = compact({ events: [event(0), event(10000), event(0, 10000, 'main', 'one', 'context'), event(5000, null, 'main', null),
    event(999999, null, 'child', 'other')], turns: [turn(0)] });
  assert.equal(axis.runtimeDurationMs, 10000);
  assert.equal(axis.durationMs, 1000);
  assert.equal(axis.foldedUnrecordedMs, 9000);
  assert.deepEqual(axis.folds, [{ start: 500, end: 9500, offset: 500 }]);
  assert.equal(axis.toWall(axis.durationMs), 10000);
});

test('compact seams select opposite wall boundaries and project arbitrary ranges into retained pieces', () => {
  const axis = compact({ events: [event(0, 1000), event(3601000, 3602000)], turns: [turn(0, 3602000)] });
  assert.equal(axis.toActive(1500), 1500);
  assert.equal(axis.toActive(2000000), 1500);
  assert.equal(axis.toActive(3600500), 1500);
  assert.equal(axis.toWall(1500, 'start'), 3600500);
  assert.equal(axis.toWall(1500, 'end'), 1500);
  assert.deepEqual(axis.projectRange(1000, 3601000), [
    { start: 1000, end: 1500, wallStart: 1000, wallEnd: 1500 },
    { start: 1500, end: 2000, wallStart: 3600500, wallEnd: 3601000 },
  ]);
  assert.deepEqual(axis.projectRange(2000, 3000000), []);
  assert.deepEqual(axis.projectRange(2000, 2000), [{ start: 1500, end: 1500, wallStart: 2000, wallEnd: 2000 }]);
});

test('visible elapsed time includes folds only when the viewport crosses them and still excludes original turn gaps', () => {
  const data = { events: [], turns: [turn(0, 10000), turn(100000, 110000, 'main', 'two')] };
  const axis = compact(data);
  assert.equal(axis.runtimeDurationMs, 20000);
  assert.equal(axis.durationMs, 2000);
  assert.equal(axis.excludedMs, 90000);
  assert.equal(axis.foldedUnrecordedMs, 18000);
  assert.equal(axis.elapsedBetween(0, 2000), 20000);
  assert.equal(axis.elapsedBetween(0, 1000), 10000);
  assert.equal(axis.elapsedBetween(1000, 2000), 10000);
  assert.equal(axis.elapsedBetween(0, 500), 500);
  assert.equal(axis.elapsedBetween(500, 1000), 500);
  assert.equal(axis.elapsedBetween(500, 500), 0);
  assert.equal(axis.elapsedBetween(500, 1500), 1000);
  assert.equal(axis.elapsedBetween(499, 1501), 19002);
  assert.equal(axis.elapsedBetween(-Infinity, Infinity), 20000);
  for (const [min, max] of [[20, 10], [NaN, 10], [0, NaN], [3000, 4000], [-1000, -1]]) assert.equal(axis.elapsedBetween(min, max), 0);
  const runtime = buildActiveTimeline(data);
  assert.equal(runtime.elapsedBetween(10, 19990), 19980);
});

test('empty and point-only positive-width fallbacks keep their original downgrade semantics', () => {
  for (const snapshot of [null, { events: [], turns: [] }, { events: [event(123)], turns: [] },
    { events: [event(123), event(10000, null, 'main', 'two')], turns: [] }]) {
    const runtime = buildActiveTimeline(snapshot), axis = compact(snapshot);
    assert.equal(axis.durationMs, 0);
    assert.equal(axis.runtimeDurationMs, 0);
    assert.equal(axis.foldedUnrecordedMs, 0);
    assert.equal(axis.mode, runtime.mode);
    assert.deepEqual(axis.segments, []);
    assert.deepEqual(axis.folds, []);
    assert.equal(axis.elapsedBetween(0, 1), 0);
  }
});

test('near-safe-integer wall timestamps subtract before accumulating compact offsets', () => {
  const epoch = Number.MAX_SAFE_INTEGER - 100000;
  const axis = compact({ events: [], turns: [turn(epoch, epoch + 100000)] });
  assert.equal(axis.durationMs, 1000);
  assert.equal(axis.runtimeDurationMs, 100000);
  assert.equal(axis.foldedUnrecordedMs, 99000);
  assert.equal(axis.toWall(750), epoch + 99750);
  assert.equal(axis.elapsedBetween(0, 1000), 100000);
});

test('compact projection never mutates event times, original turn episodes, or snapshot ordering', () => {
  const data = { events: [event(10000), event(0)], turns: [turn(0, 10000)] }, before = structuredClone(data);
  const axis = compact(data);
  axis.toActive(5000); axis.toWall(500); axis.projectRange(0, 10000); axis.elapsedBetween(0, 1000);
  assert.deepEqual(data, before);
});

test('dense point streams produce linear-size pieces with bounded narrow-window lookup', () => {
  const count = 20000, last = (count - 1) * 2000;
  const axis = compact({ events: Array.from({ length: count }, (_, index) => event(index * 2000)), turns: [] });
  assert.equal(axis.durationMs, (count - 1) * 1000);
  assert.equal(axis.foldedUnrecordedMs, (count - 1) * 1000);
  assert.equal(axis.segments.length, count);
  assert.equal(axis.folds.length, count - 1);
  assert.equal(axis.elapsedBetween(axis.durationMs - 10, axis.durationMs), 10);
  assert.deepEqual(axis.projectRange(last - 10, last), [{ start: axis.durationMs - 10, end: axis.durationMs, wallStart: last - 10, wallEnd: last }]);
});
