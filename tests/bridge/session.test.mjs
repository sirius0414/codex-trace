import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { SessionGate, PreferenceGate, defaultView, sameBinding, mergeSnapshot, mergeDetail, partsForTab, restoredView, selectedView, eventLocation, readAnchorPage, readSelectionPage, searchKeys, searchVisibility } from '../../frontend/src/state/session.ts';
import { visibleEvents, makeRows } from '../../frontend/src/components/model.ts';
import { DEFAULT_TIMELINE_SPACING } from '../../frontend/src/components/active-timeline.ts';
import { TrackingGate } from '../../frontend/src/state/tracking.ts';

const fixture=JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json',import.meta.url)));
const open={viewSessionId:'v',sourceInstanceId:'s',identity:{state:'verified',threadId:'a',selectionEpoch:1}};

test('all async channels reject old task, old epoch, old activation and wrong source',()=>{
  const gate=new SessionGate(); const a=gate.begin(open);assert.ok(gate.accepts(a));
  const b=gate.select('b');assert.ok(gate.accepts(b));assert.equal(gate.accepts(a),false);
  assert.equal(gate.accepts(b,{...b.binding,sourceInstanceId:'other'}),false);
  assert.equal(gate.accepts(b,{...b.binding,viewSessionId:'other'}),false);
  const a2=gate.select('a');assert.equal(gate.accepts(a),false);assert.ok(gate.accepts(a2));
  gate.begin({...open,viewSessionId:'v2'});assert.equal(gate.accepts(a2),false);
  gate.invalidate();assert.equal(gate.accepts(gate.capture()),false);
});

test('slow historical page adds missing events but cannot overwrite new output or snapshot revision',()=>{
  const current=structuredClone(fixture), page=structuredClone(fixture);
  current.revision=8;current.events=current.events.slice(0,1);current.events[0].eventRevision=5;current.events[0].preview='new';
  page.revision=3;page.events[0].eventRevision=1;page.events[0].preview='old';
  const merged=mergeSnapshot(current,page);
  assert.equal(merged.revision,8);assert.equal(merged.events.find(e=>e.eventId===current.events[0].eventId).preview,'new');
  assert.equal(merged.events.length,page.events.length);
  assert.equal(new Set(merged.events.map(e=>e.eventId)).size,merged.events.length);
  assert.equal(mergeSnapshot(current,{...page,generation:current.generation+1}).events[0].preview,'old');
  assert.equal(mergeSnapshot({...current,generation:5},{...page,generation:4}).events[0].preview,'new');
});

test('preference writes serialize latest state and stop after another window wins CAS',()=>{
  const prefs=new PreferenceGate();const one=defaultView(),two={...one,follow:'paused'};
  prefs.edit(one);assert.equal(prefs.take(),null);prefs.restored(4);
  assert.deepEqual(prefs.take(),{expectedPreferenceRevision:4,state:one});
  prefs.edit(two);assert.equal(prefs.take(),null);
  prefs.finish({saved:true,preferenceRevision:5});assert.equal(prefs.take().state,two);
  prefs.edit(one);prefs.finish({saved:false,preferenceRevision:6});
  assert.equal(prefs.take(),null);assert.equal(prefs.pending,null);assert.equal(prefs.blocked,true);
});

test('detail chunks append by UTF8 byte position and retry/gap cannot duplicate or erase',()=>{
  const response={eventId:'e',summary:'summary',explanation:'',chunks:[{part:'output',text:'中文',byteOffset:0,nextCursor:'next',completeness:'fragment',sourceTruncated:false}]};
  const initial=mergeDetail(null,response,false);
  const second={...response,chunks:[{...response.chunks[0],text:'!',byteOffset:6,nextCursor:null}]};
  const final=mergeDetail(initial,second,true);assert.equal(final.parts.output.text,'中文!');
  assert.equal(mergeDetail(final,second,true).parts.output.text,'中文!');
  assert.equal(mergeDetail(initial,{...second,chunks:[{...second.chunks[0],byteOffset:99}]},true).parts.output.text,'中文');
});

