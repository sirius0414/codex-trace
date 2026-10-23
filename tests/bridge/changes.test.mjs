import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { ChangeAssembly, ChangeReader, mergeChanges } from '../../frontend/src/state/changes.ts';
import { mergeSnapshot } from '../../frontend/src/state/session.ts';
import { readDetailWindow } from '../../frontend/src/state/detail.ts';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
function snapshot() { return structuredClone(fixture); }
function batch(state, extra={}) {
  const {binding,generation,revision,turns,groups,relationships,status,mainUsage,statistics,coverage,freshness,timelineOrigin}=state;
  return {binding,generation,revision,baseRevision:revision,turns,groups,relationships,status,mainUsage,statistics,coverage,freshness,timelineOrigin,
    resetRequired:false,resetReason:null,continuationCursor:null,upsertEvents:[],removeEventIds:[],...extra};
}
function deferred() { let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject}; }
async function settle() { for(let i=0;i<12;i++) await Promise.resolve(); }
function setup(read) {
  let time=0,id=0,active=true,state=snapshot(),confirmed=state.revision;
  const timers=new Map(),commits=[],failures=[],received=[],resets=[];
  const reader=new ChangeReader({capture:()=>({snapshot:state,afterRevision:confirmed,isCurrent:()=>active}),read,
    commit:(value,timing)=>{commits.push(value);state=mergeChanges(state,value);confirmed=value.revision;},
    reset:async value=>{resets.push(value);state={...state,generation:value.generation,revision:value.revision};confirmed=value.revision;},
    failed:value=>failures.push(value),received:value=>received.push(value),
    now:()=>time,setTimer:(callback,delay)=>{const key=++id;timers.set(key,{at:time+delay,callback});return key;},clearTimer:key=>timers.delete(key)});
  return {reader,commits,failures,received,resets,timers,get state(){return state;},set state(value){state=value;},
    invalidate:()=>{active=false;},
    async advance(ms=0){time+=ms;for(const [key,timer] of [...timers]) if(timer.at<=time){timers.delete(key);timer.callback();}await settle();},
    get nextDelay(){return Math.min(...[...timers.values()].map(timer=>timer.at-time));}};
}

test('a continuation batch freezes revision and applies once after its final page',()=>{
  const state=snapshot(), request={binding:state.binding,generation:state.generation,afterRevision:state.revision,continuationCursor:null};
  const assembly=new ChangeAssembly(request),first=batch(state,{revision:state.revision+1,continuationCursor:'next',upsertEvents:[state.events[0]]});
  assert.equal(assembly.append(first),'more');assert.throws(()=>assembly.finish());
  assert.equal(assembly.append({...first,continuationCursor:null,upsertEvents:[state.events[0],state.events[1]]}),'complete');
  assert.equal(assembly.finish().upsertEvents.length,2);
  for(const invalid of [{...first,baseRevision:999},{...first,generation:999},{...first,binding:{...state.binding,selectionEpoch:999}},
    {...first,revision:first.revision+1},{...first,continuationCursor:'next'}]) assert.throws(()=>assembly.append(invalid));
});

test('atomic changes update original rows, remove once, preserve latest metadata and resist stale history resurrection',()=>{
  const state=snapshot(),event=state.events[0],removed=state.events[1];
  const changed={...event,eventRevision:event.eventRevision+1,preview:'new output'};
  const delta=batch(state,{revision:state.revision+1,upsertEvents:[changed],removeEventIds:[removed.eventId]});
  const merged=mergeChanges(state,delta);
  assert.equal(merged.events.find(row=>row.eventId===event.eventId).preview,'new output');
  assert.ok(!merged.events.some(row=>row.eventId===removed.eventId));
  assert.deepEqual(mergeChanges(merged,delta),merged);
  const paged=mergeSnapshot(merged,state,new Map([[removed.eventId,delta.revision]]));
  assert.ok(!paged.events.some(row=>row.eventId===removed.eventId));
  assert.equal(paged.events.find(row=>row.eventId===event.eventId).preview,'new output');
  const newerHistory={...state,revision:state.revision+5,events:[]};
  const behind=mergeChanges(newerHistory,delta);
  assert.equal(behind.revision,newerHistory.revision);assert.equal(behind.events[0].preview,'new output');
  assert.throws(()=>mergeChanges(state,{...delta,generation:state.generation+1}));
});

test('root creation origin applies with its atomic revision and old pages or changes cannot replace it',()=>{
  const known=snapshot().timelineOrigin;
  const missing={timestampMs:null,basis:'notRecorded',precision:'notRecorded',explanation:'Missing.'};
  const state={...snapshot(),timelineOrigin:missing};
  const latest=mergeChanges(state,batch(state,{revision:state.revision+1,timelineOrigin:known}));
  assert.deepEqual(latest.timelineOrigin,known);
  assert.deepEqual(mergeChanges(latest,batch(state)).timelineOrigin,known);
  assert.deepEqual(mergeSnapshot(latest,state).timelineOrigin,known);
});

