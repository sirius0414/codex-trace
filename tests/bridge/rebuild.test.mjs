import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { rebuildLoadedSnapshot } from '../../frontend/src/state/rebuild.ts';
import { defaultView } from '../../frontend/src/state/session.ts';
import { ChangeProtocolError } from '../../frontend/src/state/changes.ts';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
const root = fixture.rootGroupId;
function event(eventId, groupId = root) {
  return { ...structuredClone(fixture.events[0]), eventId, groupId, preview: eventId,
    order: { section: 'turn', ordinal: Number(eventId.replace(/\D/g, '')) || 0 } };
}
function snapshot(events, options = {}) {
  const { cursor = null, revision = 10, groups = [fixture.groups.find(group => group.groupId === root)], ...rest } = options;
  return { ...structuredClone(fixture), events, revision, groups: structuredClone(groups),
    relationships: groups.filter(group => group.groupId !== root).map(group => ({ relationshipId: group.relationshipId,
      type: 'spawned-by', resolution: 'confirmed', fromThreadId: group.threadId, toThreadId: fixture.binding.threadId })),
    coverage: { ...fixture.coverage, loadedEventCount: events.length, scopeRevision: revision, hasEarlier: !!cursor, nextCursor: cursor }, ...rest };
}
function batch(state, extra = {}) {
  const { binding, generation, revision, turns, groups, relationships, status, mainUsage, statistics, coverage, freshness, timelineOrigin } = state;
  return { binding, generation, revision, baseRevision: revision, turns, groups, relationships, status, mainUsage, statistics, coverage, freshness, timelineOrigin,
    resetRequired: false, resetReason: null, continuationCursor: null, upsertEvents: [], removeEventIds: [], ...extra };
}
const unchanged = state => async request => batch(state, { baseRevision: request.afterRevision });
function child(cursor = null) {
  return { ...structuredClone(fixture.groups[0]), groupId: 'child', parentGroupId: root, kind: 'subtask',
    relationshipId: 'spawn-child', threadId: 'thread-child',
    hasEarlier: !!cursor, nextCursor: cursor };
}

test('same-generation rebuild rereads all 501 loaded IDs and discards a deleted ID without mixing the old snapshot', async () => {
  const before = snapshot(Array.from({ length: 501 }, (_, i) => event(`e${i}`)), { revision: 9 });
  const original = structuredClone(before), calls = [];
  const pages = [snapshot(Array.from({ length: 200 }, (_, i) => event(`e${i + 301}`)), { cursor: 'older-1' }),
    snapshot(Array.from({ length: 200 }, (_, i) => event(`e${i + 101}`)), { cursor: 'older-2' }),
    snapshot(Array.from({ length: 100 }, (_, i) => event(`e${i + 1}`)))];
  const result = await rebuildLoadedSnapshot(before, defaultView(), async request => { calls.push(request); return pages[calls.length - 1]; }, unchanged(pages[2]));
  assert.deepEqual(calls.map(request => request.cursor), [null, 'older-1', 'older-2']);
  assert.equal(result.snapshot.events.length, 500);
  assert.ok(!result.snapshot.events.some(row => row.eventId === 'e0'));
  assert.equal(result.confirmedRevision, 10);
  assert.deepEqual(before, original);
});

test('a child reading anchor preserves root history and follows each group cursor independently', async () => {
  const mainGroup = fixture.groups.find(group => group.groupId === root), groups = [mainGroup, child()];
  const before = snapshot([event('root1'), event('root2'), event('child1', 'child'), event('child2', 'child')], { groups });
  const calls = [];
  const result = await rebuildLoadedSnapshot(before, { ...defaultView(), anchor: { eventId: 'child1', groupId: 'child', offsetPx: 3 } }, async request => {
    calls.push(request);
    if (!request.groupId) return snapshot([event(request.cursor ? 'root1' : 'root2')], { groups, cursor: request.cursor ? null : 'root-older' });
    return snapshot([event(request.cursor ? 'child1' : 'child2', 'child')], { groups: [mainGroup, child(request.cursor ? null : 'child-older')] });
  }, unchanged(snapshot([], { groups })));
  assert.deepEqual(calls.map(request => [request.groupId ?? root, request.cursor]), [[root, null], ['child', null], [root, 'root-older'], ['child', 'child-older']]);
  assert.deepEqual(result.snapshot.events.map(row => row.eventId).sort(), ['child1', 'child2', 'root1', 'root2']);
  assert.equal(result.missingAnchor, false);
});

test('pages are caught up from the earliest revision and all continuation pages apply atomically', async () => {
  const before = snapshot([event('old1'), event('old2')]), requests = [];
  let page = 0;
  const later = snapshot([event('old1')], { revision: 12 });
  const result = await rebuildLoadedSnapshot(before, defaultView(), async () => ++page === 1
    ? snapshot([event('old2')], { revision: 10, cursor: 'old' }) : later, async request => {
    requests.push(request);
    return batch(later, { baseRevision: 10, revision: 13, continuationCursor: request.continuationCursor ? null : 'change-next',
      upsertEvents: request.continuationCursor ? [event('new3')] : [], removeEventIds: request.continuationCursor ? [] : ['old1'] });
  });
  assert.deepEqual(requests.map(request => [request.afterRevision, request.continuationCursor]), [[10, null], [10, 'change-next']]);
  assert.deepEqual(result.snapshot.events.map(row => row.eventId).sort(), ['new3', 'old2']);
  assert.equal(result.confirmedRevision, 13);
});

