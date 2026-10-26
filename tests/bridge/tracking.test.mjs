import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { TrackingGate } from '../../frontend/src/state/tracking.ts';
import { ChangeReader, mergeChanges } from '../../frontend/src/state/changes.ts';
import { GroupReader } from '../../frontend/src/state/groups.ts';
import { defaultView, mergeSnapshot } from '../../frontend/src/state/session.ts';
import { buildActiveTimeline } from '../../frontend/src/components/active-timeline.ts';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }
async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve(); }
function delta(state, extra = {}) {
  return { ...state, baseRevision: state.revision, resetRequired: false, resetReason: null,
    continuationCursor: null, upsertEvents: [], removeEventIds: [], ...extra };
}
function summary(state) {
  return { turns: state.turns.length, records: state.coverage.loadedEventCount,
    status: state.status.value, groups: state.groups, timeline: buildActiveTimeline(state).segments };
}

test('pause invalidates snapshot requests even after a rapid resume, while new requests get a fresh signal', async () => {
  const tracking = new TrackingGate(), selection = new AbortController(), pending = deferred();
  let state = structuredClone(fixture);
  const before = state, signal = AbortSignal.any([selection.signal, tracking.signal]);
  const read = pending.promise.then(result => { if (!signal.aborted) state = result; });
  tracking.setPaused(true); assert.equal(signal.aborted, true);
  tracking.setPaused(false); assert.equal(tracking.signal.aborted, false);
  tracking.setPaused(false); const currentSignal = tracking.signal;
  pending.resolve({ ...state, revision: state.revision + 1, turns: [] });
  await read; assert.strictEqual(state, before);
  assert.strictEqual(tracking.signal, currentSignal);
  tracking.setPaused(true); assert.equal(currentSignal.aborted, true);
});

test('paused polling freezes waterfall, counts and status, rejects late data, and resumes from the last committed revision', async () => {
  const tracking = new TrackingGate(), pending = deferred(), timers = new Map(), calls = [];
  let state = structuredClone(fixture), clock = 0, timerId = 0;
  const before = state, frozen = summary(state);
  const extraEvent = { ...state.events[0], eventId: 'after-pause', turnId: 'new-turn',
    order: { ...state.events[0].order, ordinal: 100 },
    timing: { ...state.events[0].timing, startMs: state.events[0].timing.startMs + 20000,
      endMs: state.events[0].timing.startMs + 21000, durationMs: 1000, precision: 'exact' } };
  const latest = delta(state, { revision: state.revision + 1, upsertEvents: [extraEvent],
    turns: [...state.turns, { ...state.turns[0], turnId: 'new-turn', order: 2 }],
    coverage: { ...state.coverage, loadedEventCount: state.coverage.loadedEventCount + 1 },
    status: { ...state.status, value: 'completed' } });
  const reader = new ChangeReader({
    capture: () => {
      if (tracking.paused) return null;
      // 暂停不改变任务身份；在途请求由 ChangeReader 自己的代次拒收。
      return { snapshot: state, isCurrent: () => true };
    },
    read: (request, signal) => {
      calls.push({ request, signal });
      return calls.length === 1 ? pending.promise : Promise.resolve(calls.length === 2 ? latest : delta(state));
    },
    commit: batch => { state = mergeChanges(state, batch); },
    reset: () => assert.fail('unexpected rebuild'), failed: error => assert.fail(error),
    now: () => clock, setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, at: clock + delay }); return id; },
    clearTimer: id => timers.delete(id),
  });
  const advance = async (ms = 0) => {
    clock += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= clock) { timers.delete(id); timer.callback(); }
    await flush();
  };
  reader.start(); await advance();
  tracking.setPaused(true); reader.pause();
  assert.equal(calls[0].signal.aborted, true);
  pending.resolve(latest); await flush(); await advance(10000);
  assert.strictEqual(state, before); assert.deepEqual(summary(state), frozen); assert.equal(calls.length, 1);
  // 页面再次可见仍保持显式暂停。
  reader.setVisible(false); reader.setVisible(true); await advance();
  assert.equal(calls.length, 1); assert.deepEqual(summary(state), frozen);
  tracking.setPaused(false); reader.start(); await advance();
  assert.equal(calls.length, 2); assert.equal(calls[1].request.afterRevision, before.revision);
  assert.equal(state.turns.length, before.turns.length + 1);
  assert.equal(state.coverage.loadedEventCount, before.coverage.loadedEventCount + 1);
  assert.equal(state.status.value, 'completed'); assert.notDeepEqual(summary(state).timeline, frozen.timeline);
  // 完成状态不停止观察，下一轮仍自动读取。
  await advance(750); assert.equal(calls.length, 3); reader.dispose();
});

