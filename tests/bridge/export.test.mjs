import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { ExportCoordinator, exportScopeMessage, validateExportResult } from '../../frontend/src/state/export.ts';

const readyFixture=JSON.parse(readFileSync(new URL('../fixtures/contracts/export-ready.json',import.meta.url)));
const verifiedFixture=JSON.parse(readFileSync(new URL('../fixtures/contracts/export-save-verified.json',import.meta.url)));
const binding=readyFixture.binding;
function memoryStorage(){const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),values};}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
async function flush(){for(let i=0;i<40;i++)await Promise.resolve();}
function harness(handler,options={}) {
  const storage=options.storage??memoryStorage(),calls=[],feedback=[],timers=new Map(),identities=new Map();
  let time=0,sequence=0,timerId=0,active=0,maxActive=0;
  const reply=(request,overrides={})=>{
    const thread=request.binding.threadId;
    let identity=identities.get(thread);
    if(request.action==='create'){
      if(!identity||identity.create!==request.requestId){identity={create:request.requestId,exportId:`export-${request.requestId}`,commit:null};identities.set(thread,identity);}
    }
    if(request.action==='commit')identity.commit=request.requestId;
    const result={...structuredClone(readyFixture),binding:request.binding,createRequestId:identity?.create,exportId:identity?.exportId,
      filename:`codex-trace-${thread}-frozen.zip`,manifest:{...structuredClone(readyFixture.manifest),rootThreadId:thread}};
    if(request.action==='commit'||request.action==='result')result.save={...verifiedFixture.save,requestId:request.requestId,filename:result.filename,
      path:`/Users/synthetic/Downloads/Codex Trace/${result.filename}`};
    if(request.action==='release'){
      result.status='released';
      if(identity.commit)result.save={status:'pending',requestId:identity.commit};
    }
    return {...result,...overrides};
  };
  const manager=new ExportCoordinator({...options,storage,onChange:value=>feedback.push(value),now:()=>time,randomId:()=>`request-${String(++sequence).padStart(8,'0')}`,
    setTimer:(callback,delay)=>{const id=++timerId;timers.set(id,{at:time+delay,callback});return id;},clearTimer:id=>timers.delete(id),
    call:async(request,signal)=>{calls.push(request);active++;maxActive=Math.max(maxActive,active);try{return await handler(request,{reply,signal,calls,feedback,identities});}finally{active--;}}});
  manager.bind(binding);
  return {manager,calls,feedback,storage,identities,reply,timers,get maxActive(){return maxActive;},get latest(){return feedback.at(-1);},
    async advance(ms=0){time+=ms;for(const[id,timer]of [...timers])if(timer.at<=time){timers.delete(id);timer.callback();}await flush();}};
}

test('one click creates and commits distinct stable keys, validates the saved receipt and persists only IDs/phase',async()=>{
  const h=harness((request,{reply})=>reply(request));
  const first=h.manager.run(),second=h.manager.run();assert.equal(first,second);
  await first;await flush();
  assert.deepEqual(h.calls.map(call=>call.action),['create','commit','release']);
  assert.notEqual(h.calls[0].requestId,h.calls[1].requestId);assert.equal(h.maxActive,1);
  assert.equal(h.latest.busy,false);assert.match(h.latest.message,/本地导出已校验/);assert.match(h.latest.message,/来源范围不完整/);assert.match(h.latest.message,/Downloads\/Codex Trace/);
  const stored=JSON.parse([...h.storage.values.values()][0]);
  assert.deepEqual(Object.keys(stored[0]).sort(),['commitRequestId','createRequestId','exportId','phase','sourceInstanceId','threadId'].sort());
  assert.equal(stored[0].phase,'verified');assert.doesNotMatch(JSON.stringify(stored),/manifest|sha256|Downloads|\.zip|unknown-source/);
  h.manager.dispose();
});

test('collecting and pending are polled serially; ready is never reported as saved',async()=>{
  const h=harness((request,{reply})=>reply(request,request.action==='create'?{status:'collecting',filename:null,totalBytes:null}
    :request.action==='commit'?{save:{status:'pending',requestId:request.requestId}}:{}));
  const running=h.manager.run();await flush();assert.deepEqual(h.calls.map(call=>call.action),['create']);assert.doesNotMatch(h.latest.message,/已校验/);
  await h.advance(1000);assert.deepEqual(h.calls.map(call=>call.action),['create','status','commit']);assert.doesNotMatch(h.latest.message,/已校验/);
  await h.advance(1000);await running;await flush();assert.deepEqual(h.calls.map(call=>call.action),['create','status','commit','result','release']);
  assert.equal(h.maxActive,1);assert.match(h.latest.message,/本地导出已校验/);h.manager.dispose();
});

