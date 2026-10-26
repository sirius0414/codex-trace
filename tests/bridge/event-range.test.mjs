/** 范围高亮只依据条目本身的墙钟时间，列表可见性和轮次折叠结构保持独立。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { eventRangeIds, makeRows, visibleEvents } from '../../frontend/src/components/model.ts';
import { defaultView } from '../../frontend/src/state/session.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const root = base.rootGroupId, thread = base.binding.threadId;
function timing(startMs, endMs = null, durationMs = endMs == null ? null : endMs - startMs, precision = endMs == null ? 'startOnly' : 'exact') {
  return { ...base.events[0].timing, startMs, endMs, durationMs, precision };
}
function event(eventId, startMs, endMs = null, options = {}) {
  return { ...structuredClone(base.events[0]), eventId, threadId: thread, groupId: root, turnId: 'same',
    kind: 'toolCall', toolName: 'syntheticTool', timing: timing(startMs, endMs),
    order: { section: 'timeline', ordinal: 0, basis: 'officialOrder' }, ...options };
}
function snapshot(events) { return { ...structuredClone(base), events, groups: base.groups.filter(group => group.groupId === root),
  coverage: { ...base.coverage, hasEarlier: false } }; }
function view(range, options = {}) { return { ...defaultView(), turnRange: range, expandedTurnIds: ['same'], expandedGroupIds: [], ...options }; }
const ids = events => events.map(item => item.eventId);
const highlighted = (events, matches) => ids(events.filter(item => matches?.has(item.eventId)));

test('a range highlights only the intersecting items within one turn, while every list record remains', () => {
  const data = snapshot([event('first', 0, 10), event('middle', 40, 50), event('last', 80, 90), event('untimed', null)]);
  const range = { startMs: 35, endMs: 60 }, visible = visibleEvents(data, view(range), null);
  assert.deepEqual([...eventRangeIds(data, range)], ['middle']);
  assert.deepEqual(ids(visible), ['first', 'middle', 'last', 'untimed']);
  assert.deepEqual(highlighted(visible, eventRangeIds(data, range)), ['middle']);
});

test('recorded ranges overlapping the selection match even when their starts are outside it', () => {
  const data = snapshot([
    event('long', 0, 100),
    event('exact-duration', 30, null, { timing: timing(30, null, 50, 'exact') }),
    event('unconfirmed-duration', 30, null, { timing: timing(30, null, 50, 'startOnly') }),
    event('duration-only', null, null, { timing: timing(null, null, 50, 'durationOnly') }),
    event('touches-left', 0, 40), event('touches-right', 50, 70),
  ]);
  assert.deepEqual([...eventRangeIds(data, { startMs: 40, endMs: 50 })], ['long', 'exact-duration']);
});

test('points include the left boundary, exclude internal right boundaries, and include the final recorded endpoint', () => {
  const data = snapshot([event('left', 100), event('middle', 200), event('last', 300), event('span', 100, 300)]);
  assert.deepEqual([...eventRangeIds(data, { startMs: 100, endMs: 200 })], ['left', 'span']);
  assert.deepEqual([...eventRangeIds(data, { startMs: 200, endMs: 300 })], ['middle', 'last', 'span']);
  assert.deepEqual([...eventRangeIds(data, { startMs: 300, endMs: 301 })], ['last']);
  const later = { ...data, events: [...data.events, event('later-span', 350, 400)] };
  assert.deepEqual([...eventRangeIds(later, { startMs: 200, endMs: 300 })], ['middle', 'span']);
});

test('null, missing, invalid, and empty ranges never access timing evidence or remove list records', () => {
  const data = snapshot([event('one', 10)]);
  assert.equal(eventRangeIds(data, undefined), null);
  assert.equal(eventRangeIds(data, null), null);
  const unreadable = { get events() { throw new Error('an absent or invalid range must not inspect the snapshot'); } };
  assert.equal(eventRangeIds(unreadable, undefined), null);
  for (const range of [{ startMs: 10, endMs: 10 }, { startMs: 20, endMs: 10 }, { startMs: -1, endMs: 20 },
    { startMs: NaN, endMs: 20 }, { startMs: 0, endMs: Infinity }]) {
    assert.deepEqual([...eventRangeIds(unreadable, range)], []);
    assert.deepEqual(ids(visibleEvents(data, view(range), null)), ['one']);
  }
});

test('untimed items stay visible without borrowing any same-turn execution boundary', () => {
  const data = snapshot([
    event('no-start', null), event('duration-only', null, null, { timing: timing(null, null, 1000, 'durationOnly') }),
    event('point', 50), event('invalid-start', NaN),
  ]);
  Object.defineProperty(data, 'turns', { get: () => { throw new Error('item highlights must not read turn envelopes'); } });
  assert.deepEqual([...eventRangeIds(data, { startMs: 0, endMs: 100 })], ['point']);
  assert.deepEqual(ids(visibleEvents(data, view({ startMs: 0, endMs: 100 }), null)), ['no-start', 'duration-only', 'point', 'invalid-start']);
});

test('context and null-turn records use their own recorded times without inventing an untimed location', () => {
  const data = snapshot([
    event('context', 50, null, { order: { section: 'context', ordinal: 0, basis: 'officialOrder' } }),
    event('null-turn', 55, null, { turnId: null }), event('untimed-context', null, null, { turnId: null }),
    event('outside-context', 100, null, { turnId: null }),
  ]);
  assert.deepEqual([...eventRangeIds(data, { startMs: 40, endMs: 60 })], ['context', 'null-turn']);
});

test('different threads reusing a turnId do not expand a matching item into either entire turn', () => {
  const data = snapshot([
    event('parent-before', 10), event('parent-inside', 100),
    event('child-before', 20, null, { threadId: 'child' }), event('child-inside', 110, null, { threadId: 'child' }),
    event('child-later', 200, null, { threadId: 'child' }),
  ]);
  assert.deepEqual([...eventRangeIds(data, { startMs: 90, endMs: 120 })], ['parent-inside', 'child-inside']);
});

test('pointer movement and clearing change highlights without changing visible IDs or row geometry', () => {
  const data = snapshot([event('context', null, null, { turnId: null }), event('first', 10), event('middle', 50), event('untimed', null), event('last', 100)]);
  const cleared = view(null), original = visibleEvents(data, cleared, null), rows = makeRows(data, cleared, original);
  for (const range of [null, { startMs: 0, endMs: 20 }, { startMs: 40, endMs: 60 }, { startMs: 90, endMs: 110 }, { startMs: 200, endMs: 300 }]) {
    const selected = view(range), visible = visibleEvents(data, selected, null);
    assert.deepEqual(ids(visible), ids(original));
    assert.deepEqual(makeRows(data, selected, visible), rows);
  }
  assert.deepEqual(highlighted(original, eventRangeIds(data, { startMs: 40, endMs: 60 })), ['middle']);
});

test('existing Agent and event filters continue to determine list visibility independently of item highlighting', () => {
  const data = snapshot([
    event('match', 50, null, { status: { ...base.events[0].status, value: 'failed' } }),
    event('outside-range', 150, null, { status: { ...base.events[0].status, value: 'failed' } }),
    event('wrong-kind', 50, null, { kind: 'assistantMessage' }), event('wrong-status', 50),
    event('other-agent', 50, null, { groupId: 'child' }), event('not-search-match', 50),
  ]);
  const selected = view({ startMs: 40, endMs: 60 }, { agentScope: { kind: 'main', groupId: null },
    filters: { text: 'needle', kinds: ['toolCall'], toolName: 'syntheticTool', failedOnly: true, timeRange: null } });
  const matches = new Set(['match', 'outside-range', 'wrong-kind', 'wrong-status', 'other-agent']);
  const visible = visibleEvents(data, selected, matches);
  assert.deepEqual(ids(visible), ['match', 'outside-range']);
  assert.deepEqual(highlighted(visible, eventRangeIds(data, selected.turnRange)), ['match']);
  assert.deepEqual(ids(visibleEvents(data, { ...selected, turnRange: null }, matches)), ['match', 'outside-range']);
  const timed = { ...selected, filters: { ...selected.filters, timeRange: { startMs: 100, endMs: 200 } } };
  assert.deepEqual(ids(visibleEvents(data, timed, matches)), ['outside-range']);
});

test('highlight projection preserves source event times and ordering', () => {
  const data = snapshot([event('later', 100), event('earlier', 0, 50)]), before = structuredClone(data);
  eventRangeIds(data, { startMs: 10, endMs: 20 });
  assert.deepEqual(data, before);
});
