/** 指针范围按原墙钟片段匹配整轮高亮；列表记录与行几何只随原有筛选改变。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildActiveTimeline } from '../../frontend/src/components/active-timeline.ts';
import { makeRows, turnRangeFromActive, turnRangeKeys, visibleEvents } from '../../frontend/src/components/model.ts';
import { defaultView, PreferenceGate, restoredView, searchKeys } from '../../frontend/src/state/session.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const root = base.rootGroupId, thread = base.binding.threadId;
const key = (threadId, turnId) => JSON.stringify([threadId, turnId]);
function timing(startMs, endMs = null) {
  return { ...base.events[0].timing, startMs, endMs, durationMs: endMs == null ? null : endMs - startMs,
    precision: startMs == null ? 'orderOnly' : endMs == null ? 'startOnly' : 'exact' };
}
function turn(turnId, intervals, threadId = thread) {
  return { ...base.turns[0], threadId, turnId, ...(intervals == null ? {} : { executionIntervals: intervals }) };
}
function event(eventId, turnId, startMs, options = {}) {
  return { ...base.events[0], eventId, threadId: thread, groupId: root, turnId, timing: timing(startMs),
    kind: 'toolCall', toolName: 'syntheticTool', order: { section: 'timeline', ordinal: 0, basis: 'officialOrder' }, ...options };
}
function snapshot(events, turns, groups = [base.groups.find(group => group.groupId === root)]) {
  return { ...structuredClone(base), events, turns, groups, coverage: { ...base.coverage, hasEarlier: false } };
}
function view(range, options = {}) {
  return { ...defaultView(), turnRange: range, expandedTurnIds: ['one', 'two', 'same', 'child'], expandedGroupIds: ['child'], ...options };
}
const ids = events => events.map(item => item.eventId);
const highlighted = (events, keys) => ids(events.filter(item => item.turnId != null && keys?.has(key(item.threadId, item.turnId))));

test('a range after a ten-hour gap highlights the whole matching turn and keeps every list record', () => {
  const later = 36000000;
  const data = snapshot([
    event('old', 'one', 10), event('early-in-turn', 'two', later + 1), event('later-in-turn', 'two', later + 99),
    event('untimed', 'two', null), event('context', null, later + 50),
  ], [turn('one', [timing(0, 100)]), turn('two', [timing(later, later + 100)])]);
  const range = { startMs: later + 20, endMs: later + 30 };
  const matches = turnRangeKeys(data, range), visible = visibleEvents(data, view(range), null);
  assert.deepEqual([...matches], [key(thread, 'two')]);
  assert.deepEqual(ids(visible), ['old', 'early-in-turn', 'later-in-turn', 'untimed', 'context']);
  assert.deepEqual(highlighted(visible, matches), ['early-in-turn', 'later-in-turn', 'untimed']);
});

test('an interrupted turn highlights only through its recorded episodes while the list remains intact', () => {
  const data = snapshot([event('before-abort', 'same', 20), event('after-resume', 'same', 1100)], [
    turn('same', [timing(0, 100), timing(1000, 1200)]),
  ]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 200, endMs: 800 })], []);
  const gap = { startMs: 200, endMs: 800 }, resumed = { startMs: 1090, endMs: 1110 };
  assert.deepEqual(ids(visibleEvents(data, view(gap), null)), ['before-abort', 'after-resume']);
  assert.deepEqual(highlighted(visibleEvents(data, view(gap), null), turnRangeKeys(data, gap)), []);
  assert.deepEqual(highlighted(visibleEvents(data, view(resumed), null), turnRangeKeys(data, resumed)), ['before-abort', 'after-resume']);
});

test('half-open overlap distinguishes adjacent turns and internal point boundaries', () => {
  const data = snapshot([], [
    turn('one', [timing(0, 100)]), turn('two', [timing(100, 200)]),
    turn('internal-point', [timing(100, 100)]), turn('terminal-point', [timing(200, 200)]),
  ]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 50, endMs: 100 })], [key(thread, 'one')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 100, endMs: 150 })], [key(thread, 'two'), key(thread, 'internal-point')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 150, endMs: 200 })], [key(thread, 'two'), key(thread, 'terminal-point')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 200, endMs: 201 })], [key(thread, 'terminal-point')]);
});

test('missing, null, invalid, and collapsed ranges never remove list records', () => {
  const data = snapshot([event('one', 'one', 50)], [turn('one', [timing(0, 100)])]);
  assert.equal(turnRangeKeys(data, null), null);
  assert.equal(turnRangeKeys(data, undefined), null);
  assert.deepEqual(ids(visibleEvents(data, defaultView(), null)), ['one']);
  assert.deepEqual(ids(visibleEvents(data, view(null), null)), ['one']);
  for (const range of [{ startMs: 50, endMs: 50 }, { startMs: 100, endMs: 0 }, { startMs: -1, endMs: 50 },
    { startMs: NaN, endMs: 50 }, { startMs: 0, endMs: Infinity }]) {
    assert.deepEqual([...turnRangeKeys(data, range)], []);
    assert.deepEqual(ids(visibleEvents(data, view(range), null)), ['one']);
  }
});

test('open metadata and observed fallbacks use only their own thread-turn evidence', () => {
  const data = snapshot([
    event('open', 'same', 140), event('own-later', 'same', 150),
    event('different-thread', 'same', 1000, { threadId: 'child' }),
    event('different-thread-later', 'same', 1100, { threadId: 'child' }),
    event('different-turn', 'other', 2000),
    event('unplaced', 'untimed', null),
  ], [turn('same', [timing(100)]), turn('untimed')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 145, endMs: 146 })], [key(thread, 'same')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 160, endMs: 170 })], []);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 1050, endMs: 1060 })], [key('child', 'same')]);
  const range = { startMs: 1050, endMs: 1060 }, visible = visibleEvents(data, view(range), null);
  assert.deepEqual(ids(visible), ids(data.events));
  assert.deepEqual(highlighted(visible, turnRangeKeys(data, range)), ['different-thread', 'different-thread-later']);
});

test('JSON tuple keys preserve thread identities that collide under delimiter concatenation', () => {
  const data = snapshot([], [turn('b:c', [timing(0, 10)], 'a'), turn('c', [timing(20, 30)], 'a:b')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 0, endMs: 5 })], [key('a', 'b:c')]);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 25, endMs: 30 })], [key('a:b', 'c')]);
});

test('Agent, text, kind, tool, failure, and event-time filters remain independent of the highlight range', () => {
  const data = snapshot([
    event('match', 'one', 5, { status: { ...base.events[0].status, value: 'failed' } }),
    event('wrong-status', 'one', 5), event('wrong-kind', 'one', 5, { kind: 'assistantMessage' }),
    event('wrong-tool', 'one', 5, { toolName: 'other' }), event('not-search-match', 'one', 5),
    event('untimed', 'one', null), event('other-agent', 'one', 5, { groupId: 'child' }), event('other-turn', 'two', 200),
  ], [turn('one', [timing(0, 100)]), turn('two', [timing(200, 300)])]);
  const selected = view({ startMs: 20, endMs: 30 }, { agentScope: { kind: 'main', groupId: null },
    filters: { text: 'needle', kinds: ['toolCall'], toolName: 'syntheticTool', failedOnly: true, timeRange: { startMs: 0, endMs: 10 } } });
  const matches = new Set(['match', 'wrong-status', 'wrong-kind', 'wrong-tool', 'untimed', 'other-agent', 'other-turn']);
  assert.deepEqual(ids(visibleEvents(data, selected, matches)), ['match']);
  assert.deepEqual(ids(visibleEvents(data, { ...selected, turnRange: { startMs: 200, endMs: 210 } }, matches)), ['match']);
  assert.deepEqual(ids(visibleEvents(data, { ...selected, turnRange: null }, matches)), ['match']);
  assert.deepEqual(data.events.find(item => item.eventId === 'match').timing, timing(5));
});

test('precomputed highlight keys and list visibility never rebuild the execution timeline during dragging', () => {
  const data = snapshot([event('one', 'one', 5), event('untimed', 'one', null)], [turn('one', [timing(0, 100)])]);
  const timeline = buildActiveTimeline(data), range = { startMs: 20, endMs: 30 };
  Object.defineProperty(data, 'turns', { get: () => { throw new Error('timeline must not be rebuilt'); } });
  const matches = turnRangeKeys(data, range, timeline);
  assert.deepEqual(highlighted(visibleEvents(data, view(range), null), matches), ['one', 'untimed']);
  assert.deepEqual(ids(visibleEvents(data, view(range), null)), ['one', 'untimed']);
  const noRangeRead = view(null);
  Object.defineProperty(noRangeRead, 'turnRange', { get: () => { throw new Error('list visibility must not read the highlight range'); } });
  assert.deepEqual(ids(visibleEvents(data, noRangeRead, null)), ['one', 'untimed']);
  assert.equal(turnRangeKeys(data, undefined), null);
});

test('a turn range preserves empty Agent groups, loading entries, and row geometry in exact and legacy scopes', () => {
  const child = { ...base.groups[0], groupId: 'child', threadId: 'child', kind: 'subtask', parentGroupId: root,
    creationEventId: null, loadedEventCount: 0, loadedToolCallCount: 0, hasEarlier: true, nextCursor: 'child-page', readIssue: 'Synthetic incomplete child' };
  const data = snapshot([event('one', 'one', 5)], [turn('one', [timing(0, 100)])], [base.groups.find(group => group.groupId === root), child]);
  for (const agentScope of [{ kind: 'all', groupId: null }, { kind: 'selected', groupIds: [root, 'child'] }]) {
    const selected = view({ startMs: 20, endMs: 30 }, { agentScope });
    const rows = makeRows(data, selected, visibleEvents(data, selected, null));
    const clear = { ...selected, turnRange: null };
    assert.deepEqual(rows, makeRows(data, clear, visibleEvents(data, clear, null)));
    assert.ok(rows.some(row => row.type === 'group' && row.group.groupId === 'child'));
    assert.ok(rows.some(row => row.type === 'earlier' && row.group?.groupId === 'child'));
    assert.ok(rows.some(row => row.type === 'warning' && row.group.groupId === 'child'));
  }
});

test('moving and clearing pointers leave visible IDs, turn headers, and every row offset unchanged', () => {
  const data = snapshot([
    event('context', null, null), event('first', 'one', 10), event('untimed', 'one', null),
    event('second', 'two', 210), event('second-later', 'two', 290),
  ], [turn('one', [timing(0, 100)]), turn('two', [timing(200, 300)])]);
  const cleared = view(null), original = visibleEvents(data, cleared, null), rows = makeRows(data, cleared, original);
  assert.deepEqual(rows.filter(row => row.type === 'turn').map(row => row.turnKey), [null, key(thread, 'one'), key(thread, 'two')]);
  for (const range of [undefined, null, { startMs: 20, endMs: 30 }, { startMs: 220, endMs: 250 }, { startMs: 400, endMs: 500 }]) {
    const changed = view(range), visible = visibleEvents(data, changed, null), next = makeRows(data, changed, visible);
    assert.deepEqual(ids(visible), ids(original));
    assert.deepEqual(next.map(row => ({ key: row.key, top: row.top, height: row.height })), rows.map(row => ({ key: row.key, top: row.top, height: row.height })));
    assert.deepEqual(next, rows);
  }
  const noRangeRead = view(null);
  Object.defineProperty(noRangeRead, 'turnRange', { get: () => { throw new Error('list geometry must not read the highlight range'); } });
  assert.deepEqual(makeRows(data, noRangeRead, visibleEvents(data, noRangeRead, null)), rows);
});

test('Turn row highlight identities distinguish a child that reuses the parent turnId in every scope layout', () => {
  const child = { ...base.groups[0], groupId: 'child', threadId: 'child', kind: 'subtask', parentGroupId: root,
    creationEventId: 'create', loadedEventCount: 2, loadedToolCallCount: 1 };
  const data = snapshot([
    event('root-context', null, null), event('parent', 'same', 10), event('create', 'same', 20, { kind: 'collaboration' }),
    event('child-context', null, null, { threadId: 'child', groupId: 'child' }),
    event('child-same-turn', 'same', 210, { threadId: 'child', groupId: 'child' }),
  ], [turn('same', [timing(0, 100)]), turn('same', [timing(200, 300)], 'child')], [base.groups.find(group => group.groupId === root), child]);
  const range = { startMs: 220, endMs: 230 }, matches = turnRangeKeys(data, range);
  assert.deepEqual([...matches], [key('child', 'same')]);
  for (const agentScope of [{ kind: 'all', groupId: null }, { kind: 'selected', groupIds: [root, 'child'] }]) {
    const selected = view(range, { agentScope }), visible = visibleEvents(data, selected, null), rows = makeRows(data, selected, visible);
    const turns = rows.filter(row => row.type === 'turn' && row.id === 'same');
    assert.deepEqual(turns.map(row => row.turnKey), [key(thread, 'same'), key('child', 'same')]);
    assert.deepEqual(turns.filter(row => matches.has(row.turnKey)).map(row => row.turnKey), [key('child', 'same')]);
    assert.ok(rows.filter(row => row.type === 'turn' && row.id === '__context__').every(row => row.turnKey === null));
    assert.deepEqual(highlighted(visible, matches), ['child-same-turn']);
    assert.deepEqual(ids(visible), ids(data.events));
  }
});

test('absolute selection survives reload and preference persistence without changing text-search cache keys', () => {
  const data = snapshot([event('selected', 'two', 1000)], [turn('two', [timing(900, 1100)])]);
  const selected = view({ startMs: 1000, endMs: 1010 }, { filters: { ...defaultView().filters, text: 'needle' } });
  const before = structuredClone(selected), beforeKeys = searchKeys({ ...selected, turnRange: null }, data);
  assert.deepEqual(searchKeys(selected, data), beforeKeys);
  const reloaded = snapshot([event('earlier', 'one', 10), ...data.events], [turn('one', [timing(0, 20)]), ...data.turns]);
  const restored = restoredView(JSON.parse(JSON.stringify(selected)), 4, reloaded);
  assert.deepEqual(restored.turnRange, { startMs: 1000, endMs: 1010 });
  assert.deepEqual(ids(visibleEvents(reloaded, restored, new Set(['selected']))), ['selected']);
  const preferences = new PreferenceGate(); preferences.restored(4); preferences.edit(restored);
  assert.deepEqual(preferences.take().state.turnRange, selected.turnRange);
  assert.deepEqual(selected, before);
});

test('active selections retain trailing point-only turns beyond the last positive interval', () => {
  const data = snapshot([], [turn('one', [timing(0, 100)]), turn('point', [timing(1000, 1000)]), turn('last-point', [timing(1200, 1200)])]);
  const timeline = buildActiveTimeline(data);
  assert.equal(timeline.durationMs, 100);
  const range = turnRangeFromActive(timeline, 80, 100);
  assert.deepEqual(range, { startMs: 80, endMs: 1201 });
  assert.deepEqual([...turnRangeKeys(data, range, timeline)], [key(thread, 'one'), key(thread, 'point'), key(thread, 'last-point')]);
  assert.deepEqual([...turnRangeKeys(data, turnRangeFromActive(timeline, 0, 50), timeline)], [key(thread, 'one')]);
});

test('leading points are included only when the active selection starts at their projected zero', () => {
  const data = snapshot([], [turn('leading', [timing(0, 0)]), turn('one', [timing(1000, 1100)])]);
  const timeline = buildActiveTimeline(data);
  const range = turnRangeFromActive(timeline, 0, 50);
  assert.deepEqual(range, { startMs: 0, endMs: 1050 });
  assert.deepEqual([...turnRangeKeys(data, range, timeline)], [key(thread, 'leading'), key(thread, 'one')]);
  assert.deepEqual(turnRangeFromActive(timeline, 10, 50), { startMs: 1010, endMs: 1050 });
});

test('internal gap points follow half-open active bounds and do not select the preceding positive turn', () => {
  const data = snapshot([], [
    turn('one', [timing(0, 100)]), turn('boundary-point', [timing(100, 100)]),
    turn('gap-point', [timing(500, 500)]), turn('two', [timing(1000, 1100)]),
  ]);
  const timeline = buildActiveTimeline(data);
  const right = turnRangeFromActive(timeline, 100, 150);
  assert.deepEqual(right, { startMs: 100, endMs: 1050 });
  assert.deepEqual([...turnRangeKeys(data, right, timeline)], [key(thread, 'boundary-point'), key(thread, 'gap-point'), key(thread, 'two')]);
  const left = turnRangeFromActive(timeline, 50, 100);
  assert.deepEqual(left, { startMs: 50, endMs: 100 });
  assert.deepEqual([...turnRangeKeys(data, left, timeline)], [key(thread, 'one')]);
});

test('a point at an abort-resume seam can be selected without matching a neighboring completed turn', () => {
  const data = snapshot([], [
    turn('resumed', [timing(0, 100), timing(1000, 1100)]), turn('one', [timing(0, 100)]), turn('gap-point', [timing(500, 500)]),
  ]);
  const timeline = buildActiveTimeline(data), range = turnRangeFromActive(timeline, 100, 150);
  assert.deepEqual(range, { startMs: 500, endMs: 1050 });
  assert.deepEqual([...turnRangeKeys(data, range, timeline)], [key(thread, 'resumed'), key(thread, 'gap-point')]);
});

test('the inclusive final wall endpoint includes all turn points, not just the positive union end', () => {
  const data = snapshot([], [
    turn('one', [timing(0, 100)]), turn('old-end-point', [timing(100, 100)]), turn('tail', [timing(1000, 1000)]),
  ]);
  const timeline = buildActiveTimeline(data);
  assert.deepEqual([...turnRangeKeys(data, { startMs: 50, endMs: 100 }, timeline)], [key(thread, 'one')]);
  const range = turnRangeFromActive(timeline, 50, 100);
  assert.deepEqual(range, { startMs: 50, endMs: 1001 });
  assert.deepEqual([...turnRangeKeys(data, range, timeline)], [key(thread, 'one'), key(thread, 'old-end-point'), key(thread, 'tail')]);
});

test('point-only axes produce a positive saved range and invalid active selections stay cleared', () => {
  const data = snapshot([], [turn('one', [timing(123, 123)])]), timeline = buildActiveTimeline(data);
  const range = turnRangeFromActive(timeline, 0, 1);
  assert.deepEqual(range, { startMs: 123, endMs: 124 });
  assert.deepEqual([...turnRangeKeys(data, range, timeline)], [key(thread, 'one')]);
  for (const [start, end] of [[0, 0], [10, 5], [-1, 5], [NaN, 5], [0, Infinity]]) assert.equal(turnRangeFromActive(timeline, start, end), null);
  assert.equal(turnRangeFromActive(buildActiveTimeline(null), 0, 1), null);
});

test('point projection is cached across repeated active selection updates', () => {
  const timeline = buildActiveTimeline(snapshot([], [turn('one', [timing(0, 100)]), turn('point', [timing(1000, 1000)])]));
  const spans = timeline.turnSpans;
  let reads = 0;
  Object.defineProperty(timeline, 'turnSpans', { get: () => { reads++; return spans; } });
  assert.deepEqual(turnRangeFromActive(timeline, 0, 100), { startMs: 0, endMs: 1001 });
  assert.deepEqual(turnRangeFromActive(timeline, 50, 100), { startMs: 50, endMs: 1001 });
  assert.deepEqual(turnRangeFromActive(timeline, 0, 50), { startMs: 0, endMs: 50 });
  assert.equal(reads, 1);
});


test('saved trailing point inclusion survives a later turn being appended', () => {
  const data = snapshot([], [turn('one', [timing(0, 100)]), turn('tail', [timing(1000, 1000)])]);
  const range = turnRangeFromActive(buildActiveTimeline(data), 50, 100);
  const later = { ...data, turns: [...data.turns, turn('later', [timing(2000, 2100)])] };
  assert.deepEqual(range, { startMs: 50, endMs: 1001 });
  assert.deepEqual([...turnRangeKeys(later, range)], [key(thread, 'one'), key(thread, 'tail')]);
});