test('a child with a revoked spawn relation is dropped without preventing root history rebuild', async () => {
  const groups = [fixture.groups.find(group => group.groupId === root), child()];
  const before = snapshot([event('root1'), event('child1', 'child')], { groups });
  const rootPage = snapshot([event('root1')], { groups });
  rootPage.relationships[0].resolution = 'ambiguous';
  const calls = [];
  const result = await rebuildLoadedSnapshot(before, { ...defaultView(), anchor: { eventId: 'child1', groupId: 'child', offsetPx: 0 },
    selectedEventId: 'child1' }, async request => { calls.push(request); return rootPage; }, unchanged(rootPage));
  assert.deepEqual(calls, [{ cursor: null, anchorEventId: null }]);
  assert.deepEqual(result.snapshot.events.map(row => row.eventId), ['root1']);
  assert.equal(result.missingAnchor, true); assert.equal(result.missingSelection, true);
});

test('changed generations read a fresh root page and restore anchor and selection without refetching old history', async () => {
  const before = snapshot([event('old1'), event('old2')]), calls = [];
  const generation = before.generation + 1;
  const view = { ...defaultView(), anchor: { eventId: 'anchor', offsetPx: 0 }, selectedEventId: 'selected' };
  const result = await rebuildLoadedSnapshot(before, view, async request => {
    calls.push(request);
    return snapshot([event(request.anchorEventId ?? 'latest')], { generation, revision: 1, cursor: 'unused-history' });
  }, unchanged(snapshot([], { generation, revision: 1 })));
  assert.deepEqual(calls.map(request => request.anchorEventId), [null, 'anchor', 'selected']);
  assert.ok(calls.every(request => request.cursor === null));
  assert.deepEqual(result.snapshot.events.map(row => row.eventId).sort(), ['anchor', 'latest', 'selected']);
  assert.equal(result.missingSelection, false);
});

test('missing anchor and selected records are reported after fresh fallback pages and catch-up', async () => {
  const before = snapshot([event('old1')]), calls = [];
  const view = { ...defaultView(), anchor: { eventId: 'missing-anchor', offsetPx: 0 }, selectedEventId: 'missing-selected' };
  const result = await rebuildLoadedSnapshot(before, view, async request => {
    calls.push(request);
    if (request.anchorEventId) throw { code: 'notFound' };
    return snapshot([event('old1')]);
  }, unchanged(snapshot([])));
  assert.equal(result.missingAnchor, true);
  assert.equal(result.missingSelection, true);
  assert.equal(result.snapshot.events.length, 1);
  assert.equal(calls.filter(request => !request.anchorEventId).length, 2);
});

test('page failure never returns or mutates a partial rebuilt collection', async () => {
  const before = snapshot([event('old1'), event('old2')]), original = structuredClone(before);
  let calls = 0, changeCalls = 0, committed = false;
  await assert.rejects(async () => {
    await rebuildLoadedSnapshot(before, defaultView(), async () => {
      if (++calls === 1) return snapshot([event('old2')], { cursor: 'older' });
      throw new Error('page offline');
    }, async () => { changeCalls++; throw new Error('unexpected'); });
    committed = true;
  }, /page offline/);
  assert.equal(committed, false); assert.equal(changeCalls, 0); assert.deepEqual(before, original);
});

test('catch-up failure and resets reject the complete tentative collection without committing it', async () => {
  const before = snapshot([event('old1')]), original = structuredClone(before);
  for (const mode of ['offline', 'reset', 'repeat', 'binding', 'generation', 'behind']) {
    let committed = false;
    await assert.rejects(async () => {
      await rebuildLoadedSnapshot(before, defaultView(), async () => snapshot([event('old1')], { revision: 12 }), async request => {
        if (mode === 'offline') throw new Error('changes offline');
        return batch(before, { baseRevision: 12, revision: mode === 'behind' ? 11 : 13,
          resetRequired: mode === 'reset', resetReason: mode === 'reset' ? 'expiredCursor' : null,
          binding: mode === 'binding' ? { ...before.binding, selectionEpoch: 999 } : before.binding,
          generation: mode === 'generation' ? before.generation + 1 : before.generation,
          continuationCursor: mode === 'repeat' ? 'repeat' : null });
      });
      committed = true;
    });
    assert.equal(committed, false, mode);
    assert.deepEqual(before, original, mode);
  }
});

test('cross-binding, changing generation and repeated history cursors reject rather than exposing a partial range', async () => {
  const before = snapshot([event('old1'), event('old2')]), original = structuredClone(before);
  for (const mode of ['binding', 'generation', 'repeat']) {
    let calls = 0, committed = false;
    await assert.rejects(async () => {
      await rebuildLoadedSnapshot(before, defaultView(), async () => {
        calls++;
        return snapshot([event('old2')], { cursor: 'repeat',
          ...(calls > 1 && mode === 'binding' ? { binding: { ...before.binding, selectionEpoch: 99 } } : {}),
          ...(calls > 1 && mode === 'generation' ? { generation: before.generation + 1 } : {}) });
      }, unchanged(before));
      committed = true;
    }, ChangeProtocolError);
    assert.equal(committed, false, mode); assert.equal(calls, 2, mode); assert.deepEqual(before, original, mode);
  }
});

test('request and record budgets terminate unbounded fresh histories', async () => {
  const before = snapshot([event('never')]);
  let requests = 0;
  await assert.rejects(() => rebuildLoadedSnapshot(before, defaultView(), async () => {
    requests++;
    return snapshot([], { cursor: `cursor-${requests}` });
  }, unchanged(before)), ChangeProtocolError);
  assert.equal(requests, 1024);
  await assert.rejects(() => rebuildLoadedSnapshot(before, defaultView(), async () => snapshot(Array(100001).fill(event('row'))), unchanged(before)), ChangeProtocolError);
});
