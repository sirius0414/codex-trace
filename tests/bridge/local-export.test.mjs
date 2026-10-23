import assert from 'node:assert/strict';
import test from 'node:test';
import { LocalExportAttempt, LocalExportBinding, exportFailure } from '../../frontend/src/bridge/local-export.mjs';
import { SelectionGuard } from '../../frontend/src/bridge/selection.mjs';

const ACTIVATION = '11111111-1111-4111-8111-111111111111';
const UI_INSTANCE = '22222222-2222-4222-8222-222222222222';
const REQUEST = '33333333-3333-4333-8333-333333333333';
const HASH = 'a'.repeat(64);
const activation = () => ({ activationId: ACTIVATION, downloadExpectedBytes: 812, downloadExpectedSha256: HASH });
const receipt = (requestId = REQUEST) => ({ kind: 'local-export', requestId, filename: '合成验证.zip',
  path: '/Users/synthetic/Local exports/合成验证.zip', bytes: 812, sha256: HASH, verified: true, synthetic: true });
const make = () => new LocalExportAttempt(activation(), UI_INSTANCE, REQUEST);

test('local export validates the activation manifest before any save can be submitted', () => {
  for (const update of [
    { activationId: '' }, { activationId: 'thread-name' }, { downloadExpectedBytes: NaN },
    { downloadExpectedBytes: -1 }, { downloadExpectedBytes: 1.5 }, { downloadExpectedBytes: Number.MAX_SAFE_INTEGER + 1 },
    { downloadExpectedSha256: 'a'.repeat(63) }, { downloadExpectedSha256: null },
  ]) assert.throws(() => new LocalExportAttempt({ ...activation(), ...update }, UI_INSTANCE, REQUEST), { code: 'invalid-manifest' });
  assert.throws(() => new LocalExportAttempt(null, UI_INSTANCE, REQUEST), { code: 'invalid-manifest' });
  assert.throws(() => new LocalExportAttempt(activation(), 'bad-ui', REQUEST), { code: 'invalid-manifest' });
  assert.throws(() => new LocalExportAttempt(activation(), UI_INSTANCE, 'bad-request'), { code: 'invalid-manifest' });
});

test('verified receipt preserves the actual local path and exposes only declared fields', async () => {
  const attempt = make();
  const result = await attempt.run(async args => {
    assert.deepEqual(args, { activationId: ACTIVATION, uiInstanceId: UI_INSTANCE, requestId: REQUEST });
    assert.ok(Object.isFrozen(args));
    return { ...receipt(), hiddenResponse: 'PRIVATE_RESPONSE_CONTENT' };
  });
  assert.deepEqual(result, receipt());
  assert.equal(JSON.stringify(result).includes('PRIVATE_RESPONSE_CONTENT'), false);
});

test('receipt rejects mismatched binding, unverified saves and invalid local file metadata', async () => {
  for (const update of [
    { kind: 'download' }, { requestId: UI_INSTANCE }, { verified: false }, { synthetic: false },
    { filename: '' }, { filename: 'nested/file.zip' }, { filename: 'bad\\file.zip' }, { filename: 'bad\nfile.zip' },
    { filename: 'x'.repeat(256) }, { path: 'relative/合成验证.zip' }, { path: 'https://example.invalid/合成验证.zip' },
    { path: '/Users/synthetic/../合成验证.zip' }, { path: '/Users/./合成验证.zip' },
    { path: '/Users/other.zip' }, { path: '/Users/bad\n/合成验证.zip' }, { path: '/' + 'x'.repeat(8193) + '/合成验证.zip' },
    { bytes: NaN }, { bytes: Infinity }, { bytes: -1 }, { bytes: '812' }, { bytes: 1.5 },
    { bytes: Number.MAX_SAFE_INTEGER + 1 }, { sha256: 'invalid' }, { sha256: null },
  ]) await assert.rejects(make().run(async () => ({ ...receipt(), ...update })), { code: 'invalid-envelope' });
  for (const invalid of [undefined, null, false, 'PRIVATE_RESPONSE_CONTENT'])
    await assert.rejects(make().run(async () => invalid), { code: 'invalid-envelope' });
});

