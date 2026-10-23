import test from 'node:test';
import assert from 'node:assert/strict';
import { SelectionGuard } from '../../frontend/src/bridge/selection.mjs';

test('late A cannot overwrite B, including A→B→A', () => {
  const guard = new SelectionGuard(); const a = guard.select('a', 'activation-1');
  guard.select('b'); guard.select('a'); assert.equal(guard.accepts(a), false);
});
test('a fresh activation invalidates download work even for the same task', () => {
  const guard = new SelectionGuard(); const old = guard.select('a', 'one');
  guard.select('a', 'two'); assert.equal(guard.accepts(old), false); assert.equal(guard.accepts(guard.capture()), true);
});
test('independent instances do not share selection state', () => {
  const a = new SelectionGuard(), b = new SelectionGuard(); const binding = a.select('a');
  b.select('b'); assert.equal(a.accepts(binding), true); assert.equal(b.accepts(binding), false);
});
