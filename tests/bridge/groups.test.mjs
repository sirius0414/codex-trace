import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { GroupReader, requestedChildGroups } from '../../frontend/src/state/groups.ts';
import { defaultView, mergeSnapshot, PreferenceGate } from '../../frontend/src/state/session.ts';
import { sameBinding } from '../../frontend/src/state/binding.mjs';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve(); }
function fixture() {
  const root = base.rootGroupId;
  const groups = [base.groups.find(group => group.groupId === root), ...['one', 'two', 'nested'].map((id, index) => ({
    ...base.groups[0], groupId: id, threadId: `thread-${id}`, kind: 'agent',
    parentGroupId: index === 2 ? 'one' : root, loadedEventCount: 0, loadedToolCallCount: 0,
  }))];
  return { ...structuredClone(base), groups, events: base.events.filter(event => event.groupId === root) };
}
function harness(handler) {
  let snapshot = fixture(), view = { ...defaultView(), agentScope: { kind: 'selected', groupIds: ['one', 'two'] },
    follow: 'paused', selectedEventId: 'root-selection', detailTab: 'source', anchor: { eventId: 'root-anchor', groupId: base.rootGroupId, offsetPx: 23 },
    filters: { ...defaultView().filters, text: 'keep the search', failedOnly: true } };
  const calls = [], commits = [], errors = [];
  let active = 0, maxActive = 0, rebuilds = 0;
  const reply = (request, empty = false) => {
    const group = snapshot.groups.find(group => group.groupId === request.groupId);
    const events = empty ? [] : [{ ...base.events[0], eventId: `event-${request.groupId}`, groupId: group.groupId, threadId: group.threadId }];
    return { ...structuredClone(snapshot), events, groups: snapshot.groups.map(value => value.groupId === group.groupId
      ? { ...value, loadedEventCount: events.length, loadedToolCallCount: events.length } : value),
    coverage: { ...snapshot.coverage, scopeRevision: snapshot.coverage.scopeRevision + 1 } };
  };
  const reader = new GroupReader({
    capture: () => {
      const state = snapshot;
      return state ? { snapshot: state, view, isCurrent: () => !!snapshot && sameBinding(state.binding, snapshot.binding) && state.generation === snapshot.generation } : null;
    },
    read: async (request, signal) => {
      calls.push(request); active++; maxActive = Math.max(maxActive, active);
      try { return await handler(request, { reply, signal }); } finally { active--; }
    },
    commit: result => { commits.push(result); snapshot = mergeSnapshot(snapshot, result); },
    failed: failure => errors.push(failure), rebuild: () => { rebuilds++; },
  });
  return { reader, calls, commits, errors, reply, get snapshot() { return snapshot; }, set snapshot(value) { snapshot = value; },
    get view() { return view; }, set view(value) { view = value; }, get maxActive() { return maxActive; }, get rebuilds() { return rebuilds; } };
}

test('all, exact selections and expanded groups request known child identities without hidden ancestors', () => {
  const data = fixture(), view = defaultView();
  assert.deepEqual(requestedChildGroups(data, view), ['one', 'two', 'nested']);
  assert.deepEqual(requestedChildGroups(data, { ...view, agentScope: { kind: 'main', groupId: null } }), []);
  assert.deepEqual(requestedChildGroups(data, { ...view, agentScope: { kind: 'selected', groupIds: ['nested', 'missing'] } }), ['nested']);
  assert.deepEqual(requestedChildGroups(data, { ...view, agentScope: { kind: 'selected', groupIds: [] }, expandedGroupIds: ['two'] }), ['two']);
  assert.deepEqual(requestedChildGroups(data, { ...view, agentScope: { kind: 'group', groupId: 'one' } }), ['one', 'nested']);
});

test('selected child first pages load serially despite blocked preferences, without using a main anchor or changing reading state', async () => {
  const first = deferred();
  const h = harness((request, { reply }) => request.groupId === 'one' ? first.promise : reply(request));
  const preferences = new PreferenceGate(); preferences.blocked = true;
  preferences.edit(h.view);
  assert.equal(preferences.take(), null);
  const before = structuredClone(h.view);
  h.reader.update(); h.reader.update(); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one']);
  assert.deepEqual(h.calls[0], { binding: h.snapshot.binding, groupId: 'one', cursor: null, anchorEventId: null });
  first.resolve(h.reply(h.calls[0])); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one', 'two']);
  assert.equal(h.maxActive, 1);
  assert.equal(h.commits.length, 2);
  assert.deepEqual(h.view, before);
  assert.ok(h.snapshot.events.some(event => event.groupId === 'one'));
  assert.ok(h.snapshot.events.some(event => event.groupId === 'two'));
  h.reader.dispose();
});