test('save length and digest must exactly match the activation, even when the service says verified', async () => {
  for (const update of [{ bytes: 813 }, { sha256: 'b'.repeat(64) }])
    await assert.rejects(make().run(async () => ({ ...receipt(), ...update })), { code: 'checksum-mismatch' });
  const manifest = activation();
  const attempt = new LocalExportAttempt(manifest, UI_INSTANCE, REQUEST);
  manifest.downloadExpectedBytes = 1;
  manifest.downloadExpectedSha256 = 'b'.repeat(64);
  assert.deepEqual(await attempt.run(async () => receipt()), receipt());
});

test('timeout retry and subsequent successful clicks preserve the same save request ID', async () => {
  const attempt = make();
  const calls = [];
  const call = async args => {
    calls.push(args);
    if (calls.length === 1) throw Object.assign(new Error('PRIVATE_TIMEOUT_DETAILS'), { code: 'timeout' });
    return receipt(args.requestId);
  };
  await assert.rejects(attempt.run(call), { code: 'timeout' });
  assert.deepEqual(await attempt.run(call), receipt());
  assert.deepEqual(await attempt.run(call), receipt());
  assert.equal(calls.length, 3);
  assert.ok(calls.every(args => args === attempt.request && args.requestId === REQUEST));
});

test('export timeout → same-task identity recheck → retry keeps the original save key', async () => {
  const selection = new LocalExportBinding();
  const task = 'abcdefab-cdef-4abc-8def-abcdefabcdef';
  const calls = [];
  const saved = new Map();
  const save = async args => {
    calls.push(args);
    // The first call saves successfully but its response is lost at the host.
    if (!saved.has(args.requestId)) saved.set(args.requestId, receipt(args.requestId));
    if (calls.length === 1) throw Object.assign(new Error('response timed out'), { code: 'timeout' });
    return saved.get(args.requestId);
  };
  selection.select(ACTIVATION, UI_INSTANCE, task); // The first identity verification.
  const first = selection.getAttempt(activation(), UI_INSTANCE, task);
  await assert.rejects(first.run(save), { code: 'timeout' });
  // These are the same binding transitions used by input edits and verify().
  assert.equal(selection.select(ACTIVATION, UI_INSTANCE, `  ${task.toUpperCase()}  `), false);
  assert.equal(selection.select(ACTIVATION, UI_INSTANCE, task), false);
  const retry = selection.getAttempt(activation(), UI_INSTANCE, task);
  assert.equal(retry, first);
  const result = await retry.run(save);
  assert.equal(result.requestId, first.request.requestId);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].requestId, calls[1].requestId);
  assert.equal(saved.size, 1);
});

test('exporting a candidate before its first identity check still preserves the same save intent', async () => {
  const selection = new LocalExportBinding();
  const task = 'abcdefab-cdef-4abc-8def-abcdefabcdef';
  const first = selection.getAttempt(activation(), UI_INSTANCE, task);
  await assert.rejects(first.run(async () => { throw { code: 'timeout' }; }), { code: 'timeout' });
  assert.equal(selection.select(ACTIVATION, UI_INSTANCE, task.toUpperCase()), false);
  assert.equal(selection.getAttempt(activation(), UI_INSTANCE, task), first);
});

test('actual task, activation, UI instance and explicit reactivation changes replace the save binding', () => {
  const selection = new LocalExportBinding();
  const first = selection.getAttempt(activation(), UI_INSTANCE, 'task-a');
  assert.equal(selection.select(ACTIVATION, UI_INSTANCE, 'task-b'), true);
  const second = selection.getAttempt(activation(), UI_INSTANCE, 'task-b');
  assert.notEqual(second.request.requestId, first.request.requestId);
  const returned = selection.getAttempt(activation(), UI_INSTANCE, 'task-a');
  assert.notEqual(returned.request.requestId, first.request.requestId);
  const nextActivation = { ...activation(), activationId: '44444444-4444-4444-8444-444444444444' };
  const next = selection.getAttempt(nextActivation, UI_INSTANCE, 'task-a');
  assert.notEqual(next.request.requestId, returned.request.requestId);
  const nextUI = selection.getAttempt(nextActivation, '55555555-5555-4555-8555-555555555555', 'task-a');
  assert.notEqual(nextUI.request.requestId, next.request.requestId);
  selection.clear(); // Every new activation clears even if its supplied ID repeats.
  assert.notEqual(selection.getAttempt(nextActivation, '55555555-5555-4555-8555-555555555555', 'task-a').request.requestId,
    nextUI.request.requestId);
});

