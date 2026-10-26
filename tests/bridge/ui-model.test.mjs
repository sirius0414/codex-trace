import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { makeRows, virtualWindow, visibleEvents, latestUsage, metric, overviewFrame, overviewMarks, recordedEnd, relativeTime, relationshipEndpoints, readingAnchor, selectedAgentIds } from '../../frontend/src/components/model.ts';

const base = JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json', import.meta.url)));
// Each test adds its own explicit children, including empty/unloaded groups.
base.groups = base.groups.filter(group => group.groupId === base.rootGroupId);
const viewBase = JSON.parse(readFileSync(new URL('../fixtures/contracts/view-state.json', import.meta.url)));
const view = { ...viewBase, filters: { text: '', kinds: [], toolName: null, failedOnly: false, timeRange: null },
  selectedEventId: null, agentScope: { kind: 'all', groupId: null }, expandedTurnIds: ['turn-1', 'child-turn'], expandedGroupIds: [] };
function event(id, options = {}) {
  return { ...structuredClone(base.events[0]), eventId: id, turnId: 'turn-1', groupId: base.rootGroupId,
    kind: 'toolCall', toolName: 'syntheticTool', preview: id, timing: { ...base.events[0].timing, startMs: 100, endMs: 200 }, ...options };
}
function snapshot(events, options = {}) { return { ...structuredClone(base), events, coverage: { ...base.coverage, hasEarlier: false }, ...options }; }

test('10,000-row virtual window renders bounded viewport with stable offsets', () => {
  const data = snapshot(Array.from({ length: 10000 }, (_, i) => event(`e-${i}`)));
  const rows = makeRows(data, view, data.events);
  const window = virtualWindow(rows, 140000, 300);
  assert.ok(window.end - window.start < 24);
  assert.equal(window.totalHeight, 23 + 10000 * 28);
  assert.equal(rows[100].top, 23 + 99 * 28);
  assert.equal(virtualWindow([], 0, 300).totalHeight, 0);
});

test('child records appear at creation location and remain collapsed until expanded', () => {
  const create = event('create', { kind: 'collaboration' });
  const child = event('child-event', { threadId: 'child', groupId: 'child-group', turnId: 'child-turn' });
  const group = { ...base.groups[0], kind: 'subtask', groupId: 'child-group', threadId: 'child',
    name: 'Synthetic child', parentGroupId: base.rootGroupId, creationEventId: 'create', loadedEventCount: 1, loadedToolCallCount: 1, readIssue: 'Read interrupted' };
  const data = snapshot([event('before'), create, child, event('after')], { groups: [...base.groups, group] });
  const collapsed = makeRows(data, view, data.events);
  assert.equal(collapsed.filter(row => row.type === 'group').length, 1);
  assert.ok(!collapsed.some(row => row.type === 'event' && row.event.eventId === 'child-event'));
  assert.ok(collapsed.some(row => row.type === 'warning'));
  const expanded = makeRows(data, { ...view, expandedGroupIds: ['child-group'] }, data.events);
  const ids = expanded.filter(row => row.type === 'event').map(row => row.event.eventId);
  assert.deepEqual(ids, ['before', 'child-event', 'after']);
  assert.equal(new Set(expanded.map(row => row.key)).size, expanded.length);
});

test('child scope retains creation group while main scope excludes child rows', () => {
  const create = event('create', { kind: 'collaboration' });
  const child = event('child', { groupId: 'g-child', threadId: 'child', turnId: 'child-turn' });
  const group = { ...base.groups[0], groupId: 'g-child', kind: 'subtask', parentGroupId: base.rootGroupId, creationEventId: 'create' };
  const data = snapshot([create, child], { groups: [...base.groups, group] });
  const childView = { ...view, agentScope: { kind: 'group', groupId: 'g-child' }, expandedGroupIds: ['g-child'] };
  assert.deepEqual(visibleEvents(data, childView, null).map(row => row.eventId), ['child']);
  const rows = makeRows(data, childView, visibleEvents(data, childView, null));
  assert.ok(rows.some(row => row.type === 'group'));
  assert.ok(rows.some(row => row.type === 'event' && row.event.eventId === 'child'));
  assert.deepEqual(visibleEvents(data, { ...view, agentScope: { kind: 'main', groupId: null } }, null).map(row => row.eventId), ['create']);
});

