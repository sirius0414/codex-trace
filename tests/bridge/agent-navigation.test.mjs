import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { activeAgentId, agentPagePatch, interactionTargets } from '../../frontend/src/components/agent-navigation.ts';
import { defaultView } from '../../frontend/src/state/session.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const root = base.groups.find(group => group.groupId === base.rootGroupId);
function group(groupId, parentGroupId = root.groupId) {
  return { ...structuredClone(root), groupId, threadId: `thread-${groupId}`, kind: 'agent', name: groupId,
    parentGroupId, creationEventId: `spawn-${groupId}`, relationshipId: `relation-${groupId}` };
}
const child = group('child'), nested = group('nested', child.groupId), sibling = group('sibling');
function source(itemId, threadId = root.threadId, turnId = 'turn-one') {
  return { kind: 'api', threadId, turnId, itemId, rawType: 'collabAgentToolCall', producerVersion: null, association: 'exactItemId' };
}
function event(eventId, owner = root, options = {}) {
  return { ...structuredClone(base.events[0]), eventId, threadId: owner.threadId, groupId: owner.groupId,
    kind: 'collaboration', turnId: 'turn-one', sources: [source(eventId, owner.threadId)], ...options };
}
function data(events = [], relationships = []) {
  return { ...structuredClone(base), groups: [root, child, nested, sibling], events, relationships };
}
function relation(from, to, creationEventId, options = {}) {
  return { relationshipId: `${from}:${to}`, type: 'communicates-with', fromThreadId: from, toThreadId: to,
    creationEventId, turnId: 'turn-one', evidence: [], resolution: 'confirmed', ...options };
}

test('restored Agent scopes select one available page without selecting descendants', () => {
  const snapshot = data([event('detail', nested)]), view = { ...defaultView(), selectedEventId: 'detail' };
  assert.equal(activeAgentId(snapshot, defaultView()), root.groupId);
  assert.equal(activeAgentId(snapshot, view), nested.groupId);
  assert.equal(activeAgentId(snapshot, { ...view, agentScope: { kind: 'group', groupId: child.groupId } }), child.groupId);
  assert.equal(activeAgentId(snapshot, { ...view, agentScope: { kind: 'selected', groupIds: [sibling.groupId] } }), sibling.groupId);
  assert.equal(activeAgentId(snapshot, { ...view, agentScope: { kind: 'selected', groupIds: [child.groupId, sibling.groupId] } }), nested.groupId);
  assert.equal(activeAgentId(snapshot, { ...view, selectedEventId: null, agentScope: { kind: 'selected', groupIds: ['removed'] } }), root.groupId);
  assert.equal(activeAgentId(snapshot, { ...view, agentScope: { kind: 'main', groupId: null } }), root.groupId);
});

test('page changes reveal loaded target turns and retain tab filters while clearing old detail and anchor', () => {
  const snapshot = data([event('child-one', child), event('nested-one', nested, { turnId: 'nested-turn' })]);
  const view = { ...defaultView(), selectedEventId: 'old', anchor: { eventId: 'old', offsetPx: 12 },
    filters: { ...defaultView().filters, text: 'search' }, expandedTurnIds: ['existing'] };
  const patch = agentPagePatch(snapshot, view, child.groupId);
  assert.deepEqual(patch.agentScope, { kind: 'selected', groupIds: [child.groupId] });
  assert.deepEqual(patch.expandedTurnIds, ['existing', 'turn-one']);
  assert.deepEqual(patch.expandedGroupIds, [child.groupId]);
  assert.equal(patch.selectedEventId, null);
  assert.equal(patch.anchor, null);
  assert.equal(patch.follow, 'paused');
  assert.equal(patch.filters, undefined);
  assert.deepEqual(agentPagePatch(snapshot, view, 'removed'), {});
});

test('explicit event jumps clear hiding filters and anchor the event within its owning page', () => {
  const snapshot = data([event('target', child)]);
  const view = { ...defaultView(), filters: { ...defaultView().filters, text: 'hide target', failedOnly: true } };
  const patch = agentPagePatch(snapshot, view, child.groupId, 'target');
  assert.equal(patch.selectedEventId, 'target');
  assert.deepEqual(patch.filters, defaultView().filters);
  assert.deepEqual(patch.anchor, { eventId: 'target', groupId: child.groupId, offsetPx: 0 });
  assert.equal(agentPagePatch(snapshot, view, sibling.groupId, 'target').selectedEventId, null);
});

