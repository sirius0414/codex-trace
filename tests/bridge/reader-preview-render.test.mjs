/** 覆盖真实 Preview DOM 的默认展开、手动状态及折叠后的读取入口。 */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server, PreviewReader, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ default: PreviewReader } = await server.ssrLoadModule('/frontend/src/components/reader/PreviewReader.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });

const body = (text, extra = {}) => ({ text, nextCursor: null, readableBytes: new TextEncoder().encode(text).length,
  completeness: 'unknown', sourceTruncated: false, ...extra });
const initialState = () => ({ expanded: false, query: '', blocks: {} });
function event(kind, parts, extra = {}) {
  return { eventId: 'preview-render', kind, toolName: kind.startsWith('tool') ? 'fixture.tool' : null,
    status: { value: 'completed' }, content: Object.entries(parts).map(([part, value]) => ({ part,
      loaded: false, readableBytes: value.readableBytes, chunkCursor: null, chunked: Boolean(value.nextCursor),
      completeness: value.completeness, sourceTruncated: value.sourceTruncated })), ...extra };
}
function render(item, parts, state = initialState(), extra = {}) {
  return renderToStaticMarkup(React.createElement(PreferencesProvider, null, React.createElement(PreviewReader, {
    event: item, detail: parts === null ? null : { eventId: item.eventId, summary: '', explanation: '', parts },
    loading: false, state, onState() {}, copy() {}, onMore() {}, ...extra,
  })));
}
function block(html, part) {
  const start = html.indexOf(`data-reader-part="${part}"`);
  assert.notEqual(start, -1, `Missing ${part} block`);
  const next = html.indexOf('<section class="cx-reader-block', start);
  return html.slice(start, next < 0 ? html.length : next);
}
function expanded(html, part, expected) {
  assert.match(block(html, part), new RegExp(`class="cx-reader-collapse" aria-expanded="${expected}"`));
}

test('a call with only output renders its only body expanded', () => {
  const parts = { output: body('The only saved output.') };
  const html = render(event('toolCall', parts), parts);
  expanded(html, 'output', true);
  assert.match(block(html, 'output'), /The only saved output\./);
  assert.match(block(html, 'output'), /Copy all loaded original text/);
});

test('calls and results render opposite defaults when both input and output exist', () => {
  const parts = { input: body('Recorded input body.'), output: body('Recorded output body.') };
  for (const kind of ['toolCall', 'toolResult']) {
    const html = render(event(kind, parts), parts);
    expanded(html, 'input', kind === 'toolCall');
    expanded(html, 'output', kind === 'toolResult');
    assert.match(block(html, kind === 'toolCall' ? 'input' : 'output'), /Recorded .* body\./);
    assert.doesNotMatch(block(html, kind === 'toolCall' ? 'output' : 'input'), /Recorded .* body\./);
  }
});

test('confirmed unloaded bodies participate in defaults before any detail arrives', () => {
  const parts = { input: body('Input is still loading.'), output: body('Output is still loading.') };
  for (const kind of ['toolCall', 'toolResult']) {
    const html = render(event(kind, parts), null, initialState(), { loading: true });
    expanded(html, 'input', kind === 'toolCall');
    expanded(html, 'output', kind === 'toolResult');
    assert.match(html, /Loading body/);
    assert.match(html, /More content is not loaded/);
  }
});

test('empty placeholders preserve the only real body and their integrity notice', () => {
  const parts = { input: body('The only saved input.'), output: body('', { completeness: 'notRecorded' }) };
  const html = render(event('toolResult', parts), parts);
  expanded(html, 'input', true);
  assert.match(block(html, 'input'), /The only saved input\./);
  assert.match(block(html, 'output'), /Output not saved/);

  const outputOnly = { input: body(''), output: body('The saved result.') };
  const callHtml = render(event('toolCall', outputOnly), outputOnly);
  expanded(callHtml, 'output', true);
  assert.match(block(callHtml, 'output'), /The saved result\./);
});

test('failed entries lead with error and initially expand all existing bodies', () => {
  const parts = { input: body('Failed input body.'), output: body('Failed output body.'), error: body('Failure details.') };
  const item = event('toolCall', parts, { status: { value: 'failed' } });
  const html = render(item, parts);
  assert.deepEqual([...html.matchAll(/data-reader-part="(\w+)"/g)].map(match => match[1]), ['error', 'input', 'output']);
  for (const part of Object.keys(parts)) {
    expanded(html, part, true);
    assert.ok(block(html, part).includes(parts[part].text));
  }
  const manual = render(item, parts, { ...initialState(), blocks: { error: { collapsed: true }, output: { collapsed: true } } });
  expanded(manual, 'error', false);
  expanded(manual, 'output', false);
  expanded(manual, 'input', true);
  assert.doesNotMatch(block(manual, 'error'), /Failure details\./);
});

test('manual collapsed and expanded states override the saved defaults', () => {
  const parts = { input: body('Input body.'), output: body('Output body.') };
  const state = { ...initialState(), blocks: {
    input: { defaultCollapsed: false, collapsed: true }, output: { defaultCollapsed: true, collapsed: false },
  } };
  const html = render(event('toolCall', parts), parts, state);
  expanded(html, 'input', false);
  expanded(html, 'output', true);
  assert.doesNotMatch(block(html, 'input'), /Input body\./);
  assert.match(block(html, 'output'), /Output body\./);
});

test('ordinary messages and a lone error body remain expanded', () => {
  const parts = { input: body('Message input.'), output: body('Message output.') };
  const html = render(event('assistantMessage', parts), parts);
  expanded(html, 'input', true);
  expanded(html, 'output', true);
  const error = { error: body('The only error body.') };
  const errorHtml = render(event('toolCall', error), error);
  expanded(errorHtml, 'error', true);
  assert.match(block(errorHtml, 'error'), /The only error body\./);
});

test('collapsed output still exposes pagination and source integrity information', () => {
  const parts = { input: body('Input body.'), output: body('Hidden loaded output.', {
    nextCursor: 'next-page', readableBytes: 400, sourceTruncated: true, completeness: 'fragment',
  }) };
  const html = render(event('toolCall', parts), parts);
  expanded(html, 'output', false);
  const output = block(html, 'output');
  assert.doesNotMatch(output, /Hidden loaded output\./);
  assert.match(output, /More content is not loaded/);
  assert.match(output, /Source was truncated/);
  assert.match(output, /Only a source fragment is saved/);
  assert.match(output, /Load more output/);
  assert.match(output, /Load all saved text in this block/);
});

test('saved source view and search location survive default initialization', () => {
  const text = '# Exact source\n\nneedle in the loaded output.';
  const parts = { input: body('Input body.'), output: body(text) };
  const start = text.indexOf('needle');
  const state = { ...initialState(), query: 'needle', hit: { part: 'output', start, end: start + 6, match: 'needle' },
    blocks: { output: { collapsed: false, source: true } } };
  const html = render(event('toolCall', parts), parts, state);
  expanded(html, 'output', true);
  assert.match(html, /1 matches/);
  assert.match(block(html, 'output'), /<pre[^>]*># Exact source/);
  assert.match(block(html, 'output'), /<mark data-reader-hit="true" tabindex="-1">needle<\/mark>/);
  assert.match(block(html, 'output'), /Copy all loaded original text/);
});

test('Preview has one loaded-entry search and one copy per visible body, with format controls inside More', () => {
  for (const text of ['{"a":1,"b":{"c":2}}', '# Heading\n\n```js\nconst x=1;\n```', 'diff --git a/a b/a\n-old\n+new', 'ordinary prose']) {
    const parts = { output: body(text) }, html = render(event('toolResult', parts), parts);
    assert.equal((html.match(/type="search"/g) ?? []).length, 1);
    assert.equal((html.match(/class="cx-button cx-reader-copy"/g) ?? []).length, 1);
    assert.doesNotMatch(html, /Copy code|Copy entire JSON|Source completeness unknown|bytes loaded|Source marked complete|field directory|Expand depth/);
    assert.match(html, /<details[\s\S]*<select[\s\S]*<\/details>/);
    assert.doesNotMatch(html, /cx-reader-integrity/);
  }
});
test('partial JSON keeps load actions and exact loaded source, known truncation is still visible', () => {
  const parts = { output: body('{"a":', { nextCursor: 'next', readableBytes: 200, sourceTruncated: true, completeness: 'fragment', reader: {format:'json'} }) };
  const html = render(event('toolResult', parts), parts);
  assert.match(html, /JSON is not fully loaded/); assert.match(html, /Load more output/);
  assert.match(html, /Source was truncated/); assert.match(html, /Only a source fragment/);
  assert.doesNotMatch(html, /bytes loaded|field directory/);
});

test('late raw-source search keeps a bounded window and original copy entry', () => {
  const text = 'x'.repeat(500000) + 'late-target';
  const parts = { output: body(text) };
  const state = { ...initialState(), hit: {part:'output',start:500000,end:500011,match:'late-target'},
    blocks:{output:{source:true,sourceOffset:499800,sourceLimit:100000}} };
  const html = render(event('toolResult',parts),parts,state);
  assert.ok(html.length<10000);
  assert.match(html, /<mark data-reader-hit="true" tabindex="-1">late-target<\/mark>/);
  assert.match(html, /Previous source section/);
  assert.match(html, /Copy all loaded original text/);
});


test('single-line tool orchestration input is JavaScript code even with a plain text transport', () => {
  const text = 'text(await tools.exec_command({cmd:"cat /synthetic/SKILL.md",max_output_tokens:6000}));';
  for (const reader of [undefined, {format:'text',origin:'recorded text'}]) {
    const parts = { input: body(text, {reader}) };
    const html = render(event('toolCall', parts, {toolName:'exec'}), parts);
    assert.match(block(html,'input'), /cx-reader-code/);
    assert.match(block(html,'input'), /class="hljs-keyword">await/);
    assert.doesNotMatch(block(html,'input'), /cx-reader-prose/);
    assert.equal((block(html,'input').match(/cx-reader-copy/g) ?? []).length, 1);
    const source = render(event('toolCall', parts), parts, {...initialState(),blocks:{input:{source:true}}});
    assert.match(source, /<pre[^>]*>text\(await tools\.exec_command/);
  }
});