test('detail preview requests only recorded applicable sections and no unrelated raw data',()=>{
  assert.deepEqual(partsForTab('preview',['summary','input','output','raw','source']),['input','output']);
  assert.deepEqual(partsForTab('source',['input','raw']),['source']);
  assert.deepEqual(partsForTab('preview',['summary']),['summary']);
  assert.equal(sameBinding(null,null),false);
});


test('duplicate opening notification leaves the active selection and token valid',()=>{
  const gate=new SessionGate();gate.begin(open);const b=gate.select('b');
  assert.equal(gate.isDuplicateOpen(open),true);
  assert.ok(gate.accepts(b));assert.equal(gate.binding.threadId,'b');
  assert.equal(gate.isDuplicateOpen({...open,viewSessionId:'recreated'}),false);
  gate.begin({...open,viewSessionId:'recreated'});assert.equal(gate.accepts(b),false);
});

test('saved collapse and reading state survives restoration while first use expands turns',()=>{
  const state={...defaultView(),filters:{...defaultView().filters,text:'needle'},follow:'paused',selectedEventId:'chosen',
    anchor:{eventId:'chosen',offsetPx:9},expandedTurnIds:[],expandedGroupIds:['child'],detailTab:'raw'};
  assert.deepEqual(restoredView(state,4,fixture),state);
  assert.deepEqual(restoredView(defaultView(),0,fixture).expandedTurnIds,fixture.turns.map(turn=>turn.turnId));
});

test('timeline spacing is a persistent display preference that preserves selection and confirmed search matches',()=>{
  const original={...defaultView(),turnRange:{startMs:1790000000000,endMs:1790000001000},
    filters:{...defaultView().filters,text:'needle'},selectedEventId:'chosen'};
  const before=structuredClone(original),keys=searchKeys(original,fixture),ids=new Set(['chosen']);
  const cached={...keys,ids};
  const legacy=restoredView(JSON.parse(JSON.stringify(original)),4,fixture);
  assert.equal(legacy.timelineSpacing??DEFAULT_TIMELINE_SPACING,'runtime');
  for(const timelineSpacing of ['runtime','compact']) {
    const changed={...legacy,timelineSpacing};
    const restored=restoredView(JSON.parse(JSON.stringify(changed)),4,fixture);
    assert.equal(restored.timelineSpacing,timelineSpacing);
    assert.deepEqual(restored.turnRange,original.turnRange);
    assert.equal(restored.selectedEventId,'chosen');
    assert.deepEqual(searchKeys(restored,fixture),keys);
    assert.deepEqual(searchVisibility(restored.filters.text,searchKeys(restored,fixture),cached),{matchedIds:ids,searching:false,searchPaused:false});
    const prefs=new PreferenceGate();prefs.restored(4);prefs.edit(restored);
    assert.equal(prefs.take().state.timelineSpacing,timelineSpacing);
  }
  assert.deepEqual(original,before);
});

test('restoration and queued preference writes preserve exact, empty and legacy Agent scopes',()=>{
  for (const agentScope of [{kind:'selected',groupIds:['main','nested']},{kind:'selected',groupIds:[]},
    {kind:'all',groupId:null},{kind:'main',groupId:null},{kind:'group',groupId:'child'}]) {
    const state={...defaultView(),agentScope};
    const restored=restoredView(JSON.parse(JSON.stringify(state)),3,fixture);
    assert.deepEqual(restored.agentScope,agentScope);
    const gate=new PreferenceGate();gate.restored(3);gate.edit(restored);
    assert.deepEqual(gate.take().state.agentScope,agentScope);
  }
});

test('timeline selection opens collapsed child ancestors and parent creation turn',()=>{
  const snapshot=structuredClone(fixture);
  snapshot.events=[{eventId:'create',turnId:'parent-turn',groupId:'main'}, {eventId:'child-event',turnId:'child-turn',groupId:'child'}];
  snapshot.groups=[{groupId:'main',parentGroupId:null,creationEventId:null}, {groupId:'child',parentGroupId:'main',creationEventId:'create'}];
  const before={...defaultView(),filters:{...defaultView().filters,text:'retained'}};
  const after=selectedView(before,snapshot,'child-event');
  assert.deepEqual(new Set(after.expandedTurnIds),new Set(['parent-turn','child-turn']));
  assert.deepEqual(new Set(after.expandedGroupIds),new Set(['child','main']));
  assert.equal(after.filters.text,'retained');assert.equal(after.follow,'paused');
  assert.equal(after.selectedEventId,'child-event');assert.equal(after.detailTab,'preview');
  assert.equal(selectedView(after,snapshot,null).follow,'paused');
});