test('rapid duplicate clicks share the in-flight request and retry retains its identity', async () => {
  const attempt = make();
  let resolve;
  let calls = 0;
  const call = () => { calls += 1; return new Promise(done => { resolve = done; }); };
  const first = attempt.run(call);
  const second = attempt.run(call);
  assert.equal(first, second);
  assert.equal(calls, 1);
  resolve(receipt());
  assert.deepEqual(await first, receipt());
  assert.deepEqual(await second, receipt());
  assert.deepEqual(await attempt.run(async args => receipt(args.requestId)), receipt());
});

test('a selection change refuses an old receipt and a new selection creates an independent request', async () => {
  const guard = new SelectionGuard();
  const initial = guard.select('synthetic-thread-a', ACTIVATION);
  const oldAttempt = make();
  let resolveOld;
  const oldCall = oldAttempt.run(() => new Promise(resolve => { resolveOld = resolve; }), () => guard.accepts(initial));
  const current = guard.select('synthetic-thread-b');
  const newAttempt = new LocalExportAttempt(activation(), UI_INSTANCE);
  assert.notEqual(newAttempt.request.requestId, oldAttempt.request.requestId);
  const newResult = await newAttempt.run(async args => receipt(args.requestId), () => guard.accepts(current));
  resolveOld(receipt());
  await assert.rejects(oldCall, { code: 'cancelled' });
  assert.equal(newResult.requestId, newAttempt.request.requestId);
  let staleCalls = 0;
  await assert.rejects(oldAttempt.run(() => { staleCalls += 1; }, () => guard.accepts(initial)), { code: 'cancelled' });
  assert.equal(staleCalls, 0);
});

test('reactivation invalidates a pending export even when the selected task is unchanged', async () => {
  const guard = new SelectionGuard();
  const binding = guard.select('synthetic-thread-a', ACTIVATION);
  let resolve;
  const pending = make().run(() => new Promise(done => { resolve = done; }), () => guard.accepts(binding));
  guard.select('synthetic-thread-a', '44444444-4444-4444-8444-444444444444');
  resolve(receipt());
  await assert.rejects(pending, { code: 'cancelled' });
});

test('a rejected old RPC is never allowed to surface an error in a newer selection', async () => {
  const guard = new SelectionGuard();
  const binding = guard.select('synthetic-thread-a', ACTIVATION);
  let reject;
  const pending = make().run(() => new Promise((_, fail) => { reject = fail; }), () => guard.accepts(binding));
  guard.select('synthetic-thread-b');
  reject(Object.assign(new Error('PRIVATE_ERROR'), { code: 'rpc-error' }));
  await assert.rejects(pending, error => {
    // The component uses this same guard before committing both errors and receipts.
    assert.equal(guard.accepts(binding), false);
    assert.equal(exportFailure(error).message.includes('PRIVATE_ERROR'), false);
    return true;
  });
});

test('uncertain export feedback never claims the file was not saved or leaks RPC details', () => {
  const timeout = exportFailure(Object.assign(new Error('PRIVATE_ERROR'), { code: 'timeout' }));
  assert.equal(timeout.code, 'timeout');
  assert.match(timeout.message, /超时.*结果未确认.*重试核对同一次导出/);
  assert.doesNotMatch(timeout.message, /失败|未保存|未提交|PRIVATE_ERROR/);
  for (const error of [new Error('PRIVATE_ERROR'), 'PRIVATE_ERROR', null,
    { code: 'SECRET_CODE', message: 'PRIVATE_ERROR' }, { code: 'rpc-error', message: 'PRIVATE_ERROR' },
    { code: 'isError', message: 'PRIVATE_ERROR' }, { code: 'checksum-mismatch', message: 'PRIVATE_ERROR' },
    { code: 'invalid-envelope', message: 'PRIVATE_ERROR' }]) {
    const result = exportFailure(error);
    assert.match(result.message, /结果未确认.*重试核对同一次导出/);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE_ERROR|SECRET_CODE/);
  }
  assert.match(exportFailure({ code: 'invalid-manifest' }).message, /尚未提交/);
});
