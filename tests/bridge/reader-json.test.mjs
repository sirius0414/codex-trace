import test from 'node:test';
import assert from 'node:assert/strict';
import { parseJsonDocument, rawJsonValue, searchJson, jsonAncestors, matchingField, revealJsonNode, jsonStringPresentation } from '../../frontend/src/components/reader/json-model.ts';

test('JSON reading preserves unsafe integers, exponent spelling, duplicate keys, escaping and empty values', () => {
  const text = '{ "id":900719925474099312345, "id":1.00e+009, "text":"a\\nb", "empty":"", "null":null,"false":false,"zero":0,"array":[],"object":{} }';
  const { document } = parseJsonDocument(text);
  assert.ok(document);
  const fields = document.root.children;
  assert.equal(fields.length, 9);
  assert.equal(document.duplicateKeys, true);
  assert.equal(rawJsonValue(document, fields[0]), '900719925474099312345');
  assert.equal(rawJsonValue(document, fields[1]), '1.00e+009');
  assert.notEqual(fields[0].id, fields[1].id);
  assert.equal(rawJsonValue(document, fields[2]), '"a\\nb"');
  assert.equal(fields[2].value, 'a\nb');
  assert.equal(fields[3].value, '');
  assert.deepEqual(fields.slice(4).map(n => n.kind), ['null', 'boolean', 'number', 'array', 'object']);
  assert.equal(document.text, text);
});

test('partial, malformed and trailing JSON never receives a fabricated tree', () => {
  for (const text of ['{"a":', '{"a":1,}', '[1,]', 'false trailing', '01', '"\\q"', '"unterminated', '{"a":"line\nbreak"}']) {
    assert.equal(parseJsonDocument(text).error, 'syntax', text);
  }
  assert.equal(parseJsonDocument('['.repeat(140) + '0' + ']'.repeat(140)).error, 'limit');
});

test('search indexes folded values and escaped keys, producing ancestors and precise source slices', () => {
  const { document } = parseJsonDocument('{"a/b":{"~key":[{"needle":"FOUND"}]},"untouched":false}');
  const { matches, total } = searchJson(document, 'found');
  assert.equal(total, 1);
  assert.equal(matches[0].path, '/a~1b/~0key/0/needle');
  assert.deepEqual(jsonAncestors(document, matches[0]).map(n => n.path), ['', '/a~1b', '/a~1b/~0key', '/a~1b/~0key/0']);
  assert.equal(rawJsonValue(document, matches[0]), '"FOUND"');
  assert.equal(searchJson(document, '~key').total, 1);
});

test('large collections retain complete structure while search result lists stay bounded', () => {
  const text = JSON.stringify({ rows: Array.from({ length: 12_000 }, (_, index) => ({ index, note: `searchable ${index}` })), tail: 'last field' });
  const { document } = parseJsonDocument(text);
  assert.equal(document.root.children.length, 2);
  assert.equal(document.root.children[0].children.length, 12_000);
  const results = searchJson(document, 'searchable', 100);
  assert.equal(results.total, 12_000);
  assert.equal(results.matches.length, 100);
  assert.equal(searchJson(document, 'last field').matches[0].path, '/tail');
});

test('field documentation uses exact paths or explicit array wildcards, never matching a name globally', () => {
  const fields = [{ path: '/items/*/code', language: 'python' }, { path: '/code', language: 'typescript' }];
  assert.equal(matchingField(fields, '/items/3/code').language, 'python');
  assert.equal(matchingField(fields, '/other/code'), undefined);
  assert.equal(matchingField(fields, '/items/foo/code'), undefined);
  assert.equal(matchingField(fields, '/code').language, 'typescript');
});