test('late pagination coverage grows without rolling back newer event state',()=>{
  const current=structuredClone(fixture),older=structuredClone(fixture);
  current.revision=9;current.coverage.scopeRevision=3;current.coverage.nextCursor='previous';
  older.revision=8;older.coverage.scopeRevision=4;older.coverage.nextCursor='earlier';
  const merged=mergeSnapshot(current,older);
  assert.equal(merged.revision,9);assert.equal(merged.coverage.scopeRevision,4);assert.equal(merged.coverage.nextCursor,'earlier');
});

test('late child pagination preserves current status and root cursor while advancing that group coverage',()=>{
  const current=structuredClone(fixture),page=structuredClone(fixture);
  current.revision=9;current.coverage.scopeRevision=3;current.coverage.nextCursor='root-page';
  current.groups[0]={...current.groups[0],hasEarlier:true,nextCursor:'child-page-one',loadedEventCount:200,status:{...current.groups[0].status,value:'running'}};
  page.revision=8;page.coverage.scopeRevision=4;page.coverage.nextCursor='root-page';
  page.groups[0]={...page.groups[0],hasEarlier:true,nextCursor:'child-page-two',loadedEventCount:400,status:{...page.groups[0].status,value:'unknown'}};
  const merged=mergeSnapshot(current,page);
  assert.equal(merged.revision,9);assert.equal(merged.coverage.nextCursor,'root-page');
  assert.equal(merged.groups[0].nextCursor,'child-page-two');assert.equal(merged.groups[0].loadedEventCount,400);
  assert.equal(merged.groups[0].status.value,'running');
  const lateCurrent=mergeSnapshot(merged,current);
  assert.equal(lateCurrent.groups[0].nextCursor,'child-page-two');assert.equal(lateCurrent.groups[0].loadedEventCount,400);
});

test('slow preference read cannot lower an acknowledged CAS revision',()=>{
  const gate=new PreferenceGate();gate.restored(3);gate.edit(defaultView());gate.take();
  gate.finish({saved:true,preferenceRevision:4});assert.equal(gate.restored(3),false);
  gate.edit({...defaultView(),follow:'paused'});assert.equal(gate.take().expectedPreferenceRevision,4);
});

test('saved child anchor routes restoration to its group and old preferences omit unknown group safely',()=>{
  const saved={...defaultView(),selectedEventId:'child-event',anchor:{eventId:'child-event',offsetPx:-23,groupId:'child-group'}};
  assert.deepEqual(eventLocation(saved,null),{anchorEventId:'child-event',groupId:'child-group'});
  assert.deepEqual(restoredView(saved,4,fixture),saved);
  const old={...saved,anchor:{eventId:'old-event',offsetPx:8}};
  assert.deepEqual(eventLocation(old,null),{anchorEventId:'old-event'});
  assert.deepEqual(eventLocation(old,null,'child-event'),{anchorEventId:'child-event'});
  const known={...fixture,events:[{eventId:'child-event',groupId:'known-child'}]};
  assert.deepEqual(eventLocation(old,known,'child-event'),{anchorEventId:'child-event',groupId:'known-child'});
  assert.deepEqual(eventLocation(defaultView(),null),{anchorEventId:null});
});

test('missing child anchor falls back to that group recent page and accepts the rebuilt generation',async()=>{
  const requests=[],rebuilt={...fixture,generation:fixture.generation+1};
  const result=await readAnchorPage(async location=>{
    requests.push(location);if(location.anchorEventId)throw {code:'notFound'};return rebuilt;
  },{anchorEventId:'removed-child-event',groupId:'child-group'});
  assert.deepEqual(requests,[{anchorEventId:'removed-child-event',groupId:'child-group'},{anchorEventId:null,groupId:'child-group'}]);
  assert.equal(result.snapshot,rebuilt);assert.equal(result.missing,true);
});