test('Agent buttons select exact groups, including none, while legacy scopes retain their meaning', () => {
  const groups = [base.groups[0],
    { ...base.groups[0], groupId: 'child', parentGroupId: base.rootGroupId, creationEventId: 'create-child', kind: 'subtask' },
    { ...base.groups[0], groupId: 'nested', parentGroupId: 'child', creationEventId: 'create-nested', kind: 'subtask' },
    { ...base.groups[0], groupId: 'sibling', parentGroupId: base.rootGroupId, creationEventId: 'create-sibling', kind: 'subtask' }];
  const data = snapshot([event('main'), event('child-event', { groupId: 'child' }), event('nested-event', { groupId: 'nested' }), event('sibling-event', { groupId: 'sibling' })], { groups });
  const selected = { ...view, agentScope: { kind: 'selected', groupIds: [base.rootGroupId, 'child'] } };
  assert.deepEqual(visibleEvents(data, selected, null).map(item => item.eventId), ['main', 'child-event']);
  assert.deepEqual([...selectedAgentIds(data, selected)], [base.rootGroupId, 'child']);
  const none = { ...selected, agentScope: { kind: 'selected', groupIds: [] } };
  assert.deepEqual(visibleEvents(data, none, null), []);
  assert.deepEqual(makeRows({ ...data, coverage: { ...data.coverage, hasEarlier: true } }, none, []), []);
  assert.deepEqual([...selectedAgentIds(data, view)], groups.map(group => group.groupId));
  assert.deepEqual([...selectedAgentIds(data, { ...view, agentScope: { kind: 'main', groupId: null } })], [base.rootGroupId]);
  assert.deepEqual([...selectedAgentIds(data, { ...view, agentScope: { kind: 'group', groupId: 'child' } })], ['child', 'nested']);
});

test('selected descendants render independently of hidden or collapsed ancestors without their headers', () => {
  const groups = [base.groups[0],
    { ...base.groups[0], groupId: 'child', parentGroupId: base.rootGroupId, creationEventId: 'create-child', kind: 'subtask' },
    { ...base.groups[0], groupId: 'nested', parentGroupId: 'child', creationEventId: 'create-nested', kind: 'subtask' }];
  const data = snapshot([event('create-child'), event('create-nested', { groupId: 'child', turnId: 'child-turn' }),
    event('nested-event', { groupId: 'nested', turnId: 'nested-turn' })], { groups });
  for (const groupIds of [['nested'], [base.rootGroupId, 'nested'], ['child', 'nested']]) {
    const selected = { ...view, agentScope: { kind: 'selected', groupIds }, expandedTurnIds: ['nested-turn'], expandedGroupIds: ['nested'] };
    const rows = makeRows(data, selected, visibleEvents(data, selected, null));
    assert.deepEqual(rows.filter(row => row.type === 'event').map(row => row.event.eventId), ['nested-event']);
    assert.deepEqual(rows.filter(row => row.type === 'group').map(row => row.group.groupId), groupIds.filter(id => id !== base.rootGroupId));
    assert.deepEqual(rows.filter(row => row.type === 'turn').map(row => row.id), groupIds.includes(base.rootGroupId) ? ['turn-1', 'nested-turn'] : ['nested-turn']);
    assert.equal(new Set(rows.map(row => row.key)).size, rows.length);
  }
});

test('selected Agent rows preserve creation events, per-Agent paging and active filters', () => {
  const group = { ...base.groups[0], groupId: 'child', parentGroupId: base.rootGroupId, creationEventId: 'create', kind: 'subtask', hasEarlier: true, nextCursor: 'child-page' };
  const data = snapshot([event('create', { kind: 'collaboration' }), event('child-event', { groupId: 'child', turnId: 'child-turn' })],
    { groups: [...base.groups, group], coverage: { ...base.coverage, hasEarlier: true } });
  const selected = { ...view, agentScope: { kind: 'selected', groupIds: [base.rootGroupId, 'child'] }, expandedGroupIds: ['child'] };
  const rows = makeRows(data, selected, visibleEvents(data, selected, null));
  assert.deepEqual(rows.filter(row => row.type === 'event').map(row => row.event.eventId), ['create', 'child-event']);
  assert.deepEqual(rows.filter(row => row.type === 'earlier').map(row => row.group?.groupId), [undefined, 'child']);
  const filtered = { ...selected, filters: { ...selected.filters, text: 'only the child body' } };
  const matched = visibleEvents(data, filtered, new Set(['child-event']));
  assert.deepEqual(matched.map(item => item.eventId), ['child-event']);
  const filteredRows = makeRows(data, filtered, matched);
  assert.deepEqual(filteredRows.filter(row => row.type === 'event').map(row => row.event.eventId), ['child-event']);
  assert.deepEqual(filteredRows.filter(row => row.type === 'group').map(row => row.group.groupId), ['child']);
  assert.ok(!makeRows(data, filtered, []).some(row => row.type === 'group'));
});