test('locating a late array match opens only its ancestry and a bounded value window', () => {
  const { document } = parseJsonDocument(JSON.stringify({ before: { fold: true }, rows: Array.from({ length: 12_000 }, (_, index) => ({ note: index === 11999 ? 'target' : 'other' })) }));
  const node = searchJson(document, 'target').matches[0];
  const before = document.root.children[0];
  const state = { expanded: { [before.id]: false }, offsets: {}, limits: {} };
  const next = revealJsonNode(document, node, state);
  const rows = document.root.children[1];
  assert.equal(next.offsets[rows.id], 11900);
  assert.equal(next.limits[rows.id], 100);
  assert.equal(next.expanded[before.id], false);
  for (const ancestor of jsonAncestors(document, node)) assert.equal(next.expanded[ancestor.id], true);
  assert.equal(next.selected, node.id);
  assert.deepEqual(state, { expanded: { [before.id]: false }, offsets: {}, limits: {} });
  const first = rows.children[0].children[0];
  assert.equal(revealJsonNode(document, first, next).offsets[rows.id], 0);
});


test('short strings use explicit format evidence and diff language, never their field name', () => {
  const { document } = parseJsonDocument('{"diff":"-old\\n+new", "code":"plain"}');
  const [diff, code] = document.root.children;
  assert.deepEqual(jsonStringPresentation(diff, { format: 'diff' }), { expandable: true, mode: 'code', language: 'diff' });
  assert.deepEqual(jsonStringPresentation(code, { format: 'code', language: 'bash' }), { expandable: true, mode: 'code', language: 'bash' });
  assert.equal(jsonStringPresentation(code, { format: 'markdown' }).mode, 'markdown');
  assert.equal(jsonStringPresentation(diff).expandable, true);
  assert.deepEqual(jsonStringPresentation(diff, { format: 'json' }), { expandable: true, mode: 'code', language: 'json' });
  assert.equal(jsonStringPresentation(code, { format: 'text' }).mode, 'text');
});


test('object field identities survive inserted/reordered siblings while retaining paging positions', () => {
  const before = parseJsonDocument('{"keep":{"nested":[{"value":1}]}, "duplicate":1,"duplicate":2}').document;
  const after = parseJsonDocument('{"new":true,"duplicate":1,"keep":{"newNested":false,"nested":[{"value":2}]},"duplicate":2}').document;
  for (const path of ['/keep', '/keep/nested', '/keep/nested/0', '/keep/nested/0/value']) {
    const previous = [...before.nodes.values()].find(node => node.path === path);
    const current = [...after.nodes.values()].find(node => node.path === path);
    assert.equal(current.id, previous.id, path);
  }
  assert.equal(after.root.children[2].index, 2);
  assert.equal(before.root.children[0].index, 0);
  assert.deepEqual([...before.nodes.values()].filter(node => node.path === '/duplicate').map(node => node.id), [...after.nodes.values()].filter(node => node.path === '/duplicate').map(node => node.id));
});


test('string bodies decode JSON exactly once and keep literal paths, regex escapes and inner JSON intact', () => {
  const original = {
    text: 'first\nsecond',
    literal: String.raw`first\nsecond`,
    windows: String.raw`C:\new\notes.txt`,
    regex: String.raw`/^\w+\n$/`,
    inner: String.raw`{"line":"a\nb"}`,
  };
  // 使用真正的换行作为对照；其余样本的反斜杠均属于已记录正文。
  const source = JSON.stringify(original), { document } = parseJsonDocument(source);
  for (const node of document.root.children) {
    assert.equal(node.value, original[node.key]);
    assert.equal(rawJsonValue(document, node), JSON.stringify(original[node.key]));
  }
  assert.equal(document.root.children[1].value.includes('\n'), false);
  assert.equal(document.root.children[2].value.includes('\n'), false);
  assert.equal(jsonStringPresentation(document.root.children[4]).language, 'json');
  assert.equal(document.text, source);
});

test('short multiline and root scalar strings expose body reading while strong Markdown is detected inside text transport', () => {
  const { document } = parseJsonDocument(JSON.stringify({ text: 'first\nsecond', output: '# Heading\n\n- one\n- two', markdown: 'ordinary prose' }));
  const [text, markdown, plain] = document.root.children;
  assert.equal(jsonStringPresentation(text).expandable, true);
  assert.equal(jsonStringPresentation(text).mode, 'text');
  assert.equal(jsonStringPresentation(markdown, { format: 'text' }).mode, 'markdown');
  assert.equal(jsonStringPresentation(plain).mode, 'text');
  assert.equal(jsonStringPresentation(parseJsonDocument('"short body"').document.root).expandable, true);
});
