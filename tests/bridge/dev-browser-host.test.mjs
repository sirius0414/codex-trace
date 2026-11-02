import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../scripts/browser_dev/host.js', import.meta.url), 'utf8');
const token = 'A'.repeat(43);
const replacementToken = 'b_'.repeat(21) + '-';
const origin = 'http://127.0.0.1:4848';
const copy = value => JSON.parse(JSON.stringify(value));

function launch({ path = '/?view=trace', hash = '', storage = new Map(), storageFailure, results = [] } = {}) {
  let address = new URL(`${origin}${path}${hash}`);
  const loads = [];
  const sent = [];
  const requests = [];
  const timers = [];
  const listeners = new Map();
  const status = { textContent: '', dataset: {} };
  const frameWindow = {
    postMessage(message, targetOrigin) { sent.push({ message: copy(message), targetOrigin }); },
  };
  const frame = {
    contentWindow: frameWindow,
    get src() { return loads.at(-1)?.src; },
    set src(value) { loads.push({ src: value, address: address.href }); },
  };
  const location = {
    get origin() { return address.origin; },
    get hash() { return address.hash; },
    get pathname() { return address.pathname; },
    get search() { return address.search; },
    reload() {},
  };
  const sessionStorage = Object.fromEntries(['getItem', 'setItem', 'removeItem'].map(method => [method,
    (key, value) => {
      if (storageFailure === 'methods') throw new Error('storage disabled');
      if (method === 'getItem') return storage.get(key) ?? null;
      if (method === 'setItem') storage.set(key, String(value));
      if (method === 'removeItem') storage.delete(key);
    },
  ]));
  const surface = { getBoundingClientRect: () => ({ width: 900, height: 700 }) };
  const elements = { '#app': frame, '#surface': surface, '#status': status,
    '#refresh': { addEventListener() {} } };
  const sandbox = {
    document: { querySelector: selector => elements[selector] },
    window: {
      addEventListener(type, listener) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(listener);
      },
    },
    location,
    history: { replaceState(_state, _title, url) { address = new URL(url, address); } },
    sessionStorage,
    navigator: { language: 'en-US' },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    ResizeObserver: class { observe() {} },
    AbortSignal: { timeout: milliseconds => ({ milliseconds }) },
    setTimeout(callback) { timers.push(callback); },
    fetch: async (url, options) => {
      requests.push({ url, ...options, headers: copy(options.headers) });
      const result = results.shift() ?? { content: [], structuredContent: { connected: true } };
      return { ok: true, json: async () => copy(result) };
    },
  };
  if (storageFailure === 'access') {
    Object.defineProperty(sandbox, 'sessionStorage', {
      get() { throw new Error('storage access denied'); },
    });
  }
  vm.runInNewContext(source, sandbox, { filename: 'scripts/browser_dev/host.js' });

  return {
    loads, sent, requests, status, storage,
    runTimers() { for (const callback of timers) callback(); },
    get address() { return address.href; },
    async message(data, { source = frameWindow, origin: eventOrigin = origin } = {}) {
      for (const listener of listeners.get('message') ?? []) {
        await listener({ source, origin: eventOrigin, data });
      }
      // Initialization starts open() without awaiting it; let its fetch and response settle.
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

async function initialize(host) {
  await host.message({ jsonrpc: '2.0', id: 1, method: 'ui/initialize',
    params: { protocolVersion: '2026-01-26' } });
  await host.message({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
}

function assertPrivateTransport(host, bearer) {
  assert.equal(host.address.includes(bearer), false);
  assert.equal(JSON.stringify(host.loads).includes(bearer), false);
  assert.equal(JSON.stringify(host.sent).includes(bearer), false);
  for (const request of host.requests) {
    assert.equal(request.url, '/rpc');
    assert.equal(request.url.includes(bearer), false);
    assert.equal(request.body.includes(bearer), false);
    assert.equal(request.headers['X-Codex-Trace-Token'], bearer);
    const { 'X-Codex-Trace-Token': _privateHeader, ...otherHeaders } = request.headers;
    assert.equal(JSON.stringify(otherHeaders).includes(bearer), false);
    assert.equal(request.method, 'POST');
    assert.equal(request.cache, 'no-store');
    assert.equal(request.credentials, 'omit');
  }
}

test('anonymous root never activates the iframe or forwards an RPC', async () => {
  const host = launch({ path: '/' });
  assert.equal(host.address, `${origin}/`);
  assert.deepEqual(host.loads, []);
  assert.deepEqual(host.requests, []);
  assert.equal(host.status.dataset.state, 'error');
  const launchHint = host.status.textContent;
  host.runTimers();
  assert.equal(host.status.textContent, launchHint);

  // Even a fabricated handshake from the dormant frame cannot obtain a credential.
  await initialize(host);
  await host.message({ jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'probe_diagnostic_event', arguments: {} } });
  assert.deepEqual(host.loads, []);
  assert.deepEqual(host.requests, []);
  assert.ok(host.sent.find(entry => entry.message.id === 2)?.message.error);
});

test('private launch clears the fragment before loading and keeps the bearer in RPC headers', async () => {
  const opened = { content: [], _meta: { 'codex-trace/result': { activationId: 'private-activation' } } };
  const toolResult = { content: [{ type: 'text', text: 'Local request failed.' }], isError: true,
    _meta: { 'codex-trace/result': { kind: 'private-receipt', requestId: 'request-1' } } };
  const host = launch({ hash: `#token=${token}`, results: [opened, toolResult] });
  assert.equal(host.loads.length, 1);
  assert.equal(host.loads[0].src, '/app.html');
  assert.equal(host.loads[0].address, `${origin}/?view=trace`);
  assert.deepEqual(host.requests, []);

  await initialize(host);
  assert.deepEqual(host.sent[0].message.result.hostCapabilities, { serverTools: {} });
  assert.equal(host.sent[0].message.result.protocolVersion, '2026-01-26');
  assert.deepEqual(JSON.parse(host.requests[0].body), { name: 'open_codex_trace', arguments: {} });
  assert.deepEqual(host.sent.find(entry => entry.message.method === 'ui/notifications/tool-result'), {
    message: { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: opened }, targetOrigin: origin,
  });

  await host.message({ jsonrpc: '2.0', id: 'private-call', method: 'tools/call',
    params: { name: 'probe_export_records', arguments: { requestId: 'request-1' } } });
  assert.deepEqual(JSON.parse(host.requests[1].body), {
    name: 'probe_export_records', arguments: { requestId: 'request-1' },
  });
  assert.deepEqual(host.sent.find(entry => entry.message.id === 'private-call'), {
    message: { jsonrpc: '2.0', id: 'private-call', result: toolResult }, targetOrigin: origin,
  });
  await host.message({ jsonrpc: '2.0', method: 'ui/notifications/initialized' });
  assert.equal(host.requests.length, 2);
  assertPrivateTransport(host, token);
});

test('refresh restores the private credential from the same tab session', async () => {
  const first = launch({ hash: `#token=${token}` });
  assert.deepEqual([...first.storage.values()], [token]);
  const refreshed = launch({ storage: first.storage });
  assert.equal(refreshed.loads[0].src, '/app.html');
  await initialize(refreshed);
  assert.equal(refreshed.requests.length, 1);
  assertPrivateTransport(refreshed, token);
});

test('a fresh private launch replaces the previous stored credential', async () => {
  const previous = launch({ hash: `#token=${token}` });
  const host = launch({ hash: `#token=${replacementToken}`, storage: previous.storage });
  assert.deepEqual([...host.storage.values()], [replacementToken]);
  await initialize(host);
  assert.equal(host.requests.length, 1);
  assertPrivateTransport(host, replacementToken);
  assert.equal(JSON.stringify(host.requests).includes(token), false);
});

test('invalid fragments invalidate the old credential instead of falling back to it', async t => {
  for (const hash of [
    '#token=short',
    `#token=${token}&token=${replacementToken}`,
    `#token=%41${token.slice(1)}`,
    `#token=${token}&other=value`,
    `#token=${token}%26token=${replacementToken}`,
    `#token=${token}=`,
  ]) {
    await t.test(hash, async () => {
      const previous = launch({ hash: `#token=${replacementToken}` });
      const host = launch({ hash, storage: previous.storage });
      assert.equal(host.address, `${origin}/?view=trace`);
      assert.deepEqual(host.loads, []);
      assert.deepEqual([...host.storage.values()], []);
      await initialize(host);
      await host.message({ jsonrpc: '2.0', id: 2, method: 'tools/call',
        params: { name: 'probe_diagnostic_event', arguments: {} } });
      assert.deepEqual(host.requests, []);
      assert.equal(host.status.dataset.state, 'error');
    });
  }
});

test('messages from a different window or origin cannot initialize or forward RPC', async () => {
  const host = launch({ hash: `#token=${token}` });
  const hostileSenders = [{ source: {} }, { origin: 'http://localhost:4848' }, { origin: 'null' }];
  for (const sender of hostileSenders) {
    await host.message({ jsonrpc: '2.0', id: 1, method: 'ui/initialize', params: {} }, sender);
    await host.message({ jsonrpc: '2.0', method: 'ui/notifications/initialized' }, sender);
  }
  assert.deepEqual(host.sent, []);
  assert.deepEqual(host.requests, []);
  await initialize(host);
  const messagesBefore = host.sent.length;
  for (const sender of hostileSenders) {
    await host.message({ jsonrpc: '2.0', id: 2, method: 'tools/call',
      params: { name: 'probe_diagnostic_event', arguments: {} } }, sender);
  }
  assert.equal(host.sent.length, messagesBefore);
  assert.equal(host.requests.length, 1);
  assertPrivateTransport(host, token);
});

test('unavailable session storage still permits the current valid fragment in memory', async t => {
  for (const storageFailure of ['access', 'methods']) {
    await t.test(storageFailure, async () => {
      const host = launch({ hash: `#token=${token}`, storageFailure });
      assert.equal(host.address, `${origin}/?view=trace`);
      assert.equal(host.loads.length, 1);
      assert.equal(host.loads[0].src, '/app.html');
      await initialize(host);
      assert.equal(host.requests.length, 1);
      assertPrivateTransport(host, token);
    });
  }
});
