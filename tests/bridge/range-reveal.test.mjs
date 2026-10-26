/** 完成指针选区只定位首个可见命中项并展开路径；不筛掉记录或选择详情。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { rangeReveal } from '../../frontend/src/components/expansion.ts';
import { makeRows, visibleEvents } from '../../frontend/src/components/model.ts';
import { defaultView } from '../../frontend/src/state/session.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const root = base.rootGroupId, thread = base.binding.threadId;
function event(eventId, startMs, options = {}) {
  return { ...structuredClone(base.events[0]), eventId, threadId: thread, groupId: root, turnId: 'same',
    kind: 'toolCall', toolName: 'syntheticTool', order: { section: 'timeline', ordinal: 0, basis: 'officialOrder' },
    timing: { ...base.events[0].timing, startMs, endMs: startMs == null ? null : startMs + 5,
      durationMs: startMs == null ? null : 5, precision: startMs == null ? 'orderOnly' : 'exact' }, ...options };
}
function group(groupId, parentGroupId, creationEventId) {
  return { ...base.groups[0], groupId, threadId: groupId, kind: 'subtask', parentGroupId, creationEventId, name: groupId };
}
function snapshot(events, groups = [base.groups.find(item => item.groupId === root)]) {
  return { ...structuredClone(base), events, groups, coverage: { ...base.coverage, hasEarlier: false } };
}
function view(options = {}) {
  return { ...defaultView(), selectedEventId: 'existing-detail', detailTab: 'source', timelineSpacing: 'compact',
    turnRange: { startMs: 1, endMs: 2 }, anchor: { eventId: 'existing-anchor', groupId: root, offsetPx: -5 },
    expandedTurnIds: ['unrelated-turn'], expandedGroupIds: ['unrelated-group'], ...options };
}

test('a distant range creates a virtual-list anchor and opens only the target turn without selecting details', () => {
  const data = snapshot(Array.from({ length: 10000 }, (_, index) => event(`record-${index}`, index * 10)));
  const before = view(), visible = visibleEvents(data, before, null), range = { startMs: 90000, endMs: 90005 };
  const patch = rangeReveal(data, before, visible, range), after = { ...before, ...patch };
  assert.deepEqual(patch.anchor, { eventId: 'record-9000', groupId: root, offsetPx: 0 });
  assert.deepEqual(patch.expandedTurnIds, ['unrelated-turn', 'same']);
  assert.deepEqual(patch.expandedGroupIds, ['unrelated-group']);
  assert.equal(patch.follow, 'paused');
  assert.deepEqual(Object.keys(patch).sort(), ['anchor', 'expandedGroupIds', 'expandedTurnIds', 'follow']);
  for (const field of ['selectedEventId', 'detailTab', 'filters', 'turnRange', 'agentScope', 'timelineSpacing', 'overviewMode']) assert.deepEqual(after[field], before[field]);
  assert.deepEqual(visibleEvents(data, after, null), visible);
  const row = makeRows(data, after, visible).find(row => row.type === 'event' && row.event.eventId === patch.anchor.eventId);
  assert.ok(row.top > 250000);
});

test('the first match is chosen from the filtered visible order rather than the full snapshot', () => {
  const data = snapshot([event('filtered-out-first', 100), event('visible-first', 110), event('visible-later', 120)]);
  const before = view({ filters: { ...defaultView().filters, text: 'needle' } });
  const visible = visibleEvents(data, before, new Set(['visible-first', 'visible-later']));
  const patch = rangeReveal(data, before, visible, { startMs: 90, endMs: 130 });
  assert.equal(patch.anchor.eventId, 'visible-first');
  assert.deepEqual(visibleEvents(data, { ...before, ...patch }, new Set(['visible-first', 'visible-later'])), visible);
});

test('untimed records are not jump targets and cannot borrow their turn envelope', () => {
  const data = snapshot([event('untimed', null), event('timed-outside', 1000)]), before = view();
  const visible = visibleEvents(data, before, null);
  assert.equal(rangeReveal(data, before, visible, { startMs: 0, endMs: 100 }), null);
  assert.equal(rangeReveal(data, before, [visible[0]], { startMs: 0, endMs: 2000 }), null);
});

test('no matching visible record leaves existing reading position, follow state, and expansion unchanged', () => {
  const data = snapshot([event('outside', 1000)]), before = view(), copy = structuredClone(before);
  for (const range of [{ startMs: 0, endMs: 100 }, { startMs: 50, endMs: 50 }, { startMs: 100, endMs: 0 }]) {
    assert.equal(rangeReveal(data, before, visibleEvents(data, before, null), range), null);
    assert.deepEqual(before, copy);
  }
  assert.equal(rangeReveal(data, before, [], { startMs: 0, endMs: 2000 }), null);
});

function nested() {
  const data = snapshot([
    event('spawn-child', 0, { turnId: 'root-turn', kind: 'collaboration' }),
    event('spawn-grandchild', 100, { threadId: 'child', groupId: 'child', turnId: 'child-turn', kind: 'collaboration' }),
    event('target', 10000, { threadId: 'grandchild', groupId: 'grandchild', turnId: 'leaf-turn' }),
    event('sibling', 20000, { threadId: 'sibling', groupId: 'sibling', turnId: 'sibling-turn' }),
  ], [base.groups.find(item => item.groupId === root), group('child', root, 'spawn-child'),
    group('grandchild', 'child', 'spawn-grandchild'), group('sibling', root, null)]);
  return data;
}

test('legacy hierarchical scopes open the target nested path and its ancestor creation turns', () => {
  const data = nested(), before = view({ agentScope: { kind: 'group', groupId: 'grandchild' } });
  const visible = visibleEvents(data, before, null), patch = rangeReveal(data, before, visible, { startMs: 10000, endMs: 10005 });
  assert.deepEqual(patch.anchor, { eventId: 'target', groupId: 'grandchild', offsetPx: 0 });
  assert.deepEqual(new Set(patch.expandedTurnIds), new Set(['unrelated-turn', 'root-turn', 'child-turn', 'leaf-turn']));
  assert.deepEqual(new Set(patch.expandedGroupIds), new Set(['unrelated-group', 'child', 'grandchild']));
  assert.ok(!patch.expandedGroupIds.includes('sibling'));
  assert.deepEqual(makeRows(data, { ...before, ...patch }, visible).filter(row => row.type === 'event').map(row => row.event.eventId), ['target']);
});

test('exact Agent selections reveal the target without opening unselected ancestors', () => {
  const data = nested(), before = view({ agentScope: { kind: 'selected', groupIds: ['grandchild'] } });
  const visible = visibleEvents(data, before, null), patch = rangeReveal(data, before, visible, { startMs: 10000, endMs: 10005 });
  assert.deepEqual(patch.expandedTurnIds, ['unrelated-turn', 'leaf-turn']);
  assert.deepEqual(patch.expandedGroupIds, ['unrelated-group', 'grandchild']);
  assert.equal(patch.agentScope, undefined);
  assert.deepEqual(makeRows(data, { ...before, ...patch }, visible).filter(row => row.type === 'event').map(row => row.event.eventId), ['target']);
});

test('a child reusing the parent turnId still receives its own event and group anchor', () => {
  const data = snapshot([event('parent', 10), event('child-target', 1000, { threadId: 'child', groupId: 'child' })],
    [base.groups.find(item => item.groupId === root), group('child', root, null)]);
  const before = view({ agentScope: { kind: 'selected', groupIds: [root, 'child'] } }), visible = visibleEvents(data, before, null);
  const patch = rangeReveal(data, before, visible, { startMs: 1000, endMs: 1005 });
  assert.deepEqual(patch.anchor, { eventId: 'child-target', groupId: 'child', offsetPx: 0 });
  assert.deepEqual(patch.expandedTurnIds, ['unrelated-turn', 'same']);
  assert.deepEqual(patch.expandedGroupIds, ['unrelated-group', 'child']);
  assert.deepEqual(visibleEvents(data, { ...before, ...patch }, null), visible);
});

test('a context item with a recorded time is a valid anchor without inventing a turn to expand', () => {
  const data = snapshot([event('context', 50, { turnId: null })]), before = view();
  const patch = rangeReveal(data, before, visibleEvents(data, before, null), { startMs: 40, endMs: 60 });
  assert.deepEqual(patch.anchor, { eventId: 'context', groupId: root, offsetPx: 0 });
  assert.deepEqual(patch.expandedTurnIds, before.expandedTurnIds);
  assert.deepEqual(patch.expandedGroupIds, before.expandedGroupIds);
});

test('range reveal does not mutate snapshot, visible order, or any existing view field', () => {
  const data = nested(), before = view({ agentScope: { kind: 'selected', groupIds: ['grandchild'] } });
  const visible = visibleEvents(data, before, null), copies = structuredClone({ data, before, visible });
  Object.freeze(visible); Object.freeze(before.expandedTurnIds); Object.freeze(before.expandedGroupIds); Object.freeze(before);
  const patch = rangeReveal(data, before, visible, { startMs: 10000, endMs: 10005 });
  assert.deepEqual(data, copies.data);
  assert.deepEqual(before, copies.before);
  assert.deepEqual(visible, copies.visible);
  assert.notEqual(patch.expandedTurnIds, before.expandedTurnIds);
  assert.notEqual(patch.expandedGroupIds, before.expandedGroupIds);
});
