import assert from 'node:assert/strict';
import test from 'node:test';
import { HostAdapter } from '../../frontend/src/bridge/host.ts';

test('early opening result waits for the shared initialization handshake', async () => {
  let finishHandshake;
  const handshake = new Promise(resolve => { finishHandshake = resolve; });
  let connectionCount = 0;
  const calls = [];
  let observation;
  const host = new HostAdapter(activation => { observation = host.observe(activation); }, () => {});
  host.app.connect = async () => { connectionCount += 1; await handshake; };
  host.app.getHostContext = () => ({ displayMode: 'fullscreen', theme: 'dark' });
  host.app.getHostCapabilities = () => ({ serverTools: {}, downloadFile: {} });
  host.app.callServerTool = async request => {
    calls.push(request);
    return { content: [], structuredContent: { recorded: true } };
  };
  const connecting = host.connect();
  assert.equal(host.connect(), connecting);
  host.app.ontoolresult({ content: [], structuredContent: { kind: 'activation', activationId: 'synthetic-activation' } });
  await Promise.resolve();
  assert.equal(connectionCount, 1);
  assert.equal(calls.length, 0);
  finishHandshake();
  await connecting;
  assert.deepEqual(await observation, { recorded: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'probe_host_observation');
  assert.deepEqual(calls[0].arguments.contextKeys, ['displayMode', 'theme']);
  assert.equal(calls[0].arguments.displayMode, 'fullscreen');
  assert.equal(calls[0].arguments.activationId, 'synthetic-activation');
  assert.deepEqual(calls[0].arguments.hostCapabilityKeys, ['downloadFile', 'serverTools']);
});

test('failed tool envelope cannot be treated as successful structured data', async () => {
  const host = new HostAdapter(() => {}, () => {});
  host.app.connect = async () => {};
  host.app.getHostContext = () => ({});
  host.app.callServerTool = async () => ({ content: [], isError: true, structuredContent: { recorded: true } });
  await assert.rejects(host.call('probe_host_observation', {}), /本地工具读取失败/);
});

test('host handshake and diagnostic RPC carry hard deadlines, abort signals and instance identity', async () => {
  const host = new HostAdapter(() => {}, () => {});
  const handshakes = [];
  const calls = [];
  host.app.connect = async (transport, options) => { handshakes.push({ transport, options }); };
  host.app.getHostContext = () => ({ displayMode: 'fullscreen' });
  host.app.callServerTool = async (request, options) => {
    calls.push({ request, options });
    return { content: [], structuredContent: { recorded: true } };
  };

  assert.deepEqual(await host.diagnostic('synthetic-activation', 'download-error',
    { code: 'timeout', elapsedMs: 12000 }), { recorded: true });
  assert.equal(handshakes.length, 1);
  assert.equal(handshakes[0].transport, undefined);
  assert.equal(handshakes[0].options.timeout, 12000);
  assert.equal(handshakes[0].options.maxTotalTimeout, 12000);
  assert.ok(handshakes[0].options.signal instanceof AbortSignal);
  assert.equal(handshakes[0].options.signal.aborted, false);

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].request, {
    name: 'probe_diagnostic_event',
    arguments: { activationId: 'synthetic-activation', uiInstanceId: host.instanceId,
      phase: 'download-error', code: 'timeout', elapsedMs: 12000 },
  });
  assert.equal(calls[0].options.timeout, 12000);
  assert.equal(calls[0].options.maxTotalTimeout, 12000);
  assert.equal(calls[0].options.resetTimeoutOnProgress, false);
  assert.ok(calls[0].options.signal instanceof AbortSignal);
  assert.equal(calls[0].options.signal.aborted, false);
  assert.notEqual(calls[0].options.signal, handshakes[0].options.signal);
});

test('missing structured data is rejected without exposing model-visible response content', async () => {
  const host = new HostAdapter(() => {}, () => {});
  host.app.connect = async () => {};
  host.app.getHostContext = () => ({});
  for (const response of [
    { content: [{ type: 'text', text: 'PRIVATE_RESPONSE_CONTENT' }] },
    { content: [], structuredContent: null },
  ]) {
    host.app.callServerTool = async () => response;
    await assert.rejects(host.call('probe_diagnostic_event', {}), error => {
      assert.equal(error.code, 'invalid-envelope');
      assert.match(error.message, /响应缺少结构化结果/);
      assert.equal(error.message.includes('PRIVATE_RESPONSE_CONTENT'), false);
      return true;
    });
  }
});

test('local export reads only private metadata with a hard RPC deadline and exact request binding', async () => {
  const host = new HostAdapter(() => {}, () => {});
  host.app.connect = async () => {};
  host.app.getHostContext = () => ({});
  const args = { activationId: '11111111-1111-4111-8111-111111111111', uiInstanceId: host.instanceId,
    requestId: '33333333-3333-4333-8333-333333333333' };
  const receipt = { kind: 'local-export', requestId: args.requestId };
  const calls = [];
  host.app.callServerTool = async (request, options) => {
    calls.push({ request, options });
    return { content: [{ type: 'text', text: '本地导出请求已处理。' }],
      structuredContent: { kind: 'untrusted-fallback' }, _meta: { 'codex-trace/result': receipt } };
  };
  assert.equal(await host.privateResult('probe_export_records', args), receipt);
  assert.deepEqual(calls[0].request, { name: 'probe_export_records', arguments: args });
  assert.equal(calls[0].options.timeout, 12000);
  assert.equal(calls[0].options.maxTotalTimeout, 12000);
  assert.equal(calls[0].options.resetTimeoutOnProgress, false);
  assert.ok(calls[0].options.signal instanceof AbortSignal);
  assert.equal(calls[0].options.signal.aborted, false);
  host.app.callServerTool = async () => ({ content: [], _meta: { 'codex-trace/result': receipt } });
  assert.equal(await host.privateResult('probe_export_records', args), receipt);
});

test('private-result RPC never falls back to structured content or leaks invalid response content', async () => {
  const host = new HostAdapter(() => {}, () => {});
  host.app.connect = async () => {};
  host.app.getHostContext = () => ({});
  for (const privateMeta of [undefined, {}, { wrong: {} }, { 'codex-trace/result': null },
    { 'codex-trace/result': [] }, { 'codex-trace/result': 'PRIVATE_RESPONSE_CONTENT' }]) {
    host.app.callServerTool = async () => ({ content: [{ type: 'text', text: 'PRIVATE_RESPONSE_CONTENT' }],
      structuredContent: { kind: 'local-export' }, _meta: privateMeta });
    await assert.rejects(host.privateResult('probe_export_records', {}), error => {
      assert.equal(error.code, 'invalid-envelope');
      assert.match(error.message, /私有结果/);
      assert.equal(error.message.includes('PRIVATE_RESPONSE_CONTENT'), false);
      return true;
    });
  }
  host.app.callServerTool = async () => ({ content: [{ type: 'text', text: 'PRIVATE_RESPONSE_CONTENT' }],
    isError: true, _meta: { 'codex-trace/result': { kind: 'local-export' } } });
  await assert.rejects(host.privateResult('probe_export_records', {}), error => {
    assert.equal(error.code, 'isError');
    assert.equal(error.message.includes('PRIVATE_RESPONSE_CONTENT'), false);
    return true;
  });
});