test('server matched IDs support body matches absent from previews', () => {
  const data = snapshot([event('one'), event('two')]);
  assert.deepEqual(visibleEvents(data, { ...view, filters: { ...view.filters, text: 'only in complete body' } }, new Set(['two'])).map(row => row.eventId), ['two']);
  assert.equal(visibleEvents(data, view, new Set()).length, 0);
});

test('time filters keep overlapping intervals and exclude unrecorded starts', () => {
  const data = snapshot([event('overlap'), event('untimed', { timing: { ...base.events[0].timing, startMs: null, endMs: null } }), event('later', { timing: { ...base.events[0].timing, startMs: 300, endMs: 400 } })]);
  const filtered = { ...view, filters: { ...view.filters, timeRange: { startMs: 150, endMs: 250 } } };
  assert.deepEqual(visibleEvents(data, filtered, null).map(item => item.eventId), ['overlap']);
});

test('usage chooses one latest scope and preserves true zero versus missing', () => {
  const usage = { ...base.mainUsage[0], scope: 'thread', order: 1, input: { availability: 'recorded', value: 100 } };
  assert.equal(latestUsage([usage, { ...usage, order: 2, input: { availability: 'recorded', value: 150 } }]).input.value, 150);
  assert.equal(metric({ availability: 'recorded', value: 0 }), '0');
  assert.equal(metric({ availability: 'notRecorded', value: null }), '—');
  assert.equal(latestUsage([]), null);
});

test('a confirmed child stays accessible when its creation event is outside the loaded page', () => {
  const child = event('child', { groupId: 'g-child', threadId: 'child', turnId: 'child-turn' });
  const group = { ...base.groups[0], groupId: 'g-child', kind: 'subtask', parentGroupId: base.rootGroupId, creationEventId: 'not-loaded' };
  const data = snapshot([child], { groups: [...base.groups, group] });
  const rows = makeRows(data, { ...view, expandedGroupIds: ['g-child'] }, data.events);
  assert.ok(rows.some(row => row.type === 'group'));
  assert.ok(rows.some(row => row.type === 'event' && row.event.eventId === 'child'));
});

test('an unloaded confirmed child remains expandable outside the root page without inventing matching records', () => {
  const group={...base.groups[0],groupId:'unread-child',kind:'subtask',parentGroupId:base.rootGroupId,creationEventId:'not-loaded',loadedEventCount:0};
  const data=snapshot([event('root')],{groups:[...base.groups,group]});
  assert.ok(makeRows(data,view,data.events).some(row=>row.type==='group'&&row.group.groupId===group.groupId));
  const filtered={...view,filters:{...view.filters,text:'no-match'}};
  assert.ok(!makeRows(data,filtered,[]).some(row=>row.type==='group'));
  const mainOnly={...view,agentScope:{kind:'main',groupId:null}};
  assert.ok(!makeRows(data,mainOnly,data.events).some(row=>row.type==='group'));
});

test('each expanded child has its own earlier row and cursor while the root entry stays independent', () => {
  const group={...base.groups[0],groupId:'paged-child',kind:'subtask',name:'Paged child',parentGroupId:base.rootGroupId,creationEventId:'create',hasEarlier:true,nextCursor:'child-next'};
  const data=snapshot([event('create'),event('child',{groupId:group.groupId,turnId:'child-turn'})],{groups:[...base.groups,group],coverage:{...base.coverage,hasEarlier:true,nextCursor:'root-next'}});
  const collapsed=makeRows(data,view,data.events).filter(row=>row.type==='earlier');
  assert.equal(collapsed.length,1);assert.equal(collapsed[0].group,undefined);
  const expanded=makeRows(data,{...view,expandedGroupIds:[group.groupId]},data.events);
  const earlier=expanded.filter(row=>row.type==='earlier');
  assert.equal(earlier.length,2);assert.equal(earlier[1].group.groupId,group.groupId);assert.equal(earlier[1].group.nextCursor,'child-next');
  assert.ok(earlier[1].top<expanded.find(row=>row.type==='event'&&row.event.eventId==='child').top);
  group.hasEarlier=false;assert.equal(makeRows(data,{...view,expandedGroupIds:[group.groupId]},data.events).filter(row=>row.type==='earlier').length,1);
});

