import test from 'node:test';
import assert from 'node:assert/strict';
import { detectReader, readableComplete, searchPreview, previewParts, previewCollapseDefaults } from '../../frontend/src/components/reader/preview-model.ts';
import { initialReadingState, initializePreviewState, readReadingState, saveReadingState } from '../../frontend/src/components/reader/reading-state.ts';
import { DetailReadWindows, readDetailWindow } from '../../frontend/src/state/detail.ts';
import { mergeDetail } from '../../frontend/src/state/session.ts';

const part = (text, extra = {}) => ({text,nextCursor:null,completeness:'unknown',sourceTruncated:false,...extra});
const ref = (part, readableBytes, extra = {}) => ({part,readableBytes,loaded:false,chunked:false,chunkCursor:null,completeness:'unknown',sourceTruncated:false,...extra});
const entry = (kind = 'toolCall', content = [], extra = {}) => ({eventId:'collapse-entry',kind,content,status:{value:'completed'},...extra});
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
test('object-property source language metadata falls back to plain text or Markdown', () => {
  for (const language of ['constructor', '__proto__', ' CONSTRUCTOR extra ']) {
    const metadata = {format:'text',language,origin:'schema.language'};
    assert.equal(detectReader('const answer = 42;', metadata), 'text');
    assert.equal(detectReader('```js\nconst answer = 42;\n```', metadata), 'markdown');
  }
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
test('a single effective body is expanded for calls and results, regardless of its part', () => {
  for (const kind of ['toolCall', 'toolResult']) {
    for (const name of ['input', 'output', 'error']) {
      assert.deepEqual(previewCollapseDefaults(entry(kind), {[name]:part('Only body')}), {[name]:false});
      assert.deepEqual(previewCollapseDefaults(entry(kind, [ref(name, 50)]), {}), {[name]:false});
    }
  }
  assert.deepEqual(previewCollapseDefaults(entry(), {output:part('Output'),error:part('Error')}), {output:false,error:false});
});
test('paired bodies default to input for calls and output for results, including unloaded references', () => {
  const refs = [ref('input', 40),ref('output', 80)];
  for (const loaded of [{}, {input:part('Input')}, {output:part('Output')}, {input:part('Input'),output:part('Output')}]) {
    assert.deepEqual(previewCollapseDefaults(entry('toolCall', refs), loaded), {input:false,output:true});
    assert.deepEqual(previewCollapseDefaults(entry('toolResult', refs), loaded), {output:false,input:true});
  }
  for (const kind of ['userMessage','assistantMessage','developerContext']) {
    assert.deepEqual(previewCollapseDefaults(entry(kind, refs), {}), {input:false,output:false});
  }
});
test('empty placeholders do not count, and source evidence is not lost while data loads', () => {
  const empty = part('', {readableBytes:0,completeness:'notRecorded'});
  assert.deepEqual(previewCollapseDefaults(entry('toolResult', [ref('input', 40),ref('output', 0, {completeness:'notRecorded'})]), {output:empty}), {input:false});
  assert.deepEqual(previewCollapseDefaults(entry('toolCall', [ref('input', 0),ref('output', 40)]), {input:part('')}), {output:false});
  assert.deepEqual(previewCollapseDefaults(entry(), {output:empty,raw:part('Raw'),summary:part('Summary')}), {});
  for (const input of [part('', {nextCursor:'more'}),part('', {readableBytes:40}),part(' ')]) {
    assert.deepEqual(previewCollapseDefaults(entry(), {input,output:part('Output')}), {input:false,output:true});
  }
  for (const input of [ref('input', null),ref('input', null, {chunkCursor:'more'}),ref('input', 0, {chunked:true})]) {
    assert.deepEqual(previewCollapseDefaults(entry('toolCall', [input,ref('output', 40)]), {}), {input:false,output:true});
  }
  assert.deepEqual(previewCollapseDefaults(entry('toolCall', [ref('input', null),ref('output', 40)]), {input:part('', {readableBytes:0})}), {output:false});
  assert.deepEqual(previewCollapseDefaults(entry('toolCall', [ref('input', 40),ref('output', 40)]), {input:empty}), {input:false,output:true});
});
test('failed entries initialize every existing body expanded and keep explicit user choices', () => {
  const event = entry('toolCall', [ref('input', 40),ref('output', 80),ref('error', 20)], {status:{value:'failed'}});
  assert.deepEqual(previewParts(event, {}), ['error','input','output']);
  assert.deepEqual(previewCollapseDefaults(event, {}), {error:false,input:false,output:false});
  const manual = {...initialReadingState(),query:'saved query',blocks:{error:{collapsed:true,source:true},output:{collapsed:true},input:{collapsed:false}}};
  const initialized = initializePreviewState(event, {}, manual);
  assert.equal(initialized.blocks.error.defaultCollapsed, false);
  assert.equal(initialized.blocks.error.collapsed, true);
  assert.equal(initialized.blocks.error.source, true);
  assert.equal(initialized.blocks.output.collapsed, true);
  assert.equal(initialized.blocks.input.collapsed, false);
  assert.equal(initialized.query, 'saved query');
  assert.deepEqual(manual.blocks.error, {collapsed:true,source:true});
  assert.strictEqual(initializePreviewState(event, {}, initialized), initialized);
});
test('defaults settle once per effective block, never on an unsaved output placeholder', () => {
  const outputOnly = entry('toolCall', [ref('output', 40)]);
  const first = initializePreviewState(outputOnly, {}, initialReadingState());
  assert.equal(first.blocks.output.defaultCollapsed, false);
  const paired = {...outputOnly, content:[ref('input', 40),ref('output', 80)],eventRevision:2};
  const refreshed = initializePreviewState(paired, {output:part('Output')}, first);
  assert.equal(refreshed.blocks.output.defaultCollapsed, false, 'new references do not collapse an already visible body');
  assert.strictEqual(initializePreviewState({...paired,content:[]}, {}, refreshed), refreshed);

  const pending = entry('toolCall', [ref('input', 40),ref('output', 0, {completeness:'notRecorded'})]);
  const waiting = initializePreviewState(pending, {output:part('', {completeness:'notRecorded'})}, initialReadingState());
  assert.equal(waiting.blocks.output, undefined);
  const available = initializePreviewState(paired, {output:part('New output')}, waiting);
  assert.equal(available.blocks.output.defaultCollapsed, true);
  assert.strictEqual(available.blocks.input, waiting.blocks.input);
});
test('unknown-size counterparts participate immediately but only lock folding after confirming body presence', () => {
  for (const [kind, primary, secondary] of [['toolCall','input','output'],['toolResult','output','input']]) {
    const event = entry(kind, [ref(primary, null),ref(secondary, 40)]);
    const pending = initializePreviewState(event, {}, initialReadingState());
    assert.equal(previewCollapseDefaults(event, {})[secondary], true, 'unloaded reference still participates');
    assert.equal(pending.blocks[secondary]?.defaultCollapsed, undefined, 'unknown size cannot permanently hide the sole body');
    const empty = {[primary]:part('', {readableBytes:0}),[secondary]:part('Only body')};
    const resolved = initializePreviewState(event, empty, pending);
    assert.equal(resolved.blocks[secondary].defaultCollapsed, false);
    assert.strictEqual(initializePreviewState(event, {}, resolved), resolved, 'a later detail reload cannot collapse it again');
    const present = initializePreviewState(event, {[primary]:part('Confirmed body')}, pending);
    assert.equal(present.blocks[secondary].defaultCollapsed, true);
    assert.strictEqual(initializePreviewState(event, {}, present), present);
    const manual = {...pending,blocks:{...pending.blocks,[secondary]:{collapsed:true}}};
    assert.equal(initializePreviewState(event, empty, manual).blocks[secondary].collapsed, true);
  }
});
test('switching entries, appending pages and live refresh retain defaults and manual folding independently', () => {
  const event = entry('toolCall', [ref('input', 40),ref('output', 80)]);
  const initial = initializePreviewState(event, {}, initialReadingState());
  const manual = {...initial,blocks:{...initial.blocks,input:{...initial.blocks.input,collapsed:true},output:{...initial.blocks.output,collapsed:false,source:true,sourceLimit:200000}}};
  const key = JSON.stringify(['collapse-source','session','thread',event.eventId]);
  saveReadingState(key, manual);
  const otherKey = JSON.stringify(['collapse-source','session','thread','other-entry']);
  const other = initializePreviewState(entry('toolResult', event.content), {}, readReadingState(otherKey));
  saveReadingState(otherKey, other);
  assert.equal(other.blocks.input.defaultCollapsed, true);
  assert.equal(other.blocks.input.collapsed, undefined);
  assert.strictEqual(readReadingState(key), manual);
  assert.strictEqual(initializePreviewState(event, {}, readReadingState(key)), manual);

  const response = {eventId:event.eventId,summary:'summary',explanation:'',chunks:[{...part('First page\n',{nextCursor:'next',readableBytes:80}),part:'output',byteOffset:0}]};
  const first = mergeDetail(null, response, false);
  const appended = mergeDetail(first, {...response,chunks:[{...part('Second page'),part:'output',byteOffset:11}]}, true);
  assert.equal(appended.parts.output.text, 'First page\nSecond page');
  assert.strictEqual(initializePreviewState(event, appended.parts, readReadingState(key)), manual);
  const liveEvent = {...event,eventRevision:2,status:{value:'failed'},content:[...event.content,ref('error', 20)]};
  const liveDetail = mergeDetail(appended, {...response,chunks:[{...part('Refreshed body'),part:'output',byteOffset:0}]}, false);
  const liveState = initializePreviewState(liveEvent, liveDetail.parts, readReadingState(key));
  assert.strictEqual(liveState.blocks.input, manual.blocks.input);
  assert.strictEqual(liveState.blocks.output, manual.blocks.output);
  assert.equal(liveState.blocks.error.defaultCollapsed, false);
  assert.equal(liveDetail.parts.output.text, 'Refreshed body');
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
