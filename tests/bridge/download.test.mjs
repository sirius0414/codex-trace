import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { HostCallError, withDeadline } from '../../frontend/src/bridge/deadline.mjs';
import { DownloadError, prepareDownload, sha256 } from '../../frontend/src/bridge/download.mjs';

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const expectedFor = bytes => ({ downloadExpectedBytes: bytes.length, downloadExpectedSha256: digest(bytes) });
const content = Buffer.from([0x50, 0x4b, 0x00, 0xff, 0xfe, 0x0d, 0x0a, 0xe4, 0xb8]);

function chunksFor(bytes, size = 4) {
  const result = [];
  for (let offset = 0; offset < bytes.length; offset += size) {
    const part = bytes.subarray(offset, Math.min(offset + size, bytes.length));
    result.push({ offset, nextOffset: offset + part.length, totalBytes: bytes.length,
      data: part.toString('base64'), sha256: digest(bytes), chunkSha256: digest(part),
      done: offset + part.length === bytes.length });
  }
  return result;
}

const isCode = code => error => {
  assert.ok(error instanceof DownloadError || error instanceof HostCallError);
  assert.equal(error.code, code);
  return true;
};

test('a never-resolving RPC has a hard deadline and aborts its signal', async () => {
  let signal;
  let aborted = false;
  const start = performance.now();
  await assert.rejects(withDeadline(value => {
    signal = value;
    value.addEventListener('abort', () => { aborted = true; });
    return new Promise(() => {});
  }, 20), isCode('timeout'));
  assert.ok(performance.now() - start < 1000);
  assert.equal(signal.aborted, true);
  assert.equal(aborted, true);
});

test('late RPC completion and progress cannot extend or reverse a deadline', async () => {
  let resolveLate;
  let progress = 0;
  let signal;
  const interval = setInterval(() => { progress += 1; }, 2);
  try {
    const result = withDeadline(value => {
      signal = value;
      return new Promise(resolve => { resolveLate = resolve; });
    }, 20);
    await assert.rejects(result, isCode('timeout'));
    resolveLate({ privateResult: 'late content must not be returned' });
    await assert.rejects(result, isCode('timeout'));
    assert.ok(progress > 0);
    assert.equal(signal.aborted, true);
  } finally { clearInterval(interval); }
});

test('late RPC rejection is consumed after timeout', async () => {
  let rejectLate;
  const result = withDeadline(() => new Promise((_, reject) => { rejectLate = reject; }), 10);
  await assert.rejects(result, isCode('timeout'));
  rejectLate(new Error('secret late rejection'));
  await delay(0); // The test runner also fails on unhandled rejections.
});

test('successful RPC clears the deadline without aborting the completed operation', async () => {
  let signal;
  assert.deepEqual(await withDeadline(value => { signal = value; return { ok: true }; }, 10), { ok: true });
  await delay(20);
  assert.equal(signal.aborted, false);
});

test('protocol failures never expose original exception text or cause', async () => {
  for (const original of [new Error('PRIVATE_SOURCE_BODY'), { code: -32001, message: 'PRIVATE_SOURCE_BODY' },
    new HostCallError('private-code', 'PRIVATE_SOURCE_BODY'), 'PRIVATE_SOURCE_BODY']) {
    await assert.rejects(withDeadline(() => { throw original; }, 50), error => {
      assert.ok(error instanceof HostCallError);
      assert.equal(error.message.includes('PRIVATE_SOURCE_BODY'), false);
      assert.equal(error.code.includes('private'), false);
      assert.equal(error.cause, undefined);
      return true;
    });
  }
});

test('invalid timeout configurations fail before calling the RPC', async () => {
  for (const timeout of [0, -1, NaN, Infinity, 2147483648, '20', null]) {
    let called = false;
    await assert.rejects(withDeadline(() => { called = true; }, timeout), isCode('invalid-timeout'));
    assert.equal(called, false);
  }
});

test('multi-chunk download preserves binary bytes, validates hashes and reports phases', async () => {
  const chunks = chunksFor(content);
  const offsets = [], phases = [];
  const result = await prepareDownload(async ({ offset }) => {
    offsets.push(offset);
    return chunks.shift();
  }, expectedFor(content), phase => phases.push(phase));
  assert.deepEqual(Buffer.from(result.bytes), content);
  assert.equal(result.hash, digest(content));
  assert.equal(result.filename, 'codex-trace-m0-local-download.zip');
  assert.deepEqual(offsets, [0, 4, 8]);
  assert.deepEqual(phases, Array(3).fill(['download-start', 'chunk-received', 'chunk-verified']).flat());
});

test('maximum supported synthetic download is bounded and accepted', async () => {
  const bytes = Buffer.alloc(1024 * 1024, 0xab);
  const [chunk] = chunksFor(bytes, bytes.length);
  const result = await prepareDownload(async () => chunk, expectedFor(bytes), () => {});
  assert.deepEqual(Buffer.from(result.bytes), bytes);
});

test('zero-byte terminal download is distinct from a no-progress nonterminal block', async () => {
  const empty = Buffer.alloc(0);
  const chunk = { offset: 0, nextOffset: 0, totalBytes: 0, data: '', sha256: digest(empty), chunkSha256: digest(empty), done: true };
  const result = await prepareDownload(async () => chunk, expectedFor(empty), () => {});
  assert.equal(result.bytes.length, 0);
});