test('loaded groups and successful empty pages do not repeat on polling revisions, filters or collapse toggles', async () => {
  const h = harness((request, { reply }) => reply(request, true));
  h.snapshot.groups.find(group => group.groupId === 'two').loadedEventCount = 3;
  h.snapshot.events.push({ ...base.events[0], eventId: 'already-loaded-two', groupId: 'two' });
  h.reader.update(); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one']);
  h.snapshot = { ...h.snapshot, revision: h.snapshot.revision + 1 };
  h.view = { ...h.view, expandedGroupIds: ['one'], filters: { ...h.view.filters, text: 'another query' } };
  h.reader.update(); await flush();
  assert.equal(h.calls.length, 1);
  h.reader.retry(); await flush();
  assert.equal(h.calls.length, 2);
  h.reader.dispose();
});

test('same-generation reset restores missing local child rows even when backend loaded counts stay positive', async () => {
  const h = harness((request, { reply }) => reply(request));
  h.view = { ...h.view, agentScope: { kind: 'selected', groupIds: ['one'] } };
  h.reader.update(); await flush();
  assert.equal(h.snapshot.groups.find(group => group.groupId === 'one').loadedEventCount, 1);
  h.snapshot = { ...h.snapshot, events: h.snapshot.events.filter(event => event.groupId !== 'one') };
  h.reader.update(); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one', 'one']);
  assert.ok(h.snapshot.events.some(event => event.groupId === 'one'));
  h.reader.update(); await flush(); assert.equal(h.calls.length, 2); h.reader.dispose();
});

test('failed groups wait for explicit retry while other queued groups continue', async () => {
  let failing = true;
  const h = harness((request, { reply }) => { if (request.groupId === 'one' && failing) throw new Error('read interrupted'); return reply(request); });
  h.reader.update(); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one', 'two']);
  assert.equal(h.errors.length, 1);
  h.reader.update(); await flush(); assert.equal(h.calls.length, 2);
  failing = false; h.reader.retry(); await flush();
  assert.deepEqual(h.calls.map(call => call.groupId), ['one', 'two', 'one']);
  h.reader.dispose();
});

test('task switches abort old reads and reject late responses before dispatching the new queue', async () => {
  const old = deferred(); let oldSignal;
  const h = harness((request, { reply, signal }) => {
    if (request.binding.threadId === base.binding.threadId) { oldSignal = signal; return old.promise; }
    return reply(request);
  });
  h.reader.update(); await flush();
  const oldReply = h.reply(h.calls[0]);
  h.snapshot = { ...fixture(), binding: { ...h.snapshot.binding, threadId: 'another-root', selectionEpoch: h.snapshot.binding.selectionEpoch + 1 } };
  h.view = { ...h.view, agentScope: { kind: 'selected', groupIds: ['two'] } };
  h.reader.update(); assert.equal(oldSignal.aborted, true);
  old.resolve(oldReply); await flush();
  assert.equal(h.maxActive, 1);
  assert.equal(h.commits.length, 1);
  assert.equal(h.commits[0].binding.threadId, 'another-root');
  assert.deepEqual(h.calls.map(call => call.groupId), ['one', 'two']);
  h.reader.dispose();
});

test('new generations reload empty groups and a mismatched generation requests a root rebuild without partial commit', async () => {
  const h = harness((request, { reply }) => reply(request, true));
  h.view = { ...h.view, agentScope: { kind: 'selected', groupIds: ['one'] } };
  h.reader.update(); await flush();
  h.snapshot = { ...h.snapshot, generation: h.snapshot.generation + 1 };
  h.reader.update(); await flush(); assert.equal(h.calls.length, 2);
  h.reader.dispose();
  const mismatch = harness((request, { reply }) => ({ ...reply(request), generation: base.generation + 1 }));
  mismatch.reader.update(); await flush();
  assert.equal(mismatch.rebuilds, 1); assert.equal(mismatch.commits.length, 0);
  mismatch.reader.update(); await flush(); assert.equal(mismatch.calls.length, 1);
  mismatch.reader.dispose();
});

test('a queued child deselected during another read is skipped and disposal rejects late content', async () => {
  const first = deferred();
  const h = harness(() => first.promise);
  h.reader.update(); await flush();
  h.view = { ...h.view, agentScope: { kind: 'selected', groupIds: ['one'] } };
  first.resolve(h.reply(h.calls[0])); await flush(); assert.equal(h.calls.length, 1);
  h.reader.dispose();
  const late = deferred(), disposed = harness(() => late.promise);
  disposed.reader.update(); await flush(); disposed.reader.dispose();
  late.resolve(disposed.reply(disposed.calls[0])); await flush();
  assert.equal(disposed.commits.length, 0); assert.equal(disposed.calls.length, 1);
});

test('hidden reads abort and their unfinished group is retried on visibility restore', async () => {
  const first = deferred(); let signal;
  const h = harness((request, context) => { signal = context.signal; return h.calls.length === 1 ? first.promise : context.reply(request); });
  h.view = { ...h.view, agentScope: { kind: 'selected', groupIds: ['one'] } };
  h.reader.update(); await flush(); h.reader.setVisible(false); assert.equal(signal.aborted, true);
  first.resolve(h.reply(h.calls[0])); await flush(); assert.equal(h.commits.length, 0);
  h.reader.setVisible(true); await flush();
  assert.equal(h.calls.length, 2); assert.equal(h.commits.length, 1); h.reader.dispose();
});
