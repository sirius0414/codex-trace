import test from 'node:test';
import assert from 'node:assert/strict';
import { meaningfulPreviewExplanation, searchPreview } from '../../frontend/src/components/reader/preview-model.ts';
import { protocolBody } from '../../frontend/src/components/reader/protocol-body.ts';
import { parseJsonDocument } from '../../frontend/src/components/reader/json-model.ts';
import { isLongJsonValue, JSON_READING_LIMITS, revealJsonHit } from '../../frontend/src/components/reader/json-layout.ts';
test('only known boilerplate is removed, including when a real problem shares its explanation', () => {
  assert.equal(meaningfulPreviewExplanation('当前块为已读取内容。'), '');
  assert.equal(meaningfulPreviewExplanation('当前块为已读取内容。 加载失败，请重试。'), '加载失败，请重试。');
  assert.equal(meaningfulPreviewExplanation('原始关联未记录'), '原始关联未记录');
});
test('protocol body extraction requires origin, definition and supported complete shape', () => {
  const text = JSON.stringify({content:[{type:'text',text:'# Heading\n\nbody'}],isError:false});
  const metadata = {origin:'mcpToolCall.result',format:'json',fields:[{path:'/content/0/text',scope:'protocol',format:'text'}]};
  assert.equal(protocolBody(text,metadata), '# Heading\n\nbody');
  assert.equal(protocolBody(text,{...metadata,origin:'unknown'}),null);
  assert.equal(protocolBody(text,{...metadata,fields:[]}),null);
  for (const suffix of [',"custom":1}', ',"content":[]}', ',"structuredContent":{}}']) assert.equal(protocolBody(text.slice(0,-1)+suffix,metadata),null);
  assert.equal(protocolBody(text.replace('false','true'),metadata),null);
});
test('threshold, stable identities, precision and search remain independent of visual folds', () => {
  const text = '{"keep":{"s":"'+'x'.repeat(JSON_READING_LIMITS.longValue)+'target"},"id":900719925474099312345,"id":1}';
  const document = parseJsonDocument(text).document;
  assert.equal(isLongJsonValue(document,document.root),false);
  assert.equal(isLongJsonValue(document,document.root.children[0]),true);
  const hit=searchPreview({input:{text},output:{text:'target'},error:{text:'target'}},'target');
  assert.equal(hit.total,3);
  const state = revealJsonHit(document, {}, hit.hits[0]);
  const nextDocument=parseJsonDocument(text.replace('{"keep"','{"new":0,"keep"')).document;
  assert.equal(state.expanded[nextDocument.root.children[1].id],true);
  assert.equal(document.text,text);
});