test('removed group falls back to root while non-notFound failures never become missing anchors',async()=>{
  const requests=[];
  const result=await readAnchorPage(async location=>{
    requests.push(location);if(location.groupId)throw {code:'notFound'};return fixture;
  },{anchorEventId:'removed',groupId:'removed-group'});
  assert.equal(requests.length,3);assert.deepEqual(requests[2],{anchorEventId:null});assert.equal(result.missing,true);
  for(const code of ['resourceLimit','identityRequired','timeout']) {
    let reads=0;
    await assert.rejects(readAnchorPage(async()=>{reads++;throw {code};},{anchorEventId:'unindexed',groupId:'child'}),error=>error.code===code);
    assert.equal(reads,1);
  }
});

test('selected-page notFound retains the successful rebuilt page; resource limits remain retryable',async()=>{
  const rebuilt={...fixture,generation:fixture.generation+1};
  const result=await readSelectionPage(async()=>{throw {code:'notFound'};},{anchorEventId:'missing-selected',groupId:'child'},rebuilt);
  assert.equal(result.snapshot,rebuilt);assert.equal(result.missing,true);
  await assert.rejects(readSelectionPage(async()=>{throw {code:'resourceLimit'};},{anchorEventId:'unindexed'},rebuilt),error=>error.code==='resourceLimit');
});

test('text search retains its confirmed one-row set while output or paging advances and never expands all rows',()=>{
  const data=structuredClone(fixture),template={...data.events[0],groupId:data.rootGroupId};
  data.events=Array.from({length:201},(_,index)=>({...template,eventId:`row-${index}`,preview:index===200?'M3 片段测试':'other'}));
  const view={...defaultView(),filters:{...defaultView().filters,text:'M3 片段测试'},follow:'paused',selectedEventId:'row-200',
    anchor:{eventId:'row-200',offsetPx:0,groupId:data.rootGroupId},expandedTurnIds:data.turns.map(turn=>turn.turnId)};
  const beforeView=structuredClone(view),keys=searchKeys(view,data),ids=new Set(['row-200']);
  const cached={...keys,ids};
  const before=searchVisibility(view.filters.text,keys,cached);
  assert.equal(before.searching,false);
  const advanced={...data,revision:data.revision+1,coverage:{...data.coverage,scopeRevision:data.coverage.scopeRevision+1}};
  const pending=searchVisibility(view.filters.text,searchKeys(view,advanced),cached);
  assert.equal(pending.matchedIds,ids);assert.equal(pending.searching,true);
  const initialRows=makeRows(data,view,visibleEvents(data,view,before.matchedIds));
  const pendingRows=makeRows(advanced,view,visibleEvents(advanced,view,pending.matchedIds));
  assert.deepEqual(visibleEvents(advanced,view,pending.matchedIds).map(event=>event.eventId),['row-200']);
  assert.deepEqual(pendingRows.map(row=>[row.key,row.top]),initialRows.map(row=>[row.key,row.top]));
  assert.deepEqual(view,beforeView);
  const latest={...searchKeys(view,advanced),ids:new Set(['row-199','row-200'])};
  assert.equal(searchVisibility(view.filters.text,searchKeys(view,advanced),latest).searching,false);
  assert.equal(searchVisibility(view.filters.text,searchKeys(view,advanced),latest).matchedIds.size,2);
});

