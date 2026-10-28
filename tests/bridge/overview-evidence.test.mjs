/** Render the actual overview so extra SVG overlays cannot invent model activity. */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { defaultView } from '../../frontend/src/state/session.ts';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url), 'utf8'));
let server, Overview, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ default: Overview } = await server.ssrLoadModule('/frontend/src/components/Overview.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });

function event(kind, startMs, endMs = null, section = 'timeline') {
  return { ...fixture.events[0], eventId: `${kind}:${startMs}`, kind, preview: kind,
    turnId: section === 'context' ? null : 'turn-1', order: { section, ordinal: startMs },
    timing: { ...fixture.events[0].timing, startMs, endMs,
      durationMs: endMs == null ? null : endMs - startMs, precision: endMs == null ? 'startOnly' : 'exact' } };
}
function render(events, viewPatch = {}) {
  const snapshot = { ...fixture, events, turns: [{ ...fixture.turns[0], executionIntervals: [
    { startMs: 0, endMs: 37000, durationMs: 37000, precision: 'exact' },
  ] }] };
  const html = renderToStaticMarkup(React.createElement(PreferencesProvider, null,
    React.createElement(Overview, { snapshot, view: { ...defaultView(), ...viewPatch }, highlightedEvents: null, onView() {}, onSelect() {}, onRangeCommit() {} })));
  const nodes = [...html.matchAll(/<g\b([^>]*data-overview-key[^>]*)>([\s\S]*?)<\/g>/g)].map(match => ({
    ...Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]])), content: match[2],
  }));
  return { html, nodes, models: nodes.filter(n => /lane-1/.test(n.class)), tools: nodes.filter(n => /lane-2/.test(n.class)) };
}

test('usage snapshots do not render activity nodes but still count distinct model requests', () => {
  const events = [
    { ...event('assistantMessage', 10, 20), modelRequestKey: 'request-1' },
    { ...event('modelUsage', 21), modelRequestKey: 'request-1' },
    { ...event('modelUsage', 22), modelRequestKey: 'request-2' },
  ];
  const { nodes } = render(events);
  assert.deepEqual(nodes.map(node => node['data-event-id']), ['assistantMessage:10']);
  const { html } = render(events, { overviewMode: 'calls' });
  assert.match(html, /(?:Model requests|模型请求) <b>2<\/b>/);
});

test('Turn axis and actual model records replace synthetic time coverage', () => {
  const { html, models } = render([
    event('systemContext', 666, null, 'context'), event('developerContext', 2847),
    event('userMessage', 2847, 2847), event('assistantMessage', 4818, 5351), event('reasoningSummary', 10061, 11246),
  ]);
  assert.match(html, /data-axis-mode="turn-sequence"/);
  assert.match(html, /Turn 1/);
  assert.doesNotMatch(html, /data-execution-gap|data-active-duration-ms/);
  assert.equal(models.length, 2);
  assert.deepEqual(models.map(n => n['data-node-duration-ms']), ['533', '1185']);
  assert.ok(models.every(n => n['data-order-basis'] === 'lifecycle'));
});

test('context, input and a turn envelope cannot create model nodes; tool duration stays original', () => {
  const { models, tools } = render([
    event('systemContext', 666, null, 'context'), event('environment', 666, null, 'context'),
    event('userMessage', 2847, 2847), event('toolCall', 12000, 36000),
  ]);
  assert.deepEqual(models, []);
  assert.equal(tools.length, 1);
  assert.equal(tools[0]['data-node-duration-ms'], '24000');
  assert.match(tools[0].content, /24000 ms/);
  assert.match(tools[0].content, /data-log-width=/);
});

test('unattributed gaps never add model nodes or duration bars', () => {
  const { models } = render([event('userMessage', 2847, 2847), event('assistantMessage', 4818, 5351), event('reasoningSummary', 10061, 11246)]);
  assert.equal(models.length, 2);
  assert.deepEqual(models.map(n => n['data-event-id']), ['assistantMessage:4818', 'reasoningSummary:10061']);
});

test('large snapshots initially render a readable bounded window, not thousands of SVG nodes', () => {
  const { nodes, html } = render(Array.from({ length: 10000 }, (_, i) => event('toolCall', i * 2, i * 2 + 1)));
  assert.ok(nodes.length <= 15);
  assert.match(html, /data-viewport-max="1000"/);
});

test('concurrent calls keep individual color blocks without overlap boxes or aggregate labels', () => {
  const calls = Array.from({ length: 1000 }, (_, i) => ({ ...event('toolCall', 0, 1000), eventId: `parallel:${i}` }));
  const { nodes, html } = render(calls);
  assert.equal(nodes.length, calls.length);
  assert.equal(new Set(nodes.map(n => n['data-event-id'])).size, calls.length);
  assert.ok(nodes.every(n => n['data-parallel'] === 'true' && /compact/.test(n.class)));
  assert.ok(nodes.every(n => !/<text\b/.test(n.content)));
  assert.equal(new Set(nodes.map(n => n.content.match(/class="cx-flow-node-shell"[^>]*\by="([^"]+)"/)?.[1])).size, calls.length);
  assert.doesNotMatch(html, /data-overlap-group=|cx-flow-overlap|\d+ 条记录|aria-haspopup="dialog"/);
});
