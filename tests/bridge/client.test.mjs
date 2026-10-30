import assert from 'node:assert/strict';
import test from 'node:test';
import { TrajectoryClient, readPrivateResult } from '../../frontend/src/bridge/client.ts';

const open = { viewSessionId: 'view', sourceInstanceId: 'source', identity: { state: 'verified', threadId: 'a', selectionEpoch: 1 }, uiResourceUri: 'ui://codex-trace/trajectory-v1.html' };

test('production opening is private and waits for shared handshake before followup', async () => {
  let release, calls = 0, handshakes = 0, read;
  const handshake = new Promise(resolve => { release=resolve; });
  const client = new TrajectoryClient(value => { assert.deepEqual(value,open); read=client.call('read_trajectory',{binding:{threadId:'a'}}); }, () => {}, () => {});
  client.app.connect=async (_,options) => { assert.equal(options.timeout,12000); ++handshakes; await handshake; };
  client.app.getHostContext=()=>({theme:'dark'});
  client.app.callServerTool=async (request,options) => { ++calls; assert.equal(options.maxTotalTimeout,12000); assert.equal(options.resetTimeoutOnProgress,false); assert.ok(options.signal instanceof AbortSignal); return {content:[],_meta:{'codex-trace/result':{ok:true}}}; };
  const connecting=client.connect(); assert.equal(client.connect(),connecting);
  client.app.ontoolresult({content:[],_meta:{'codex-trace/result':open}});
  await Promise.resolve(); assert.equal(calls,0);
  release(); await connecting; assert.deepEqual(await read,{ok:true});
  assert.equal(handshakes,1);assert.equal(calls,1);
});

test('production private envelope never falls back to model-visible content', () => {
  for(const response of [{structuredContent:open}, {content:[{type:'text',text:'SECRET'}]}, {_meta:{'codex-trace/result':[]}}]) {
    assert.throws(()=>readPrivateResult(response),error=>error.code==='invalid-envelope'&&!error.message.includes('SECRET'));
  }
  assert.throws(()=>readPrivateResult({isError:true,_meta:{'codex-trace/result':{code:'staleEpoch',message:'任务选择已变化，旧请求已拒绝。'}}}),error=>error.code==='staleEpoch');
});

test('regular private responses do not reactivate the app', () => {
  let activations=0;
  const client=new TrajectoryClient(()=>++activations,()=>{},()=>{});
  client.app.ontoolresult({content:[],_meta:{'codex-trace/result':{binding:{threadId:'b'},events:[]}}});
  assert.equal(activations,0);
});

test('selection cancellation aborts the SDK request and a pre-cancelled call never dispatches', async () => {
  const client=new TrajectoryClient(()=>{},()=>{},()=>{}),controller=new AbortController();
  let received,dispatches=0;
  client.app.connect=async()=>{};client.app.getHostContext=()=>({});
  client.app.callServerTool=async(_request,options)=>{dispatches++;received=options.signal;return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('cancelled')),{once:true}));};
  const reading=client.call('read_changes',{}, {signal:controller.signal});
  for(let i=0;i<10;i++) await Promise.resolve();
  controller.abort();await assert.rejects(reading);assert.equal(received.aborted,true);
  await assert.rejects(client.call('read_event',{}, {signal:controller.signal}));assert.equal(dispatches,1);
});


test('unsolicited ordinary RPC errors cannot bypass the caller selection guard', () => {
  const errors=[];
  const client=new TrajectoryClient(()=>{},()=>{},error=>errors.push(error));
  const result={content:[],isError:true,_meta:{'codex-trace/result':{code:'staleEpoch',message:'旧请求'}}};
  client.app.ontoolresult(result);assert.equal(errors.length,0);
  client.app.ontoolresult({...result,_meta:{...result._meta,ui:{resourceUri:'ui://codex-trace/trajectory-v1.html'}}});
  assert.equal(errors.length,1);assert.equal(errors[0].code,'staleEpoch');
});