test('unloaded creation event jumps retain the pending event and group anchor for targeted history reads', () => {
  const snapshot = data();
  const view = { ...defaultView(), filters: { ...defaultView().filters, text: 'hide creation' },
    anchor: { eventId: 'previous-child-event', groupId: child.groupId, offsetPx: 15 } };
  const patch = agentPagePatch(snapshot, view, root.groupId, child.creationEventId);
  assert.deepEqual(patch.agentScope, { kind: 'selected', groupIds: [root.groupId] });
  assert.equal(patch.selectedEventId, child.creationEventId);
  assert.deepEqual(patch.anchor, { eventId: child.creationEventId, groupId: root.groupId, offsetPx: 0 });
  assert.deepEqual(patch.filters, defaultView().filters);
  assert.equal(patch.follow, 'paused');
});

test('creation and multiple communication endpoints resolve known Agent pages once', () => {
  const spawn = event('spawn-child');
  const send = event('send');
  const snapshot = data([spawn, send], [relation(root.threadId, child.threadId, 'send'),
    relation(root.threadId, sibling.threadId, 'send'), relation(root.threadId, child.threadId, 'send'),
    relation(root.threadId, 'unknown-thread', 'send')]);
  assert.deepEqual(interactionTargets(snapshot, spawn).map(group => group.groupId), [child.groupId]);
  assert.deepEqual(interactionTargets(snapshot, send).map(group => group.groupId), [child.groupId, sibling.groupId]);
});

test('recorded activity references navigate known pages without inferring ownership or linking unavailable threads', () => {
  const activity = event('interacted', child, { relatedThreadIds: [root.threadId, nested.threadId, root.threadId, child.threadId, 'unavailable'] });
  const snapshot = data([activity]);
  assert.deepEqual(interactionTargets(snapshot, activity).map(group => group.groupId), [root.groupId, nested.groupId]);
  assert.deepEqual(snapshot.relationships, []);
});

test('matching evidence links repeated communications and child-to-parent interactions with exact source identities', () => {
  const send = event('send-again', child);
  const snapshot = data([send], [relation(child.threadId, root.threadId, null, { resolution: 'ambiguous',
    evidence: [source('previous', child.threadId), source('send-again', child.threadId)] })]);
  assert.deepEqual(interactionTargets(snapshot, send).map(group => group.groupId), [root.groupId]);
  assert.deepEqual(interactionTargets(snapshot, { ...send, sources: [source('send-again', child.threadId, 'other-turn')] }), []);
  assert.deepEqual(interactionTargets(snapshot, { ...send, sources: [source('send-again', sibling.threadId)] }), []);
});

test('raw provenance includes generation and byte range; conflicts, unrelated endpoints and preview names create no links', () => {
  const raw = { kind: 'raw', threadId: root.threadId, segmentId: 'segment', generation: 1, byteStart: 20, byteEnd: 50,
    rawType: 'event_msg', producerVersion: null, association: 'physicalRecord' };
  const send = event('raw-send', root, { sources: [raw], preview: child.name });
  const matched = relation(root.threadId, child.threadId, null, { evidence: [{ ...raw, association: 'explicitMetadata' }] });
  assert.deepEqual(interactionTargets(data([send], [matched]), send).map(group => group.groupId), [child.groupId]);
  for (const changed of [{ generation: 2 }, { byteStart: 21 }, { byteEnd: 51 }]) {
    assert.deepEqual(interactionTargets(data([send], [{ ...matched, evidence: [{ ...raw, ...changed }] }]), send), []);
  }
  assert.deepEqual(interactionTargets(data([send], [{ ...matched, resolution: 'conflict' }]), send), []);
  assert.deepEqual(interactionTargets(data([send], [relation(child.threadId, sibling.threadId, send.eventId)]), send), []);
  assert.deepEqual(interactionTargets(data([send]), send), []);
  assert.deepEqual(interactionTargets(data([send], [matched]), { ...send, kind: 'assistantMessage' }), []);
});
