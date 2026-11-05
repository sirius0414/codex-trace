/** 验证连续 JSON 排版、局部折叠和搜索窗口；原文只由外层负责复制。 */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { parseJsonDocument } from '../../frontend/src/components/reader/json-model.ts';
import { revealJsonHit, JSON_READING_LIMITS } from '../../frontend/src/components/reader/json-layout.ts';
let server, JsonReader, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ JsonReader } = await server.ssrLoadModule('/frontend/src/components/reader/JsonReader.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });
function render(props) {
  return renderToStaticMarkup(React.createElement(PreferencesProvider, null, React.createElement(JsonReader, { onState() {}, ...props })));
}
const visible = html => html.replace(/<[^>]*>/g, '').replaceAll('&quot;', '"').replaceAll('&amp;', '&');
test('ordinary nested JSON reads continuously without tree controls or data normalization', () => {
  const text = '{"i\\u0064":900719925474099312345,"id":1.00e+009,"nested":{"s":"a\\nb","ok":false},"empty":[]}';
  const html = render({ text });
  assert.match(visible(html), /"i\\u0064": 900719925474099312345/);
  assert.match(visible(html), /"id": 1.00e\+009/);
  assert.match(visible(html), /"s": "a\\nb"/);
  assert.match(visible(html), /"empty": \[\]/);
  assert.doesNotMatch(html, /<button|<input|<select|Field directory|Copy|depth/);
});
test('only long values fold, while root and short siblings remain visible', () => {
  const text = JSON.stringify({ object: { body: 'x'.repeat(650) }, array: Array(200).fill('value'), string: 'long-start'+ 'x'.repeat(650)+'hidden-tail', short: { nested: true } });
  const html = render({ text });
  assert.match(html, /\{…\}|\[…\]/); assert.match(html, /long-start/);
  assert.doesNotMatch(html, /hidden-tail|Collapse \/|Expand \"/);
  assert.match(html, /nested/); assert.match(html, /true/);
  const document = parseJsonDocument(text).document, string = document.root.children[2];
  const expanded = render({ text, state: { expanded: { [string.id]: true } } });
  assert.match(expanded, /hidden-tail/); assert.match(expanded, /Collapse \/string/);
  assert.match(expanded, /Expand \/object/);
});
test('entry search opens a late match and a bounded row window without changing unrelated choices', () => {
  const text = JSON.stringify({ other: 'x'.repeat(700), rows: Array.from({length: 20000}, (_, i) => ({ note: `row-${i}` })) });
  const document = parseJsonDocument(text).document, start = text.indexOf('row-19999');
  const hit = { part: 'output', start, end: start + 9, match: 'row-19999' };
  const state = revealJsonHit(document, { expanded: { [document.root.children[0].id]: false } }, hit);
  const html = render({ text, state, hit });
  assert.match(html, /<mark data-reader-hit="true" tabindex="-1">row-19999<\/mark>/);
  assert.ok((html.match(/cx-reader-code-line/g) ?? []).length <= JSON_READING_LIMITS.rows);
  assert.equal(state.expanded[document.root.children[0].id], false);
});
test('long strings use bounded windows that search can move beyond the first 100k characters', () => {
  const text = JSON.stringify({ body: 'x'.repeat(250000)+'END_OF_STRING', after: false });
  const document = parseJsonDocument(text).document, node = document.root.children[0];
  const initial = render({ text, state: { expanded: { [node.id]: true } } });
  assert.ok(initial.length < 105000); assert.doesNotMatch(initial, /END_OF_STRING/);
  assert.match(initial, /Show more loaded text/); assert.match(initial, /after/);
  const start = text.indexOf('END_OF_STRING'), hit = { part: 'output', start, end: start+13, match:'END_OF_STRING' };
  const html = render({ text, state: revealJsonHit(document, {}, hit), hit });
  assert.match(html, /<mark data-reader-hit="true" tabindex="-1">END_OF_STRING<\/mark>/);
  assert.ok(html.length < 105000);
});
test('invalid, incomplete and oversized JSON fall back to bounded source without extra copy/search', () => {
  for (const text of ['{"a":', 'x'.repeat(8_000_050)]) {
    const html = render({ text });
    assert.match(html, /showing source/);
    assert.ok(html.length < 102000);
    assert.doesNotMatch(html, /Copy|type="search"/);
  }
});
test('line thresholds apply to values but never collapse the root', () => {
  const text = '{"many":[' + '\n'.repeat(13) + '1],"tail":2}';
  assert.match(render({text}), /Expand \/many/);
  assert.doesNotMatch(render({text: '[' + '\n'.repeat(20) + '1]'}), /Expand/);
});
