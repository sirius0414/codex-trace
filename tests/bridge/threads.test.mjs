import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeThreadPage } from '../../frontend/src/state/threads.ts';

const thread = (threadId, updatedAt, title = `Task ${threadId}`) => Object.freeze({
  threadId, title, source: 'user', updatedAt, parentThreadId: null,
});

test('initial load collapses four interleaved rollout entries into one selected thread', () => {
  const latest = thread('a', 400, 'Validator'), other = thread('b', 350);
  const incoming = Object.freeze([
    latest, other, thread('a', 300, 'Validator'),
    thread('a', 200, 'Validator'), thread('a', 100, 'Validator'),
  ]);
  const result = mergeThreadPage([], incoming);
  assert.deepEqual(result, [latest, other]);
  assert.equal(result.filter(row => row.threadId === 'a').length, 1);
  assert.equal(incoming.length, 5);
});

test('search and refresh replace the previous list and deduplicate the new response', () => {
  const previous = Object.freeze([thread('a', 900), thread('old-query', 800)]);
  const current = thread('a', 500, 'Search result'), other = thread('c', 450);
  const incoming = Object.freeze([current, other, thread('a', 400, 'Older rollout')]);
  assert.deepEqual(mergeThreadPage(previous, incoming, false), [current, other]);
  assert.deepEqual(previous.map(row => row.threadId), ['a', 'old-query']);
  assert.equal(previous[0].updatedAt, 900);
  assert.equal(incoming.length, 3);
});

test('pagination deduplicates within and across pages without replacing newer metadata with older rollouts', () => {
  const a = thread('a', 500, 'Current A'), b = thread('b', 400);
  const first = Object.freeze(mergeThreadPage([], [a, b, thread('a', 300)]));
  const c = thread('c', 200), newerB = thread('b', 600, 'Updated B');
  const page = Object.freeze([thread('a', 100, 'Old A'), c, thread('c', 150), newerB]);
  const result = mergeThreadPage(first, page, true);
  assert.deepEqual(result, [a, newerB, c]);
  assert.deepEqual(mergeThreadPage(result, page, true), result);
  assert.deepEqual(first, [a, b]);
  assert.equal(page.length, 4);
});

test('deduplication keeps the first position while choosing the latest metadata regardless of row order', () => {
  const old = thread('a', 100, 'Old title'), other = thread('b', 300);
  const latest = thread('a', 500, 'New title'), middle = thread('a', 200);
  assert.deepEqual(mergeThreadPage([], [old, other, latest, middle]), [latest, other]);
  assert.deepEqual(mergeThreadPage([], [latest, other, middle, old]), [latest, other]);
});

test('distinct thread IDs retain separate rows even when their titles are identical', () => {
  const a = thread('a', 100, 'Same title'), b = thread('b', 200, 'Same title');
  assert.deepEqual(mergeThreadPage([], [a, b]), [a, b]);
  assert.deepEqual(mergeThreadPage([a], [b], true), [a, b]);
});

test('known timestamps outrank unknown ones and equal timestamps keep the first metadata', () => {
  const unknown = thread('a', null, 'Unknown'), known = thread('a', 0, 'Known');
  const equal = thread('a', 0, 'Equal timestamp'), anotherUnknown = thread('a', null, 'Another unknown');
  assert.deepEqual(mergeThreadPage([], [unknown, known]), [known]);
  assert.deepEqual(mergeThreadPage([known], [unknown], true), [known]);
  assert.deepEqual(mergeThreadPage([], [known, equal]), [known]);
  assert.deepEqual(mergeThreadPage([known], [equal], true), [known]);
  assert.deepEqual(mergeThreadPage([], [unknown, anotherUnknown]), [unknown]);
});

test('an empty search clears old rows while an empty page preserves and normalizes loaded rows', () => {
  const latest = thread('a', 300), b = thread('b', 200);
  const previous = Object.freeze([latest, b, thread('a', 100)]);
  assert.deepEqual(mergeThreadPage(previous, [], false), []);
  assert.deepEqual(mergeThreadPage(previous, [], true), [latest, b]);
  assert.deepEqual(mergeThreadPage([], []), []);
});
