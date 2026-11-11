import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server, ExportNotice, PreferencesProvider;
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('../..', import.meta.url)), configFile: false,
    appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  ({ default: ExportNotice } = await server.ssrLoadModule('/frontend/src/components/ExportNotice.tsx'));
  ({ PreferencesProvider } = await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async () => { await server?.close(); });

test('export result remains keyboard readable and has a separately labelled dismissal control', () => {
  const message = 'Export failed: original records are not readable. No file was saved.';
  const html = renderToStaticMarkup(React.createElement(PreferencesProvider, null,
    React.createElement(ExportNotice, { message, onDismiss() {} })));
  assert.match(html, /role="status" tabindex="0"/);
  assert.ok(html.includes(message));
  assert.match(html, /<button type="button"[^>]*aria-label="Dismiss export notice"/);
  assert.ok(html.indexOf('</div>') < html.indexOf('<button'), 'the dismissal control is outside the live result text');
});

test('export result keeps complete receipts and treats source strings as text', () => {
  const message = `Local export verified.\nC:\\Users\\Synthetic\\Downloads\\Codex Trace\\${'long-'.repeat(100)}.zip\nSHA-256 ${'a'.repeat(64)}\n<script>untrusted</script>`;
  const html = renderToStaticMarkup(React.createElement(PreferencesProvider, null,
    React.createElement(ExportNotice, { message, onDismiss() {} })));
  assert.ok(html.includes('long-'.repeat(100)));
  assert.ok(html.includes(`SHA-256 ${'a'.repeat(64)}`));
  assert.match(html, /&lt;script&gt;untrusted&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