test('lost create response repeats the same create key and does not collect a second snapshot',async()=>{
  let once=true;const h=harness((request,{reply})=>{const result=reply(request);if(request.action==='create'&&once){once=false;throw {code:'timeout'};}return result;});
  const running=h.manager.run();await flush();assert.equal(h.calls.length,1);
  await h.advance(1000);await running;await flush();
  const creates=h.calls.filter(call=>call.action==='create');assert.equal(creates.length,2);assert.equal(creates[0].requestId,creates[1].requestId);
  assert.equal(h.calls.filter(call=>call.action==='commit').length,1);assert.match(h.latest.message,/本地导出已校验/);h.manager.dispose();
});

test('a timed-out commit is recovered by result, never a new commit or create',async()=>{
  let resultCount=0;const h=harness((request,{reply})=>{
    const result=reply(request);if(request.action==='commit')throw {code:'timeout'};
    if(request.action==='result'&&resultCount++===0)return {...result,save:{status:'pending',requestId:request.requestId}};
    return result;
  });
  const running=h.manager.run();await flush();assert.match(h.latest.message,/未返回确定结果/);
  await h.advance(1000);await h.advance(1000);await running;await flush();
  const commit=h.calls.find(call=>call.action==='commit'),results=h.calls.filter(call=>call.action==='result');
  assert.equal(h.calls.filter(call=>call.action==='commit').length,1);assert.equal(results.length,2);assert.ok(results.every(call=>call.requestId===commit.requestId));
  assert.match(h.latest.message,/已校验/);h.manager.dispose();
});

test('unknown stops automatic polling; a deliberate retry can commit the same candidate without changing keys',async()=>{
  const h=harness((request,{reply})=>{
    const result=reply(request);if(request.action==='commit')throw {code:'timeout'};
    if(request.action==='result')return {...result,status:'unknown',exportId:null,createRequestId:null,filename:null,totalBytes:null,manifest:null,
      save:{status:'unknown',requestId:request.requestId,reason:'requestNotRecorded'}};
    return result;
  });
  const running=h.manager.run();await flush();await h.advance(1000);await running;
  assert.equal(h.latest.busy,false);assert.match(h.latest.message,/文件可能已保存/);
  const saved=[...h.storage.values.values()][0];const retry=h.manager.run();await flush();await h.advance(1000);await retry;
  assert.equal(h.calls.filter(call=>call.action==='create').length,1);assert.equal(h.calls.filter(call=>call.action==='commit').length,2);
  const commits=h.calls.filter(call=>call.action==='commit');assert.equal(commits[0].requestId,commits[1].requestId);assert.equal(commits[0].exportId,commits[1].exportId);
  const lookups=h.calls.filter(call=>call.action==='result');assert.equal(lookups.length,2);assert.equal(lookups[0].requestId,lookups[1].requestId);
  assert.equal([...h.storage.values.values()][0],saved);h.manager.dispose();
});

test('an explicit same-key recovery returning unknown performs one result lookup before stopping',async()=>{
  const h=harness((request,{reply})=>{
    const result=reply(request);
    return request.action==='commit'?{...result,status:'unknown',exportId:null,createRequestId:null,filename:null,totalBytes:null,manifest:null,
      save:{status:'unknown',requestId:request.requestId,reason:'verificationUnavailable'}}:result;
  });
  await h.manager.run();await flush();assert.equal(h.latest.busy,false);
  assert.deepEqual(h.calls.map(call=>call.action),['create','commit']);
  const retry=h.manager.run();await flush();await h.advance(1000);await retry;await flush();
  assert.deepEqual(h.calls.map(call=>call.action),['create','commit','commit','result','release']);
  assert.equal(h.calls[1].requestId,h.calls[2].requestId);assert.equal(h.calls[2].requestId,h.calls[3].requestId);
  assert.match(h.latest.message,/本地导出已校验/);h.manager.dispose();
});