test('new filters, task identities and generation wait with an empty set, while no text uses local paging',()=>{
  const data=structuredClone(fixture),view={...defaultView(),filters:{...defaultView().filters,text:'first'}};
  const keys=searchKeys(view,data),cached={...keys,ids:new Set(['previous-match'])};
  const changedFilter={...view,filters:{...view.filters,text:'different'}};
  const changes=[searchKeys(changedFilter,data),searchKeys(view,{...data,generation:data.generation+1}),
    searchKeys(view,{...data,binding:{...data.binding,selectionEpoch:data.binding.selectionEpoch+1}}),
    searchKeys(view,{...data,binding:{...data.binding,threadId:'different-task'}}),
    searchKeys(view,{...data,binding:{...data.binding,viewSessionId:'new-window'}})];
  for(const changed of changes){const result=searchVisibility('text',changed,cached);assert.ok(result.matchedIds instanceof Set);assert.equal(result.matchedIds.size,0);assert.equal(result.searching,true);}
  assert.equal(searchVisibility('text',keys,null).matchedIds.size,0);
  const noText=searchVisibility('',searchKeys(defaultView(),data),cached);
  assert.deepEqual(noText,{matchedIds:null,searching:false,searchPaused:false});
  assert.equal(visibleEvents(data,defaultView(),noText.matchedIds).length,data.events.length);
});

test('paused search distinguishes pending terms from confirmed results and never reports an undispatched query as searching',()=>{
  const data=structuredClone(fixture),view={...defaultView(),filters:{...defaultView().filters,text:'confirmed'}};
  const keys=searchKeys(view,data),ids=new Set([data.events[0].eventId]),cached={...keys,ids};
  assert.deepEqual(searchVisibility(view.filters.text,keys,cached,true),{matchedIds:ids,searching:false,searchPaused:false});
  const changed={...view,filters:{...view.filters,text:'new search'}};
  for(const previous of [cached,null]) {
    const paused=searchVisibility(changed.filters.text,searchKeys(changed,data),previous,true);
    assert.equal(paused.matchedIds.size,0);
    assert.equal(paused.searching,false);assert.equal(paused.searchPaused,true);
    assert.deepEqual(visibleEvents(data,changed,paused.matchedIds),[]);
  }
  const advanced={...data,revision:data.revision+1};
  assert.deepEqual(searchVisibility(view.filters.text,searchKeys(view,advanced),cached,true),
    {matchedIds:ids,searching:false,searchPaused:true});
  assert.deepEqual(searchVisibility('',searchKeys(defaultView(),data),cached,true),
    {matchedIds:null,searching:false,searchPaused:false});
});

test('resuming tracking moves the pending term into searching and clears the pause notice after completion',()=>{
  const data=structuredClone(fixture),view={...defaultView(),filters:{...defaultView().filters,text:'new search'}};
  const keys=searchKeys(view,data);
  const paused=searchVisibility(view.filters.text,keys,null,true);
  assert.equal(paused.searchPaused,true);assert.equal(paused.searching,false);
  const resumed=searchVisibility(view.filters.text,keys,null,false);
  assert.equal(resumed.searchPaused,false);assert.equal(resumed.searching,true);
  const ids=new Set([data.events[0].eventId]);
  assert.deepEqual(searchVisibility(view.filters.text,keys,{...keys,ids},false),
    {matchedIds:ids,searching:false,searchPaused:false});
  assert.deepEqual(data,fixture);
});

test('late search responses stay cancelled through pause and rapid resume, preserving the pending query',async()=>{
  const tracking=new TrackingGate(),selection=new AbortController();
  const view={...defaultView(),filters:{...defaultView().filters,text:'before pause'}};
  const keys=searchKeys(view,fixture);
  let resolve,cached=null;
  const response=new Promise(done=>{resolve=done;});
  const signal=AbortSignal.any([selection.signal,tracking.signal]);
  const pending=response.then(ids=>{if(!signal.aborted)cached={...keys,ids};});
  tracking.setPaused(true);
  const changed={...view,filters:{...view.filters,text:'typed while paused'}},latest=searchKeys(changed,fixture);
  assert.equal(searchVisibility(changed.filters.text,latest,cached,tracking.paused).searchPaused,true);
  tracking.setPaused(false);
  resolve(new Set(['old-match']));await pending;
  assert.equal(cached,null);
  const resumed=searchVisibility(changed.filters.text,latest,cached,tracking.paused);
  assert.equal(resumed.matchedIds.size,0);assert.equal(resumed.searching,true);assert.equal(resumed.searchPaused,false);
});
