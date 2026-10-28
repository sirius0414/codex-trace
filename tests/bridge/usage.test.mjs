import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { usagePresentation } from '../../frontend/src/components/usage.ts';
import { makeRows, scopedEvents, scopedModelRequests, visibleEvents } from '../../frontend/src/components/model.ts';

const metric = value => value == null ? { availability: 'notRecorded', value: null } : { availability: 'recorded', value };
const usage = (input, options = {}) => ({ scope: 'turn', epoch: 'session', order: 1, input: metric(input), output: metric(0), cachedInput: metric(0), cacheSemantics: 'includedInInput', coverage: 'Synthetic scope evidence.', ...options });
const fixture = name => JSON.parse(readFileSync(new URL(`../fixtures/contracts/${name}.json`, import.meta.url)));

test('scoped usage distinguishes missing, zero, cache and repeated snapshots in both languages', () => {
  for (const language of ['zh', 'en']) {
    const empty = usagePresentation([], language, true);
    assert.equal(empty.available, false);
    assert.match(empty.text, /未记录|not recorded/);
    assert.equal(usagePresentation(undefined, language).text, '—');
    const value = usage(1000, { cachedInput: metric(400) });
    assert.deepEqual(usagePresentation([value, value], language, true), usagePresentation([value], language, true));
    const display = usagePresentation([value], language, true);
    assert.match(display.text, /0/);
    assert.match(display.title, /1,000/);
    assert.match(display.title, /400/);
    assert.match(display.title, /缓存包含|included in input/);
    assert.ok(!display.title.includes('1,400'));
    const missing = usagePresentation([usage(null, { output: metric(null), cachedInput: metric(null) })], language, true);
    assert.equal(missing.available, false);
    assert.match(missing.text, /—/);
  }
});

test('latest scope replaces a decreasing count and separate sessions never sum', () => {
  const older = usage(100), later = usage(20, { order: 2, epoch: 'another' });
  const display = usagePresentation([older, later], 'zh', true);
  assert.match(display.text, /入 20/);
  assert.match(display.text, /\*/);
  assert.match(display.title, /彼此未求和/);
  assert.equal(usagePresentation([usage(999, { scope: 'thread' })], 'zh', true).available, false);
  assert.equal(usagePresentation([usage(999)], 'en').text, '—');
  assert.equal(usagePresentation([usage(42, { scope: 'request' })], 'en').text, '42 / 0');
});

test('turn headers retain full scoped usage across filtering, folding and reused child turn IDs', () => {
  const snapshot = fixture('snapshot-running'), view = fixture('view-state');
  const main = snapshot.groups.find(group => group.groupId === snapshot.rootGroupId);
  const child = snapshot.groups.find(group => group.groupId !== snapshot.rootGroupId);
  const turnId = 'shared-turn';
  snapshot.turns = [{ threadId: main.threadId, turnId, order: 0, usage: [usage(500)] }, { threadId: child.threadId, turnId, order: 0, usage: [usage(80)] }];
  snapshot.events = [
    { ...snapshot.events[0], eventId: 'one', threadId: main.threadId, groupId: main.groupId, turnId },
    { ...snapshot.events[0], eventId: 'two', threadId: main.threadId, groupId: main.groupId, turnId },
    { ...snapshot.events[0], eventId: 'child', threadId: child.threadId, groupId: child.groupId, turnId },
  ];
  child.creationEventId = null;
  for (const selected of [false, true]) for (const expanded of [false, true]) {
    const scoped = { ...view, filters: { ...view.filters, text: 'filtered' }, expandedTurnIds: expanded ? [turnId] : [], expandedGroupIds: [child.groupId],
      agentScope: selected ? { kind: 'selected', groupIds: [main.groupId, child.groupId] } : { kind: 'all', groupId: null } };
    const rows = makeRows(snapshot, scoped, snapshot.events.filter(event => event.eventId !== 'two'));
    const headers = rows.filter(row => row.type === 'turn');
    assert.deepEqual(headers.map(row => row.usage[0].input.value), [500, 80]);
    assert.ok(headers.every(row => row.count === 1));
    assert.equal(rows.filter(row => row.type === 'event').length, expanded ? 2 : 0);
  }
});

test('usage evidence stays out of records while retaining turn tokens and Agent request counts', () => {
  const snapshot = fixture('snapshot-running'), baseView = fixture('view-state');
  const main = snapshot.groups.find(group => group.groupId === snapshot.rootGroupId);
  const child = snapshot.groups.find(group => group.groupId !== snapshot.rootGroupId);
  const turnId = 'usage-turn', turnUsage = usage(500);
  const record = { ...snapshot.events[0], eventId: 'message', kind: 'assistantMessage', threadId: main.threadId,
    groupId: main.groupId, turnId, modelRequestKey: 'main-request' };
  const evidence = { ...record, eventId: 'usage', kind: 'modelUsage', usage: [usage(100, { scope: 'request' })] };
  snapshot.events = [record, evidence,
    { ...evidence, eventId: 'second-usage', modelRequestKey: 'second-request' },
    { ...evidence, eventId: 'child-usage', groupId: child.groupId, threadId: child.threadId, modelRequestKey: 'child-request' }];
  snapshot.turns = [{ threadId: main.threadId, turnId, order: 0, usage: [turnUsage] }];
  const before = structuredClone(snapshot);
  const view = { ...baseView, agentScope: { kind: 'selected', groupIds: [main.groupId] }, expandedTurnIds: [turnId],
    filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null } };
  const visible = visibleEvents(snapshot, view, null);
  assert.deepEqual(visible.map(event => event.eventId), ['message']);
  assert.deepEqual(scopedEvents(snapshot, view), visible);
  assert.deepEqual(visibleEvents(snapshot, view, new Set(['usage'])), []);
  const rows = makeRows(snapshot, view, visible);
  assert.deepEqual(rows.filter(row => row.type === 'event').map(row => row.event.eventId), ['message']);
  assert.equal(rows.find(row => row.type === 'turn').count, 1);
  assert.deepEqual(rows.find(row => row.type === 'turn').usage, [turnUsage]);
  assert.deepEqual([...scopedModelRequests(snapshot, view)], ['main-request', 'second-request']);
  assert.deepEqual([...scopedModelRequests(snapshot, { ...view, agentScope: { kind: 'all', groupId: null } })],
    ['main-request', 'second-request', 'child-request']);
  assert.deepEqual([...scopedModelRequests(snapshot, { ...view, agentScope: { kind: 'selected', groupIds: [] } })], []);
  assert.deepEqual(snapshot, before);
});
