import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { isExportReceiptPath, isLocalOutputPath } from '../../frontend/src/bridge/local-path.mjs';
import { validateExportResult } from '../../frontend/src/state/export.ts';

const cases = JSON.parse(readFileSync(new URL('../fixtures/contracts/export-path-cases.json', import.meta.url)));
const receipt = JSON.parse(readFileSync(new URL('../fixtures/contracts/export-save-verified.json', import.meta.url)));

test('schema fixtures and frontend accept canonical POSIX and Windows receipts with Chinese and spaces', () => {
  for (const path of cases.validPaths) {
    assert.equal(isExportReceiptPath(path, cases.filename), true, path);
    assert.equal(isLocalOutputPath(path, cases.filename), true, path);
    const result = structuredClone(receipt);
    result.filename = result.save.filename = cases.filename;
    result.save.path = path;
    const intent = { createRequestId: result.createRequestId, commitRequestId: result.save.requestId, exportId: result.exportId };
    assert.equal(validateExportResult(result, result.binding, intent), result, path);
  }
});

test('receipt syntax rejects aliases, devices, streams, network paths and traversal before showing success', () => {
  for (const path of cases.invalidPaths) {
    assert.equal(isExportReceiptPath(path, cases.filename), false, path);
    const result = structuredClone(receipt);
    result.filename = result.save.filename = cases.filename;
    result.save.path = path;
    const intent = { createRequestId: result.createRequestId, commitRequestId: result.save.requestId, exportId: result.exportId };
    assert.throws(() => validateExportResult(result, result.binding, intent), undefined, path);
  }
  for (const path of [null, undefined, {}, 42]) assert.equal(isExportReceiptPath(path, cases.filename), false);
  assert.equal(isExportReceiptPath(cases.validPaths[0], 'other.zip'), false);
  assert.equal(isLocalOutputPath('C:\\Users\\张 三\\Local exports\\合成验证.zip', '合成验证.zip'), true);
});