test('a queued precommit release wins before a returning task can dispatch commit',async()=>{
  const released=deferred();let holdRelease=true;
  const h=harness((request,{reply})=>{
    if(request.action==='create')h.manager.setVisible(false);
    if(request.action==='release'&&holdRelease){holdRelease=false;return released.promise;}
    return reply(request);
  });
  const first=h.manager.run();await flush();assert.equal(h.calls.length,1);
  h.manager.bind({...binding,threadId:'thread-b',selectionEpoch:2});await flush();
  const release=h.calls.find(call=>call.action==='release');assert.ok(release);
  h.manager.bind({...binding,selectionEpoch:3});const retry=h.manager.run();h.manager.setVisible(true);await flush();
  assert.equal(h.calls.length,2);
  released.resolve(h.reply(release));await first;await retry;await flush();
  assert.equal(h.calls.filter(call=>call.action==='commit').length,0);assert.equal(h.maxActive,1);
  assert.match(h.latest.message,/快照已释放/);assert.equal(h.latest.busy,false);
  assert.equal(JSON.parse([...h.storage.values.values()][0])[0].phase,'released');h.manager.dispose();
});

test('service-confirmed expiry before commit acceptance allows a new deliberate export',async()=>{
  let expired=true;const h=harness((request,{reply})=>{
    if(request.action==='commit'&&expired)throw {code:'exportExpired'};
    return reply(request);
  });
  await h.manager.run();await flush();assert.match(h.latest.message,/快照已释放/);assert.equal(h.latest.busy,false);
  assert.equal(h.calls.filter(call=>call.action==='result').length,0);
  expired=false;await h.manager.run();await flush();
  const creates=h.calls.filter(call=>call.action==='create');assert.equal(creates.length,2);assert.notEqual(creates[0].requestId,creates[1].requestId);
  assert.match(h.latest.message,/本地导出已校验/);h.manager.dispose();
});

test('hiding after create cannot auto-commit until the page is visible again',async()=>{
  const pending=deferred();const h=harness((request,{reply})=>request.action==='create'?pending.promise:reply(request));
  const running=h.manager.run();await flush();h.manager.setVisible(false);
  pending.resolve(h.reply(h.calls[0]));await flush();await h.advance(5000);assert.deepEqual(h.calls.map(call=>call.action),['create']);
  h.manager.setVisible(true);await running;await flush();assert.equal(h.calls.filter(call=>call.action==='commit').length,1);assert.match(h.latest.message,/已校验/);h.manager.dispose();
});

test('A to B to A preserves an uncertain commit key and discards late old-binding receipts',async()=>{
  const pending=deferred();let hold=true;
  const h=harness((request,{reply})=>{const result=reply(request);if(request.action==='commit'&&request.binding.threadId===binding.threadId&&hold){hold=false;return pending.promise;}return result;});
  const a=h.manager.run();await flush();const commitA=h.calls.find(call=>call.action==='commit');
  const bBinding={...binding,threadId:'thread-b',selectionEpoch:2};h.manager.bind(bBinding);const b=h.manager.run();await flush();
  assert.equal(h.calls.filter(call=>call.binding.threadId==='thread-b').length,0);
  const oldFeedback=h.feedback.length;pending.resolve(h.reply(commitA));await a;await b;await flush();
  assert.equal(h.maxActive,1);assert.match(h.latest.message,/thread-b-frozen/);
  assert.ok(h.feedback.slice(oldFeedback).filter(item=>item.message.includes('本地导出已校验')).every(item=>item.message.includes('thread-b-frozen')));
  h.manager.bind({...binding,selectionEpoch:3});await h.manager.run();await flush();
  const aCalls=h.calls.filter(call=>call.binding.threadId===binding.threadId);
  assert.equal(aCalls.filter(call=>call.action==='commit').length,1);
  const recovered=aCalls.find(call=>call.action==='result');assert.equal(recovered.requestId,commitA.requestId);assert.equal(recovered.binding.selectionEpoch,3);
  assert.match(h.latest.message,/thread-a-frozen/);h.manager.dispose();
});

test('persisted pending intent recovers across a new coordinator using only result under the new binding',async()=>{
  const storage=memoryStorage(),pending=deferred();const h=harness((request,{reply})=>{
    const value=reply(request);if(request.action==='commit')return pending.promise;return value;
  },{storage});
  const running=h.manager.run();await flush();const commit=h.calls.find(call=>call.action==='commit');
  h.manager.dispose();pending.resolve(h.reply(commit));await running;await flush();
  const calls=[],messages=[];
  const reopened={...binding,viewSessionId:'reopened',selectionEpoch:4};
  const next=new ExportCoordinator({storage,onChange:value=>messages.push(value),call:async request=>{calls.push(request);return h.reply(request);}});
  next.bind(reopened);await next.run();await flush();
  assert.equal(calls[0].action,'result');assert.equal(calls[0].requestId,commit.requestId);assert.deepEqual(calls[0].binding,reopened);
  assert.ok(!calls.some(call=>call.action==='create'||call.action==='commit'));assert.match(messages.at(-1).message,/已校验/);next.dispose();
});