test('source relations show child-to-parent direction and never call a communication target a child',()=>{
  assert.deepEqual(relationshipEndpoints({type:'spawned-by',fromThreadId:'child',toThreadId:'parent'}),[['父任务','parent'],['子任务','child']]);
  assert.deepEqual(relationshipEndpoints({type:'forked-from',fromThreadId:'fork',toThreadId:'origin'}),[['来源任务','origin'],['分叉任务','fork']]);
  assert.deepEqual(relationshipEndpoints({type:'communicates-with',fromThreadId:'sender',toThreadId:'receiver'}),[['发起任务','sender'],['通信对象','receiver']]);
});

test('reading anchor captures exact child group and signed offset above its first visible event',()=>{
  const child=event('child-anchor',{groupId:'child-group'});
  const rows=[{key:'earlier',type:'earlier',top:0,height:23},{key:'child',type:'event',top:23,height:28,event:child,child:true}];
  assert.deepEqual(readingAnchor(rows,0),{eventId:'child-anchor',groupId:'child-group',offsetPx:-23});
  assert.deepEqual(readingAnchor(rows,28),{eventId:'child-anchor',groupId:'child-group',offsetPx:5});
  assert.equal(readingAnchor([],0),null);
});

test('10,000 overview records form at most 120 counted clusters plus the selected event', () => {
  const events = Array.from({ length: 10000 }, (_, index) => event(`e-${index}`, {
    kind: ['userMessage', 'assistantMessage', 'toolCall'][index % 3],
    timing: { ...base.events[0].timing, startMs: index, endMs: index + 1, durationMs: 1, precision: 'exact' },
  }));
  const marks = overviewMarks(events, { min: 0, max: 10000, mode: 'time', selectedEventId: 'e-5000' });
  assert.ok(marks.length <= 121);
  assert.equal(marks.flatMap(mark => mark.events).length, 10000);
  assert.equal(new Set(marks.flatMap(mark => mark.events.map(event => event.eventId))).size, 10000);
  const selected = marks.find(mark => mark.selected);
  assert.deepEqual(selected.events.map(event => event.eventId), ['e-5000']);
  assert.equal(marks.at(-1).key, selected.key);
  assert.ok(marks.some(mark => mark.events.length > 20));
  for (const mark of marks) {
    const pages = Array.from({ length: Math.ceil(mark.events.length / 20) }, (_, page) => mark.events.slice(page * 20, page * 20 + 20));
    assert.ok(pages.every(page => page.length <= 20));
    assert.deepEqual(pages.flat(), mark.events);
  }
});

test('overview window clipping includes overlapping derived durations and excludes outside starts', () => {
  const data = [
    event('overlap', { timing: { ...base.events[0].timing, startMs: 10, endMs: null, durationMs: 100, precision: 'exact' } }),
    event('before', { timing: { ...base.events[0].timing, startMs: 10, endMs: null, durationMs: null, precision: 'startOnly' } }),
    event('after', { timing: { ...base.events[0].timing, startMs: 1000, endMs: null, durationMs: null, precision: 'startOnly' } }),
  ];
  const marks = overviewMarks(data, { min: 50, max: 100, mode: 'time', selectedEventId: null });
  assert.deepEqual(marks.flatMap(mark => mark.events.map(item => item.eventId)), ['overlap']);
  assert.equal(marks[0].end, 110);
  assert.deepEqual(visibleEvents(snapshot(data), { ...view, filters: { ...view.filters, timeRange: { startMs: 50, endMs: 100 } } }, null).map(item => item.eventId), ['overlap']);
});

test('start plus exact recorded duration is a range; unconfirmed duration remains a point', () => {
  const timing = { ...base.events[0].timing, startMs: 100, endMs: null, durationMs: 12.5, precision: 'exact' };
  assert.equal(recordedEnd(event('exact', { timing })), 112.5);
  assert.equal(recordedEnd(event('zero', { timing: { ...timing, durationMs: 0 } })), 100);
  assert.equal(recordedEnd(event('unknown', { timing: { ...timing, precision: 'startOnly' } })), 100);
  assert.equal(recordedEnd(event('negative', { timing: { ...timing, durationMs: -1 } })), 100);
  assert.equal(recordedEnd(event('missing-start', { timing: { ...timing, startMs: null } })), null);
});

test('untimed overview uses explicit record positions without inventing timestamps or durations', () => {
  const data = Array.from({ length: 10000 }, (_, index) => event(`ordered-${index}`, {
    timing: { ...base.events[0].timing, startMs: null, endMs: null, durationMs: null, precision: 'orderOnly' },
  }));
  const marks = overviewMarks(data, { min: 1, max: 10000, mode: 'order', selectedEventId: 'ordered-33' });
  assert.ok(marks.length <= 41);
  assert.equal(marks.flatMap(mark => mark.events).length, 10000);
  const selected = marks.find(mark => mark.selected);
  assert.equal(selected.start, 34);
  assert.equal(selected.end, 34);
  assert.equal(selected.events[0].timing.startMs, null);
  const zoomed = overviewMarks(data, { min: 100, max: 150, mode: 'order', selectedEventId: null });
  assert.equal(zoomed.flatMap(mark => mark.events).length, 51);
});

