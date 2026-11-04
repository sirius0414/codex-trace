import test from 'node:test';
import assert from 'node:assert/strict';
import { detectReader, readableComplete, searchPreview, previewParts } from '../../frontend/src/components/reader/preview-model.ts';
import { DetailReadWindows, readDetailWindow } from '../../frontend/src/state/detail.ts';
import { mergeDetail } from '../../frontend/src/state/session.ts';

const part = (text, extra = {}) => ({text,nextCursor:null,completeness:'unknown',sourceTruncated:false,...extra});
test('specific source formats win while text carriers allow strong body syntax', () => {
  assert.equal(detectReader('# title', {format:'text',origin:'test'}), 'markdown');
  assert.equal(detectReader('# title', {format:'code',language:'python',origin:'test'}), 'code');
  assert.equal(detectReader('# title', {format:'json',origin:'test'}), 'json');
  assert.equal(detectReader('{"code":"hello"}'), 'json');
  assert.equal(detectReader('const result = call()'), 'code');
  assert.equal(detectReader('# Title\n\n| a | b |\n|---|---|'), 'markdown');
  assert.equal(detectReader('2026-10-01 INFO Ready\n    continued'), 'text');
  assert.equal(detectReader('*** Begin Patch\n*** End Patch'), 'diff');
});
test('command output and decoded strings use body syntax without inferring from field names', () => {
  const output = '---\r\nname: research\r\ndescription: Read primary sources.\r\n---\r\n# Research\r\n\r\n## Workflow\r\n\r\n1. Read\r\n2. Verify\r\n';
  assert.equal(detectReader(output, {format:'text',origin:'commandExecution.aggregatedOutput'}), 'markdown');
  assert.equal(detectReader(output), 'markdown');
  assert.equal(detectReader('def run(name):\n    return name.upper()', {format:'text',origin:'tool.output'}), 'code');
  assert.equal(detectReader('const input = 1;', {format:'markdown',origin:'source.mime'}), 'markdown');
  assert.equal(detectReader('{item: true}', {format:'text',language:'python',origin:'schema.language'}), 'code');
  for (const text of ['[INFO] Loaded\n[WARN] Retry', '2026-11-04 10:00:00 INFO Ready\n    continued', 'code: the field contains ordinary prose', 'git status is the command to inspect changes.']) assert.equal(detectReader(text, {format:'text',origin:'tool.output'}), 'text', text);
});
test('readable completeness never implies source completeness and does not hide missing pages', () => {
  assert.equal(readableComplete(part('{}', {nextCursor:'more'})), false);
  assert.equal(readableComplete(part('{}', {readableBytes:30})), false);
  assert.equal(readableComplete(part('中', {readableBytes:3,sourceTruncated:true})), true);
  assert.equal(readableComplete(undefined), false);
});
test('entry search covers loaded input/output/errors only and preserves source offsets', () => {
  const result = searchPreview({input:part('İ abc [x]'), output:part('ABC'), error:part('abc'), raw:part('abc')}, 'abc');
  assert.equal(result.total, 3);
  assert.equal(result.hits[0].start, 2);
  assert.equal(searchPreview({input:part('[x]')}, '[x]').total, 1);
  assert.equal(searchPreview({input:part('Aa Aa Aa')}, 'aa', 1).total, 3);
  assert.equal(searchPreview({input:part('Aa Aa Aa')}, 'aa', 1).hits.length, 1);
});
test('result entries lead with results, calls lead with input, errors remain available', () => {
  const event = {kind:'toolResult',toolName:'unknown',content:[{part:'input'},{part:'error'}]};
  assert.deepEqual(previewParts(event, {}), ['output','input','error']);
  assert.deepEqual(previewParts({...event,kind:'toolCall'}, {}), ['input','output','error']);
});
test('remembered UTF-8 windows restore after switching without storing stale cursors or source', async () => {
  const windows = new DetailReadWindows(); windows.remember('a','中文'); windows.remember('b','other');
  const base = {eventId:'a',generation:1,eventRevision:2,chunks:[]};
  const chunks = [part('中',{part:'input',byteOffset:0,nextCursor:'next',readableBytes:6}),part('文',{part:'input',byteOffset:3,readableBytes:6,reader:{format:'text',origin:'fixture'}})];
  const result = await readDetailWindow(async cursor => ({...base,chunks:[chunks[cursor ? 1 : 0]]}), windows.get('a'));
  assert.equal(result.chunks[0].text,'中文');
  const merged = mergeDetail(null,result,false);
  assert.equal(merged.parts.input.readableBytes,6); assert.equal(merged.parts.input.reader.origin,'fixture');
  windows.clear(); assert.equal(windows.get('a'),0);
});