test('bounded polling retains the same handle and request IDs for deliberate continuation',async()=>{
  const h=harness((request,{reply})=>reply(request,{status:'collecting',filename:null,totalBytes:null}),{pollLimit:3});
  const run=h.manager.run();await flush();await h.advance(1000);await h.advance(1000);await h.advance(1000);await run;
  assert.equal(h.calls.length,3);assert.equal(h.latest.busy,false);assert.match(h.latest.message,/时限/);
  const persisted=[...h.storage.values.values()][0];const again=h.manager.run();await flush();
  assert.equal(h.calls[3].action,'status');assert.equal([...h.storage.values.values()][0],persisted);
  h.manager.dispose();await again;
});

test('a verified or definitive failed operation permits a new explicit export with new keys',async()=>{
  let fail=true;const h=harness((request,{reply})=>reply(request,request.action==='commit'&&fail?{save:{status:'failed',requestId:request.requestId,code:'permissionDenied'}}:{}));
  await h.manager.run();await flush();assert.match(h.latest.message,/未发布最终文件/);
  fail=false;await h.manager.run();await flush();assert.match(h.latest.message,/已校验/);
  await h.manager.run();await flush();
  const creates=h.calls.filter(call=>call.action==='create');assert.equal(creates.length,3);assert.equal(new Set(creates.map(call=>call.requestId)).size,3);
  assert.equal(h.manager.known.size,1,'a new same-task snapshot retires its old terminal metadata');h.manager.dispose();
});

test('128 completed task exports retire terminal intent metadata to admit a new task',async()=>{
  const h=harness((request,{reply})=>reply(request));
  for(let index=0;index<128;index++){
    h.manager.bind({...binding,threadId:`completed-${index}`,selectionEpoch:index+1});
    await h.manager.run();await flush();
    assert.match(h.latest.message,/本地导出已校验/);
  }
  assert.equal(h.manager.known.size,128);
  h.manager.bind({...binding,threadId:'new-task',selectionEpoch:129});await h.manager.run();await flush();
  assert.match(h.latest.message,/本地导出已校验/);assert.equal(h.manager.known.size,128);
  const stored=JSON.parse([...h.storage.values.values()][0]);assert.equal(stored.length,128);
  assert.ok(stored.some(intent=>intent.threadId==='new-task'));assert.ok(!stored.some(intent=>intent.threadId==='completed-0'));
  assert.equal(h.calls.filter(call=>call.action==='create').length,129);h.manager.dispose();
});

test('128 uncertain intents are never evicted or replaced to admit a new task',async()=>{
  const storage=memoryStorage(),intents=Array.from({length:128},(_,index)=>({sourceInstanceId:binding.sourceInstanceId,
    threadId:`uncertain-${index}`,createRequestId:`original-create-${index}`,commitRequestId:`original-commit-${index}`,
    exportId:`original-export-${index}`,phase:'saveUnknown'}));
  const encoded=JSON.stringify(intents);storage.setItem('codex-trace.export-intents.v1',encoded);
  const h=harness((request,{reply})=>reply(request),{storage});await h.manager.run();await flush();
  assert.equal(h.latest.busy,false);assert.match(h.latest.message,/待核对导出意图数量已达上限/);
  assert.equal(h.calls.length,0);assert.equal([...storage.values.values()][0],encoded);h.manager.dispose();
});

test('invalid private receipt metadata never produces success and keeps the commit key for lookup',async()=>{
  let invalid=true;const h=harness((request,{reply})=>request.action==='commit'&&invalid?reply(request,{save:{...reply(request).save,bytes:99}}):reply(request));
  await h.manager.run();await flush();assert.doesNotMatch(h.latest.message,/已校验/);assert.match(h.latest.message,/无法核对/);
  invalid=false;await h.manager.run();await flush();const commits=h.calls.filter(call=>call.action==='commit');assert.equal(commits.length,2);assert.equal(commits[0].requestId,commits[1].requestId);assert.match(h.latest.message,/已校验/);h.manager.dispose();
});

