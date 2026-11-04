/** 验证 JSON 原文回退与字符串展开的真实 DOM 有界，分页仅改变显示窗口。 */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { parseJsonDocument } from '../../frontend/src/components/reader/json-model.ts';
let server, JsonReader, RichReader, CodeReader, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ JsonReader } = await server.ssrLoadModule('/frontend/src/components/reader/JsonReader.tsx'));
  ({ RichReader, CodeReader } = await server.ssrLoadModule('/frontend/src/components/reader/RichReader.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });
function render(props) {
  return renderToStaticMarkup(React.createElement(PreferencesProvider, null, React.createElement(JsonReader, {
    copy() {}, onState() {},
    renderString: (text, mode, language) => React.createElement(mode === 'markdown' ? RichReader : CodeReader, { text, language, copy() {} }),
    ...props,
  })));
}
function pre(html) { return /<pre[^>]*>([\s\S]*?)<\/pre>/.exec(html)?.[1] ?? ''; }

test('JSON above the parser limit renders only its initial source window and restores the requested window', () => {
  const text = 'x'.repeat(8_000_050) + 'END_OF_LOADED_BODY';
  const html = render({ text });
  assert.equal(pre(html).length, 100_000);
  assert.match(html, /resource|structured reader limit/);
  assert.match(html, /100,000 \/ 8,000,068 loaded code units shown/);
  assert.match(html, /copying still includes the entire loaded body/);
  assert.match(html, /Copy loaded body/);
  assert.doesNotMatch(html, /END_OF_LOADED_BODY/);
  const restored = render({ text, state: { sourceLimits: { $fallback: 200_000 } }, wrap: false });
  assert.equal(pre(restored).length, 200_000);
  assert.match(restored, /class="cx-reader-source"/);
});

test('expanded long JSON strings are bounded separately while other fields and exact source remain available', () => {
  const text = JSON.stringify({ payload: 'x'.repeat(250_000) + 'END_OF_STRING', other: false });
  const node = parseJsonDocument(text).document.root.children[0];
  const html = render({ text, state: { depth: 2 } });
  assert.ok(pre(html).length < 101_000);
  assert.doesNotMatch(pre(html), /END_OF_STRING/);
  assert.match(html, /other/); assert.match(html, /false/);
  assert.match(html, /Show 100,000 more code units/);
  assert.match(html, /Copy value \(original JSON\)/);
  const restored = render({ text, state: { depth: 2, sourceLimits: { [node.id]: 300_000 }, stringModes: { [node.id]: 'source' } } });
  assert.match(pre(restored), /END_OF_STRING/);
  assert.match(pre(restored), /^&quot;/);
  assert.match(pre(restored), /&quot;$/);
  assert.doesNotMatch(restored, /Show 100,000 more code units/);
});

test('expanded multiline strings read as decoded body by default, while source and text overrides persist', () => {
  const value = 'First paragraph\nSecond paragraph', text = JSON.stringify({ text: value });
  const node = parseJsonDocument(text).document.root.children[0];
  const html = render({ text, state: { depth: 2 } });
  assert.equal(pre(html), value);
  assert.match(html, /value="auto" selected=""/);
  assert.match(html, /Copy string body/);
  assert.match(html, /Copy JSON source/);
  assert.doesNotMatch(pre(html), /\\n|&quot;/);
  const source = render({ text, state: { depth: 2, stringModes: { [node.id]: 'source' } } });
  assert.match(pre(source), /^&quot;First paragraph\\nSecond paragraph&quot;$/);
  assert.match(source, /value="source" selected=""/);
  const plain = render({ text, state: { depth: 2, stringModes: { [node.id]: 'text' } } });
  assert.equal(pre(plain), value);
  assert.match(plain, /value="text" selected=""/);
});

test('decoded Markdown in a text transport uses the rich reader and still allows a literal text view', () => {
  const value = '# Overview\n\n| Key | Value |\n| --- | --- |\n| one | two |\n\n```bash\n  echo hello\n```';
  const text = JSON.stringify({ text: value }), node = parseJsonDocument(text).document.root.children[0];
  const metadata = { format: 'json', origin: 'test fixture', fields: [{ path: '/text', scope: 'protocol', format: 'text', description: 'Text payload.', sourceUrl: 'https://example.test/schema', sourceVersion: 'test', versionMatch: 'exact' }] };
  const html = render({ text, metadata, state: { depth: 2 } });
  assert.match(html, /<h1>/); assert.match(html, /<table>/);
  assert.match(html, /hljs-built_in/);
  const plain = render({ text, metadata, state: { depth: 2, stringModes: { [node.id]: 'text' } } });
  assert.doesNotMatch(plain, /<h1>|<table>/);
  assert.match(pre(plain), /^# Overview\n/);
});

test('literal backslashes remain literal in the decoded body and nested JSON is not decoded recursively', () => {
  const value = String.raw`C:\new\notes.txt — /^\w+\n$/`, text = JSON.stringify(value);
  assert.equal(pre(render({ text })), value);
  const nested = String.raw`{"line":"a\nb"}`;
  const html = render({ text: JSON.stringify(nested), state: { stringModes: { $: 'text' } } });
  assert.match(pre(html), /a\\nb/);
  assert.doesNotMatch(pre(html), /a\nb/);
});