test('foreground scheduling is anchored to start and never overlaps a slow read',async()=>{
  const pending=deferred(),calls=[];
  const harness=setup(async request=>{calls.push(request);return calls.length===1?pending.promise:batch(harness.state);});
  harness.reader.start();await harness.advance();assert.equal(calls.length,1);
  await harness.advance(900);assert.equal(calls.length,1);assert.equal(harness.timers.size,0);
  pending.resolve(batch(harness.state));await settle();assert.equal(harness.nextDelay,0);
  await harness.advance();assert.equal(calls.length,2);assert.equal(harness.nextDelay,750);
  await harness.advance(749);assert.equal(calls.length,2);
  await harness.advance(1);assert.equal(calls.length,3);harness.reader.dispose();
});

test('continuation failure retries unacknowledged base and never exposes a partial batch',async()=>{
  const calls=[],first=snapshot();let fail=true;
  const harness=setup(async request=>{
    calls.push({...request});
    if(!request.continuationCursor) return batch(first,{revision:first.revision+1,continuationCursor:'page-2',upsertEvents:[{...first.events[0],eventRevision:9}]});
    if(fail){fail=false;throw new Error('offline');}
    return batch(first,{revision:first.revision+1,upsertEvents:[{...first.events[1],eventRevision:9}]});
  });
  harness.reader.start();await harness.advance();assert.equal(harness.commits.length,0);assert.equal(harness.failures.length,1);assert.equal(harness.nextDelay,1000);
  await harness.advance(1000);assert.equal(harness.commits.length,1);assert.equal(harness.commits[0].upsertEvents.length,2);
  assert.ok(calls.every(request=>request.afterRevision===first.revision));
  harness.reader.dispose();
});

test('backoff is 1, 2, 5, 10 seconds, retains state and clears on recovery',async()=>{
  let fail=true;const harness=setup(async()=>{if(fail)throw new Error('offline');return batch(harness.state);});
  const original=harness.state;
  harness.reader.start();await harness.advance();
  for(const delay of [1000,2000,5000,10000,10000]) {assert.equal(harness.nextDelay,delay);assert.equal(harness.state,original);await harness.advance(delay);}
  fail=false;await harness.advance(10000);assert.equal(harness.commits.length,1);assert.equal(harness.nextDelay,750);harness.reader.dispose();
});

test('hidden pauses and aborts, visibility rereads immediately without accepting late data',async()=>{
  const pending=deferred(),calls=[];let oldSignal;
  const harness=setup(async(request,signal)=>{calls.push(request);oldSignal??=signal;return calls.length===1?pending.promise:batch(harness.state);});
  harness.reader.start();await harness.advance();harness.reader.setVisible(false);assert.equal(oldSignal.aborted,true);
  await harness.advance(10000);assert.equal(calls.length,1);
  harness.reader.setVisible(true);await harness.advance();assert.equal(calls.length,1);
  pending.resolve(batch(harness.state,{revision:999}));await settle();assert.equal(harness.commits.length,0);assert.equal(harness.nextDelay,0);
  await harness.advance();assert.equal(calls.length,2);assert.equal(harness.commits.length,1);
  harness.reader.dispose();assert.equal(harness.timers.size,0);
});

test('stale activation and disposal ignore late results and never start another read',async()=>{
  const pending=deferred();const harness=setup(()=>pending.promise);
  harness.reader.start();await harness.advance();harness.invalidate();harness.reader.dispose();
  pending.resolve(batch(harness.state,{revision:999}));await settle();
  assert.equal(harness.commits.length,0);assert.equal(harness.failures.length,0);assert.equal(harness.timers.size,0);
});

test('reset rebuild is serialized before the next read and paging revision does not advance confirmation',async()=>{
  const calls=[];const harness=setup(async request=>{calls.push(request);return calls.length===1
    ?batch(harness.state,{resetRequired:true,resetReason:'generationChanged',generation:harness.state.generation+1,revision:3})
    :batch(harness.state);});
  harness.state={...harness.state,revision:harness.state.revision+20};
  harness.reader.start();await harness.advance();assert.equal(calls[0].afterRevision,fixture.revision);
  assert.equal(harness.resets.length,1);assert.equal(harness.commits.length,0);
  await harness.advance(750);assert.equal(calls[1].generation,fixture.generation+1);assert.equal(calls[1].afterRevision,3);harness.reader.dispose();
});

test('detail refresh fills the previously loaded UTF8 byte window before replacing text',async()=>{
  const base={eventId:'event',generation:2,eventRevision:5,chunks:[]},calls=[];
  const read=async cursor=>{calls.push(cursor);return {...base,chunks:[{part:'output',text:cursor?'new tail':'中文',byteOffset:cursor?6:0,nextCursor:cursor?null:'second',completeness:'fragment'}]};};
  const refreshed=await readDetailWindow(read,10);
  assert.deepEqual(calls,[null,'second']);assert.equal(refreshed.chunks[0].text,'中文new tail');assert.equal(refreshed.chunks[0].byteOffset,0);
  await assert.rejects(()=>readDetailWindow(async cursor=>({...base,eventRevision:cursor?6:5,chunks:[{part:'output',text:'x',byteOffset:cursor?1:0,nextCursor:'next'}]}),2));
});