test('a nonterminal block without progress is rejected on the first response', async () => {
  let calls = 0;
  const chunk = { ...chunksFor(content)[0], data: '', nextOffset: 0, chunkSha256: digest(Buffer.alloc(0)) };
  await assert.rejects(prepareDownload(async () => { calls += 1; return chunk; }, expectedFor(content), () => {}), isCode('invalid-chunk'));
  assert.equal(calls, 1);
});

test('invalid manifest does not trigger a download RPC', async () => {
  const expected = expectedFor(content);
  for (const manifest of [null, {}, { ...expected, downloadExpectedBytes: -1 },
    { ...expected, downloadExpectedBytes: 1048577 }, { ...expected, downloadExpectedBytes: NaN },
    { ...expected, downloadExpectedSha256: 'bad-hash' }]) {
    let calls = 0;
    await assert.rejects(prepareDownload(async () => { calls += 1; }, manifest, () => {}), isCode('invalid-manifest'));
    assert.equal(calls, 0);
  }
});

test('oversized blocks and invalid numeric fields are rejected', async () => {
  const valid = chunksFor(content)[0];
  const variants = [null, { ...valid, data: 'A'.repeat(1400001) }, { ...valid, totalBytes: 1048577 },
    { ...valid, totalBytes: -1 }, { ...valid, nextOffset: -1 }, { ...valid, nextOffset: 0.5 },
    { ...valid, offset: true }, { ...valid, offset: '0' }, { ...valid, nextOffset: Infinity },
    { ...valid, totalBytes: NaN }, { ...valid, totalBytes: Number.MAX_SAFE_INTEGER + 1 },
    { ...valid, done: 'false' }, { ...valid, sha256: null }];
  for (const chunk of variants)
    await assert.rejects(prepareDownload(async () => chunk, expectedFor(content), () => {}), isCode('invalid-chunk'));
});

test('malformed and noncanonical base64 are rejected', async () => {
  const bytes = Buffer.from('a');
  const valid = chunksFor(bytes)[0];
  for (const encoded of ['@@==', 'YQ', 'YQ==\n', 'YR==', '====', 'A===', 'A']) {
    await assert.rejects(prepareDownload(async () => ({ ...valid, data: encoded }), expectedFor(bytes), () => {}), isCode('invalid-chunk'));
  }
});

test('chunk hash, final hash and final length disagreement cannot prepare a file', async () => {
  const valid = chunksFor(content, content.length)[0];
  await assert.rejects(prepareDownload(async () => ({ ...valid, chunkSha256: '0'.repeat(64) }), expectedFor(content), () => {}), isCode('checksum-mismatch'));
  const forgedHash = { ...expectedFor(content), downloadExpectedSha256: '0'.repeat(64) };
  await assert.rejects(prepareDownload(async () => ({ ...valid, sha256: forgedHash.downloadExpectedSha256 }), forgedHash, () => {}), isCode('checksum-mismatch'));
  await assert.rejects(prepareDownload(async () => ({ ...valid, nextOffset: content.length - 1 }), expectedFor(content), () => {}), isCode('invalid-chunk'));
  await assert.rejects(prepareDownload(async () => ({ ...valid, done: false }), expectedFor(content), () => {}), isCode('invalid-chunk'));
});

test('advertised file identity cannot change between byte chunks', async () => {
  for (const change of [{ sha256: '0'.repeat(64) }, { totalBytes: content.length + 1 }]) {
    const chunks = chunksFor(content);
    chunks[1] = { ...chunks[1], ...change };
    let calls = 0;
    await assert.rejects(prepareDownload(async () => { calls += 1; return chunks.shift(); }, expectedFor(content), () => {}), isCode('checksum-mismatch'));
    assert.equal(calls, 2);
  }
});

test('already changed selection prevents even the first request', async () => {
  let calls = 0;
  await assert.rejects(prepareDownload(async () => { calls += 1; }, expectedFor(content), () => {}, () => false), isCode('cancelled'));
  assert.equal(calls, 0);
});

test('selection change while the RPC is pending cancels its result', async () => {
  let current = true;
  const [chunk] = chunksFor(content, content.length);
  await assert.rejects(prepareDownload(async () => { current = false; return chunk; }, expectedFor(content), () => {}, () => current), isCode('cancelled'));
});

test('selection change after a verified chunk prevents another stale request', async () => {
  let current = true, calls = 0;
  const chunks = chunksFor(content);
  await assert.rejects(prepareDownload(async () => { calls += 1; return chunks.shift(); }, expectedFor(content),
    phase => { if (phase === 'chunk-verified') current = false; }, () => current), isCode('cancelled'));
  assert.equal(calls, 1);
});

test('selection change in the initial progress callback prevents the RPC', async () => {
  let current = true, calls = 0;
  await assert.rejects(prepareDownload(async () => { calls += 1; }, expectedFor(content),
    () => { current = false; }, () => current), isCode('cancelled'));
  assert.equal(calls, 0);
});

test('digest failures expose a local error instead of the native exception', async t => {
  t.mock.method(globalThis.crypto.subtle, 'digest', async () => { throw new Error('PRIVATE_NATIVE_ERROR'); });
  await assert.rejects(sha256(content), error => {
    assert.equal(error.code, 'digest-failed');
    assert.equal(error.message.includes('PRIVATE_NATIVE_ERROR'), false);
    assert.equal(error.cause, undefined);
    return true;
  });
});