test('active-time overview uses projected positions while preserving source records and wall-time filters', () => {
  const first = event('run-1', { timing: { ...base.events[0].timing, startMs: 10000, endMs: 20000, durationMs: 10000, precision: 'exact' } });
  const second = event('run-2', { turnId: 'turn-2', timing: { ...base.events[0].timing, startMs: 36070000, endMs: 36080000, durationMs: 10000, precision: 'exact' } });
  const positions = new Map([['run-1', { start: 10000, end: 20000 }], ['run-2', { start: 70000, end: 80000 }]]);
  const marks = overviewMarks([first, second], { min: 60000, max: 120000, mode: 'time', selectedEventId: 'run-2', positions });
  assert.equal(marks.length, 1);
  assert.equal(marks[0].start, 70000); assert.equal(marks[0].end, 80000);
  assert.strictEqual(marks[0].events[0], second);
  assert.equal(second.timing.startMs, 36070000);
  const data = snapshot([first, second]);
  const filtered = { ...view, filters: { ...view.filters, timeRange: { startMs: 36070000, endMs: 36080000 } } };
  assert.deepEqual(visibleEvents(data, filtered, null).map(item => item.eventId), ['run-2']);
  assert.deepEqual(overviewMarks([first], { min: 0, max: 120000, mode: 'time', selectedEventId: null, positions: new Map() }), []);
});

test('all Agent scopes and older pages keep the recorded root creation origin including real zero', () => {
  const origin={timestampMs:10000,basis:'threadCreatedAt',precision:'second',explanation:'Synthetic creation reference.'};
  const recent=event('recent',{timing:{...base.events[0].timing,startMs:50000,endMs:51000}});
  const child=event('child',{groupId:'child',timing:{...base.events[0].timing,startMs:30000,endMs:31000}});
  const earlier=event('earlier',{timing:{...base.events[0].timing,startMs:20000,endMs:21000}});
  const data=snapshot([recent,child],{timelineOrigin:origin});
  for(const rows of [[recent],[child],[recent,child],[earlier,recent,child]]) {
    const frame=overviewFrame(data,rows);
    assert.equal(frame.origin,10000);assert.equal(frame.min,10000);
    assert.match(frame.explanation,/thread\/read.createdAt/);assert.match(frame.explanation,/不是本次执行/);
  }
  assert.equal(overviewFrame({...data,timelineOrigin:{...origin,timestampMs:0}},[child]).origin,0);
});

test('missing root creation never borrows a child or loaded-root start and keeps absolute-time filtering', () => {
  const missing={timestampMs:null,basis:'notRecorded',precision:'notRecorded',explanation:'Missing.'};
  const child=event('child',{groupId:'child',timing:{...base.events[0].timing,startMs:30000,endMs:31000}});
  const data=snapshot([child],{timelineOrigin:missing});
  const frame=overviewFrame(data,[child]);
  assert.equal(frame.origin,null);assert.equal(frame.min,30000);assert.equal(frame.ordered,false);
  assert.match(frame.explanation,/绝对时间/);assert.match(frame.explanation,/创建时间未记录/);
  const untimed={...child,timing:{...child.timing,startMs:null,endMs:null}};
  assert.equal(overviewFrame(data,[untimed]).ordered,true);
  assert.equal(overviewFrame({...data,timelineOrigin:undefined},[child]).origin,null);
  assert.deepEqual(visibleEvents(data,{...view,filters:{...view.filters,timeRange:{startMs:30500,endMs:32000}}},null).map(row=>row.eventId),['child']);
});

test('inherited times before creation stay visible with signed offsets rather than a false zero', () => {
  const inherited=event('inherited',{timing:{...base.events[0].timing,startMs:8000,endMs:9000}});
  const data=snapshot([inherited],{timelineOrigin:{timestampMs:10000,basis:'threadCreatedAt',precision:'second',explanation:'Creation.'}});
  const frame=overviewFrame(data,data.events);
  assert.equal(frame.min,8000);assert.equal(frame.max,10000);assert.equal(frame.origin,10000);
  assert.equal(relativeTime(8000-frame.origin),'−00:02');assert.equal(relativeTime(0),'00:00');
});