test('result notRequested cannot turn an uncertain commit into a new automatic save',async()=>{
  const h=harness((request,{reply})=>{const result=reply(request);if(request.action==='commit')throw {code:'timeout'};
    return request.action==='result'?{...result,save:{status:'notRequested',requestId:null}}:result;});
  const run=h.manager.run();await flush();await h.advance(1000);await run;
  assert.match(h.latest.message,/无法核对/);assert.equal(h.calls.filter(call=>call.action==='commit').length,1);h.manager.dispose();
});

test('receipt validation checks identity, request, filename, size, path, digest and frozen metadata',()=>{
  const good=structuredClone(verifiedFixture),intent={...binding,createRequestId:good.createRequestId,commitRequestId:good.save.requestId,exportId:good.exportId,phase:'committing'};
  assert.equal(validateExportResult(good,binding,intent),good);
  const mutations=[value=>value.binding.selectionEpoch++,value=>value.createRequestId='wrong-key',value=>value.exportId='wrong-export',
    value=>value.save.requestId='wrong-key',value=>value.save.filename='codex-trace-other.zip',value=>value.save.bytes++,
    value=>value.save.sha256='wrong',value=>value.save.verifiedAt=-1,value=>value.save.path='/tmp/'+value.save.filename,
    value=>value.save.path='/Users/../Downloads/Codex Trace/'+value.save.filename,
    value=>value.manifest.rootThreadId='other-task',value=>value.manifest.atomicSnapshot=true];
  for(const mutate of mutations){const value=structuredClone(good);mutate(value);assert.throws(()=>validateExportResult(value,binding,intent));}
  assert.throws(()=>validateExportResult(good,binding,intent,{filename:'codex-trace-other.zip',totalBytes:good.totalBytes,manifest:null}));
  assert.throws(()=>validateExportResult(good,binding,intent,{filename:good.filename,totalBytes:good.totalBytes+1,manifest:null}));
  assert.match(exportScopeMessage(null),/不能确认来源完整性/);
});

test('storage denial is explicit and does not break same-page idempotent recovery',async()=>{
  const storage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');}};
  const h=harness((request,{reply})=>reply(request),{storage});await h.manager.run();await flush();
  assert.match(h.latest.message,/无法持久保存恢复标识/);assert.match(h.latest.message,/已校验/);h.manager.dispose();
});

test('verified reports retain bilingual coverage and receipt details when revisiting a task',async()=>{
  const h=harness((request,{reply})=>reply(request));await h.manager.run();await flush();
  const report=h.latest;
  assert.match(report.message,/本地导出已校验/);
  assert.match(report.messageEn,/Local export verified/);assert.match(report.messageEn,/Source coverage is incomplete/);
  assert.match(report.messageEn,/not a simultaneous snapshot across files/);
  assert.match(report.messageEn,/Downloads\/Codex Trace/);assert.match(report.messageEn,/SHA-256 [a-f0-9]{64}/);
  h.manager.bind({...binding,threadId:'other-task',selectionEpoch:2});
  h.manager.bind({...binding,selectionEpoch:3});
  assert.equal(h.latest.messageEn,report.messageEn);assert.equal(h.latest.message,report.message);
  assert.equal(h.calls.filter(call=>call.action==='create').length,1);
  assert.equal(h.calls.filter(call=>call.action==='commit').length,1);
  for(const feedback of h.feedback)assert.doesNotMatch(feedback.messageEn,/[\u3400-\u9fff]/);
  h.manager.dispose();
});

test('English uncertainty and storage feedback preserve unconfirmed-save semantics',async()=>{
  const storage={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');}};
  const h=harness((request,{reply})=>request.action==='commit'?{...reply(request),status:'unknown',exportId:null,
    createRequestId:null,filename:null,totalBytes:null,manifest:null,
    save:{status:'unknown',requestId:request.requestId,reason:'verificationUnavailable'}}:reply(request),{storage});
  await h.manager.run();await flush();
  assert.match(h.latest.messageEn,/file may already be saved/);assert.match(h.latest.messageEn,/same request/);
  assert.match(h.latest.messageEn,/cannot persist recovery identifiers/);assert.doesNotMatch(h.latest.messageEn,/Local export verified/);
  assert.equal(h.latest.busy,false);assert.deepEqual(h.calls.map(call=>call.action),['create','commit']);
  assert.match(exportScopeMessage(null,'en'),/completeness cannot be confirmed/);
  h.manager.dispose();
});
