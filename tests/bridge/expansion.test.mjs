import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { bulkExpansion } from '../../frontend/src/components/expansion.ts';
import { makeRows, visibleEvents } from '../../frontend/src/components/model.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const baseView = JSON.parse(readFileSync(new URL('../fixtures/contracts/view-state.json', import.meta.url)));
function fixture() {
  const root = base.rootGroupId;
  const event = (eventId, groupId, turnId, kind = 'toolCall') => ({ ...base.events[0], eventId, groupId, turnId, kind, preview: eventId });
  const group = (groupId, parentGroupId, creationEventId) => ({ ...base.groups[0], groupId, kind: 'subtask', parentGroupId, creationEventId, name: groupId });
  const events = [event('spawn-child', root, 'root-turn', 'collaboration'), event('root-record', root, 'root-turn'),
    event('spawn-grandchild', 'child', 'child-turn', 'collaboration'), event('child-record', 'child', 'child-turn'),
    event('grandchild-record', 'grandchild', 'grandchild-turn'), event('filtered-child-record', 'child', 'filtered-turn')];
  const snapshot = { ...structuredClone(base), events, coverage: { ...base.coverage, hasEarlier: false },
    groups: [base.groups.find(item => item.groupId === root), group('child', root, 'spawn-child'), group('grandchild', 'child', 'spawn-grandchild')],
    turns: ['root-turn', 'child-turn', 'grandchild-turn', 'filtered-turn'].map((turnId, order) => ({ ...base.turns[0], turnId, order })) };
  const view = { ...structuredClone(baseView), filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null },
    agentScope: { kind: 'selected', groupIds: ['child'] }, expandedTurnIds: ['unrelated-turn'], expandedGroupIds: ['unrelated-group'], follow: 'following' };
  return { snapshot, view };
}

test('expand matching turns in an exact Agent selection without opening ancestors or filtered turns', () => {
  const { snapshot, view } = fixture();
  const visible = visibleEvents(snapshot, view, new Set(['child-record']));
  const before = structuredClone(view);
  const patch = bulkExpansion(snapshot, view, visible, true);
  assert.deepEqual(patch.expandedTurnIds, ['unrelated-turn', 'child-turn']);
  assert.deepEqual(patch.expandedGroupIds, ['unrelated-group', 'child']);
  assert.equal(patch.follow, 'paused');
  assert.deepEqual(view, before);
  const rows = makeRows(snapshot, { ...view, ...patch }, visible);
  assert.deepEqual(rows.filter(row => row.type === 'event').map(row => row.event.eventId), ['child-record']);
});

test('legacy hierarchical scope expansion opens every loaded ancestor creation turn', () => {
  const { snapshot, view } = fixture();
  view.agentScope = { kind: 'group', groupId: 'grandchild' };
  const visible = visibleEvents(snapshot, view, null);
  const patch = bulkExpansion(snapshot, view, visible, true);
  assert.deepEqual(new Set(patch.expandedTurnIds), new Set(['unrelated-turn', 'grandchild-turn', 'child-turn', 'root-turn']));
  assert.deepEqual(new Set(patch.expandedGroupIds), new Set(['unrelated-group', 'grandchild', 'child']));
  assert.deepEqual(makeRows(snapshot, { ...view, ...patch }, visible).filter(row => row.type === 'event').map(row => row.event.eventId), ['grandchild-record']);
});

test('collapse preserves other turns, group sections, and selection; incoming turns stay collapsed', () => {
  const { snapshot, view } = fixture();
  view.expandedTurnIds = ['root-turn', 'child-turn', 'grandchild-turn', 'filtered-turn'];
  view.expandedGroupIds = ['child', 'grandchild'];
  view.selectedEventId = 'child-record';
  const visible = visibleEvents(snapshot, view, new Set(['child-record']));
  const patch = bulkExpansion(snapshot, view, visible, false);
  assert.deepEqual(patch.expandedTurnIds, ['root-turn', 'grandchild-turn', 'filtered-turn']);
  assert.deepEqual(patch.expandedGroupIds, ['child', 'grandchild']);
  const collapsed = { ...view, ...patch };
  assert.equal(collapsed.selectedEventId, 'child-record');
  assert.equal(collapsed.follow, 'paused');
  const rows = makeRows(snapshot, collapsed, visible);
  assert.ok(rows.some(row => row.type === 'group' && row.group.groupId === 'child'));
  assert.ok(rows.some(row => row.type === 'turn' && row.id === 'child-turn' && !row.expanded));
  assert.ok(!rows.some(row => row.type === 'event'));

  const incoming = { ...snapshot.events.find(event => event.eventId === 'child-record'), eventId: 'new-child-record', turnId: 'new-child-turn' };
  const nextSnapshot = { ...snapshot, events: [...snapshot.events, incoming] };
  const nextRows = makeRows(nextSnapshot, collapsed, [...visible, incoming]);
  assert.ok(nextRows.some(row => row.type === 'turn' && row.id === incoming.turnId && !row.expanded));
  assert.ok(!nextRows.some(row => row.type === 'event'));
});

test('empty selections leave expansion state untouched and context rows only expand their group', () => {
  const { snapshot, view } = fixture();
  view.agentScope = { kind: 'selected', groupIds: [] };
  const visible = visibleEvents(snapshot, view, null);
  for (const expand of [true, false]) {
    const patch = bulkExpansion(snapshot, view, visible, expand);
    assert.deepEqual(patch.expandedTurnIds, view.expandedTurnIds);
    assert.deepEqual(patch.expandedGroupIds, view.expandedGroupIds);
  }
  view.agentScope = { kind: 'selected', groupIds: ['child'] };
  const context = { ...snapshot.events.find(event => event.eventId === 'child-record'), turnId: null };
  const patch = bulkExpansion(snapshot, view, [context], true);
  assert.deepEqual(patch.expandedTurnIds, view.expandedTurnIds);
  assert.deepEqual(patch.expandedGroupIds, ['unrelated-group', 'child']);
});
