import assert from 'node:assert/strict';
import test from 'node:test';
import { ContextDiagnosticGate } from '../../frontend/src/bridge/context-diagnostics.mjs';
import { HostAdapter } from '../../frontend/src/bridge/host.ts';

test('same field names produce one diagnostic despite repeated events and reordered keys', () => {
  const gate = new ContextDiagnosticGate();
  assert.equal(gate.shouldReport('activation-a', { theme: 'light', containerDimensions: { width: 600 } }), true);
  for (let index = 0; index < 18; index += 1) {
    assert.equal(gate.shouldReport('activation-a', { containerDimensions: { width: 600 + index }, theme: 'dark' }), false);
  }
});

test('a new field set or activation can be diagnosed without duplicating the same activation', () => {
  const gate = new ContextDiagnosticGate();
  assert.equal(gate.shouldReport(null, { theme: 'light' }), false);
  assert.equal(gate.shouldReport('activation-a', { theme: 'light' }), true);
  assert.equal(gate.shouldReport('activation-a', { theme: 'dark' }), false);
  assert.equal(gate.shouldReport('activation-a', { theme: 'dark', displayMode: 'inline' }), true);
  assert.equal(gate.shouldReport('activation-a', { displayMode: 'fullscreen', theme: 'light' }), false);
  assert.equal(gate.shouldReport('activation-b', { theme: 'light', displayMode: 'fullscreen' }), true);
  assert.equal(gate.shouldReport('activation-b', { theme: 'dark', displayMode: 'fullscreen' }), false);
});

test('diagnostic signatures never read or retain host context values', () => {
  const gate = new ContextDiagnosticGate();
  const context = Object.defineProperty({}, 'privateField', {
    enumerable: true,
    get() { throw new Error('context values must not be inspected for key diagnostics'); },
  });
  assert.equal(gate.shouldReport('activation-a', context), true);
  assert.equal(gate.shouldReport('activation-a', { privateField: 'PRIVATE_CONTEXT_VALUE' }), false);
  assert.equal(JSON.stringify(gate).includes('PRIVATE_CONTEXT_VALUE'), false);
});

test('a diagnostic-induced context echo stops while actual theme and dimensions keep updating', () => {
  const gate = new ContextDiagnosticGate();
  const contexts = [];
  let diagnostics = 0;
  const host = new HostAdapter(() => {}, next => {
    contexts.push(next); // Mirrors the unconditional UI context application.
    if (gate.shouldReport('activation-a', next)) {
      diagnostics += 1;
      assert.equal(diagnostics, 1, 'the echo must not send another diagnostic');
      // Simulate a host that emits another context notification for each RPC.
      host.app.onhostcontextchanged({ theme: 'dark', containerDimensions: { width: 720, height: 520 } });
    }
  });
  host.app.onhostcontextchanged({ theme: 'light', containerDimensions: { width: 600, height: 520 } });
  host.app.onhostcontextchanged({ containerDimensions: { width: 800, height: 600 } });
  assert.equal(diagnostics, 1);
  assert.equal(contexts.length, 3);
  assert.equal(contexts[0].theme, 'light');
  assert.equal(contexts[1].theme, 'dark');
  assert.deepEqual(contexts[2].containerDimensions, { width: 800, height: 600 });
  assert.equal(contexts[2].theme, 'dark');
});

test('independent UI instances do not suppress each other', () => {
  const first = new ContextDiagnosticGate();
  const second = new ContextDiagnosticGate();
  assert.equal(first.shouldReport('activation-a', { theme: 'light' }), true);
  assert.equal(first.shouldReport('activation-a', { theme: 'light' }), false);
  assert.equal(second.shouldReport('activation-a', { theme: 'light' }), true);
});