test('resuming a paused rebuild completes it before reading changes that may omit removals', async () => {
  const tracking = new TrackingGate(), pending = deferred(), timers = new Map();
  let state = structuredClone(fixture), reads = 0, rebuilds = 0, timerId = 0;
  const before = state;
  const reader = new ChangeReader({
    capture: () => tracking.paused ? null : { snapshot: state, isCurrent: () => true },
    read: async () => { reads++; return delta(state, { resetRequired: true, resetReason: 'expiredCursor' }); },
    reset: async (batch, signal) => {
      if (++rebuilds === 1) await pending.promise;
      if (!signal.aborted) state = { ...state, events: [] };
    },
    commit: () => assert.fail('ordinary changes cannot replace the pending rebuild'), failed: error => assert.fail(error),
    setTimer: callback => { const id = ++timerId; timers.set(id, callback); return id; }, clearTimer: id => timers.delete(id),
  });
  const tick = async () => { const callbacks = [...timers.values()]; timers.clear(); for (const callback of callbacks) callback(); await flush(); };
  reader.start(); await tick(); assert.equal(rebuilds, 1);
  tracking.setPaused(true); reader.pause(); pending.resolve(); await flush();
  assert.strictEqual(state, before); assert.equal(timers.size, 0);
  tracking.setPaused(false); reader.start(); await tick();
  assert.equal(reads, 1); assert.equal(rebuilds, 2); assert.deepEqual(state.events, []); reader.dispose();
});

test('an in-flight child read cannot update frozen counts or restart the queue until tracking resumes', async () => {
  const tracking = new TrackingGate(), first = deferred(), calls = [];
  let state = structuredClone(fixture);
  const child = { ...state.groups[0], groupId: 'unloaded-child', threadId: 'child',
    parentGroupId: state.rootGroupId, loadedEventCount: 0, loadedToolCallCount: 0 };
  state = { ...state, groups: [...state.groups, child] };
  const before = state, frozen = summary(state), view = { ...defaultView(), follow: 'paused' };
  const reply = { ...state, events: [{ ...state.events[0], eventId: 'child-event', groupId: child.groupId, threadId: child.threadId }],
    groups: state.groups.map(group => group === child ? { ...group, loadedEventCount: 1 } : group),
    coverage: { ...state.coverage, scopeRevision: state.coverage.scopeRevision + 1, loadedEventCount: state.coverage.loadedEventCount + 1 } };
  const reader = new GroupReader({
    capture: () => {
      if (tracking.paused) return null;
      const signal = tracking.signal;
      return { snapshot: state, view, isCurrent: () => !signal.aborted };
    },
    read: (request, signal) => { calls.push({ request, signal }); return calls.length === 1 ? first.promise : Promise.resolve(reply); },
    commit: result => { state = mergeSnapshot(state, result); },
    failed: error => assert.fail(error), rebuild: () => assert.fail('unexpected rebuild'),
  });
  reader.update(); await flush();
  tracking.setPaused(true); reader.setVisible(false);
  assert.equal(calls[0].signal.aborted, true);
  first.resolve(reply); await flush(); reader.update(); await flush();
  assert.strictEqual(state, before); assert.deepEqual(summary(state), frozen); assert.equal(calls.length, 1);
  tracking.setPaused(false); reader.setVisible(true); await flush();
  assert.equal(calls.length, 2); assert.ok(state.events.some(event => event.eventId === 'child-event'));
  assert.equal(state.groups.find(group => group.groupId === child.groupId).loadedEventCount, 1);
  reader.dispose();
});
