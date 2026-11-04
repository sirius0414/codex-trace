/** 合成验收入口；挂载正式 TraceUI，分页/刷新只修改合成数据，不读取真实任务。 */
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import TraceUI from '../../../frontend/src/components/TraceUI';
import { PreferencesProvider, usePreferences } from '../../../frontend/src/preferences';
import { defaultView } from '../../../frontend/src/state/session';
import type { DetailData, DetailPart } from '../../../frontend/src/ui-types';
import type { ThreadSnapshot, TraceEvent } from '../../../contracts/types';
import fixture from '../contracts/snapshot-running.json';
import '../../../frontend/src/style.css';

const context = '# Project context\n\nReadable context with **emphasis**, a [source](https://example.com/spec), and preserved tags.\n\n<environment_context>\nworkspace: /synthetic/project\n</environment_context>\n\n## Architecture\n\n| Component | Responsibility |\n| --- | --- |\n| Reader | Preserve body |\n| Source | Keep provenance |\n\n> Every loaded byte remains available.\n\n- Input is independent\n- Output may be a log\n\n```typescript\nfunction format(value: string) {\n  return `value: ${value}`;\n}\n```\n\n' + Array.from({length:18},(_,i)=>`## Section ${i + 1}\n\n${'A long paragraph explaining evidence and reading state. '.repeat(12)}\n\n`).join('');
const complex = '{\n  "long_context": ' + JSON.stringify('Long context '.repeat(2500)) + ',\n  "safe_integer_boundary": 900719925474099312345,\n  "precise_decimal": 1.234567890123456789,\n  "enabled": false,\n  "count": 0,\n  "empty": "",\n  "missing": null,\n  "items": [],\n  "options": {},\n  "nested": {"deep": {"needle_field": "find-me-value"}},\n  "tail": "easy to find"\n}';
const command = '{"cmd":"printf \'hello\\n\'\\n  npm test","workdir":"/synthetic/project","yield_time_ms":1000,"max_output_tokens":4096}';
const body = (text: string, extra: Partial<DetailPart> = {}): DetailPart => ({text,nextCursor:null,readableBytes:new TextEncoder().encode(text).length,completeness:'unknown',sourceTruncated:false,...extra});
const meta = {format:'json' as const,adapter:'command' as const,origin:'Synthetic recorded exec_command',fields:[{path:'/cmd',scope:'tool' as const,type:'string',description:'Shell command to execute.',descriptionEn:'Shell command to execute.',required:true,sourceUrl:'https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs',sourceVersion:'main · checked 2026-11-03',versionMatch:'reference' as const,format:'code' as const,language:'bash'},{path:'/yield_time_ms',scope:'tool' as const,type:'number',description:'等待输出的时长。',descriptionEn:'Time to wait for output.',unit:'milliseconds',sourceUrl:'https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs',sourceVersion:'main · checked 2026-11-03',versionMatch:'reference' as const}]};
const markdownOutput = '---\nname: reader-fixture\ndescription: A synthetic Markdown output\n---\n\n# Research notes\n\nRead **context** with preserved metadata.\n\n| Item | State |\n| --- | --- |\n| Preview | Ready |\n\n```python\ndef greet(name):\n    return "Hello " + name\n```\n';
const stringOutput = 'Warning: truncated output (synthetic fixture)\nTotal output lines: 80\n\n' + markdownOutput;
const onlyOutput = 'Standalone Call output\r\n' + 'Loaded bytes remain available.\r\n'.repeat(4000) + 'END_OF_LOADED_OUTPUT';
const cases: Record<string, {kind:TraceEvent['kind'];tool?:string;parts:DetailData['parts']}> = {
  orchestration:{kind:'toolCall',tool:'exec',parts:{input:body('text(await tools.exec_command({cmd:"cat /synthetic/SKILL.md",max_output_tokens:6000}));')}},
  ordinary_json:{kind:'toolResult',parts:{output:body('{"i\\u0064":900719925474099312345,"id":1.00e+009,"line":"a\\nb","literal":"C:\\new","nested":{"ok":true},"array":[1,2]}')}},
  long_values:{kind:'toolResult',parts:{output:body(JSON.stringify({object:{deep:{text:'x'.repeat(700)+'OBJECT_TARGET'}},array:Array.from({length:200},(_,i)=>`item-${i}`),string:'prefix '+'x'.repeat(250000)+'STRING_TARGET',tail:'visible sibling'}))}},
  environment:{kind:'environment',parts:{input:body(JSON.stringify({cwd:'/synthetic/project',shell:'zsh',timezone:'Asia/Shanghai',network:'restricted'}))}},
  direct_command:{kind:'toolCall',parts:{input:body('printf "hello\n"\nnpm test',{reader:{format:'code',language:'bash',origin:'commandExecution.command'}})}},
  protocol:{kind:'toolResult',parts:{output:body(JSON.stringify({content:[{type:'text',text:'# Protocol body\n\nReadable **message**.'}],isError:false}),{reader:{format:'json',origin:'mcpToolCall.result',fields:[{path:'/content/0/text',scope:'protocol',format:'text',description:'MCP text body',sourceUrl:'https://modelcontextprotocol.io/specification/2025-06-18/server/tools',sourceVersion:'2025-06-18',versionMatch:'reference'}]}})}},
  call_output_only:{kind:'toolCall',tool:'output.only',parts:{output:body(onlyOutput)}},
  call_empty_input:{kind:'toolCall',tool:'output.only',parts:{input:body(''),output:body('Only effective output')}},
  result_pair:{kind:'toolResult',tool:'paired.result',parts:{input:body('Recorded input'),output:body('Recorded output')}},
  result_input_placeholder:{kind:'toolResult',tool:'input.only',parts:{input:body('Only effective input'),output:body('',{completeness:'notRecorded'})}},
  deferred_pair:{kind:'toolCall',tool:'deferred.input',parts:{input:body('Input confirmed by reference before loading'),output:body('Output arrived first')}},
  unknown_empty_pair:{kind:'toolCall',tool:'unknown.input',parts:{input:body('',{readableBytes:null}),output:body('Only output remains after the unknown input is read')}},
  unknown_empty_manual:{kind:'toolCall',tool:'unknown.input',parts:{input:body('',{readableBytes:null}),output:body('Manual collapse overrides pending defaults')}},
  markdown_output:{kind:'toolResult',tool:'command',parts:{output:body(markdownOutput,{reader:{format:'text',origin:'commandExecution.aggregatedOutput'}})}},
  escaped_output:{kind:'toolResult',tool:'functions.exec',parts:{output:body(JSON.stringify({content:[{type:'text',text:stringOutput}],literal:'literal \\n is kept; C:\\temp\\notes'}),{sourceTruncated:true,completeness:'fragment',reader:{format:'json',origin:'synthetic tool JSON'}})}},
  auto_code:{kind:'toolResult',tool:'unknown.function',parts:{output:body('def greet(name):\n    message = "Hello " + name\n    return message\n',{reader:{format:'text',origin:'synthetic text'}})}},
  context:{kind:'developerContext',parts:{input:body(context,{reader:{format:'markdown',origin:'synthetic context'}})}},
  json:{kind:'toolCall',tool:'unknown.function',parts:{input:body(complex,{reader:{format:'json',origin:'recorded JSON'}}),output:body('INFO starting\n  loading assets\nINFO complete')}},
  command:{kind:'toolCall',tool:'functions.exec_command',parts:{input:body(command,{reader:meta}),output:body('npm test\n  ✓ 28 tests passed\n  elapsed 2.1s')}},
  partial:{kind:'toolCall',tool:'unknown.function',parts:{input:body(complex.slice(0,12000),{nextCursor:'remaining',readableBytes:new TextEncoder().encode(complex).length,reader:{format:'json',origin:'recorded JSON'}}),output:body('',{completeness:'notRecorded'})}},
  truncated:{kind:'toolResult',tool:'unknown.function',parts:{output:body('{"message":"source cut',{sourceTruncated:true,completeness:'fragment',reader:{format:'json',origin:'recorded JSON'}})}},
  error:{kind:'toolCall',tool:'functions.exec_command',parts:{input:body(command,{reader:meta}),output:body('Working…'),error:body('Error: synthetic permission denied\n  path: /synthetic/project')}},
  array:{kind:'toolCall',tool:'command',parts:{input:body(JSON.stringify(['bash','-lc','echo "hello world"','literal $HOME']),{reader:{format:'json',adapter:'command',origin:'recorded argument array'}}),output:body('hello world')}},
  diff:{kind:'toolCall',tool:'apply_patch',parts:{input:body('*** Begin Patch\n*** Update File: sample.ts\n@@\n-const value = 1;\n+const value = 2;\n*** End Patch',{reader:{format:'diff',language:'diff',adapter:'patch',origin:'recorded patch'}}),output:body('Applied successfully')}},
  large:{kind:'toolResult',tool:'large.data',parts:{output:body(JSON.stringify({rows:Array.from({length:20000},(_,i)=>({id:i,label:`row-${i}`})),last:'tail-value'}),{reader:{format:'json',origin:'synthetic structure'}})}},
  files:{kind:'toolCall',tool:'apply_patch',parts:{input:body(JSON.stringify([{path:'src/example.py',kind:{type:'update'},diff:'@@ -1 +1 @@\n-print(1)\n+print(2)'}]),{reader:{format:'json',adapter:'patch',origin:'fileChange.changes',fields:[{path:'/0/diff',scope:'protocol',description:'Recorded diff',sourceUrl:'https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/v2/ThreadStartResponse.json',sourceVersion:'main · reviewed 2026-11-03',versionMatch:'reference',format:'diff'}]}}),output:body('Saved')}},
  oversized:{kind:'toolResult',tool:'large.data',parts:{output:body('{"large":"'+'x'.repeat(8_000_010)+'"}',{reader:{format:'json',origin:'synthetic large body'}})}},
  unknown:{kind:'toolCall',tool:'unknown.tool',parts:{input:body('Unrecognized format\n  indentation stays\n<script>alert("unsafe")</script>'),output:body('log line\n    second line')}},
};
const base = fixture as ThreadSnapshot;
function createSnapshot():ThreadSnapshot {
  const s = structuredClone(base), sample = s.events[0];
  s.title='Preview reader · synthetic acceptance';
  s.events=Object.entries(cases).map(([id,c],ordinal)=>({...sample,eventId:id,threadId:s.binding.threadId,groupId:s.rootGroupId,turnId:s.turns[0].turnId,kind:c.kind,toolName:c.tool??null,preview:id,order:{...sample.order,ordinal},status:{...sample.status,value:id==='error'?'failed':'unknown'},content:Object.entries(c.parts).map(([part,p])=>({part:part as any,loaded:false,sourceTruncated:p!.sourceTruncated,chunked:!!p!.nextCursor,readableBytes:p!.readableBytes!,chunkCursor:null,completeness:p!.completeness as any,reader:p!.reader}))}));
  return s;
}
function App() {
  const {setLanguage,setUiStyle}=usePreferences();
  const [snapshot,setSnapshot]=useState(createSnapshot);
  const [view,setView]=useState({...defaultView(),selectedEventId:'context',detailTab:'preview' as const,expandedTurnIds:snapshot.turns.map(t=>t.turnId)});
  const [explanation,setExplanation]=useState('');
  const [loaded,setLoaded]=useState(false);
  const [revealed,setRevealed]=useState(false);
  const [refreshCount,setRefreshCount]=useState(0);
  const [detailHidden,setDetailHidden]=useState(false);
  const [revealedUnknown,setRevealedUnknown]=useState<Record<string,boolean>>({});
  const id=view.selectedEventId;
  const parts=id ? structuredClone(cases[id].parts) : {};
  if(id==='partial' && loaded) parts.input=body(complex,{reader:{format:'json',origin:'recorded JSON'}});
  if(id==='deferred_pair' && !revealed) delete parts.input;
  if(id?.startsWith('unknown_empty_')) {
    if(revealedUnknown[id]) parts.input=body('');
    else delete parts.input;
  }
  if(id==='call_output_only' && refreshCount) parts.output=body(onlyOutput+`\nRefreshed output ${refreshCount}`);
  const detail=id&&!detailHidden?{eventId:id,summary:id,parts:{...parts,raw:body(JSON.stringify({type:'synthetic_raw_record',case:id},null,2)),source:body('Synthetic test source')},explanation}:null;
  Object.assign(window,{readerFixture:{setExplanation,markdownOutput,stringOutput,select:(id:string)=>setView(v=>({...v,selectedEventId:id})),reveal:()=>setRevealed(true),revealUnknown:()=>id&&setRevealedUnknown(values=>({...values,[id]:true})),setDetailHidden,currentParts:()=>structuredClone(parts),refresh:()=>{setRefreshCount(count=>count+1);setSnapshot(s=>({...s,revision:s.revision+1,events:s.events.map(event=>({...event,eventRevision:event.eventRevision+1,content:event.content.map(ref=>event.eventId==='call_output_only'&&ref.part==='output'?{...ref,readableBytes:new TextEncoder().encode(onlyOutput+`\nRefreshed output ${refreshCount+1}`).length}:ref)}))}));},state:()=>({view,snapshot}),setLanguage,setUiStyle}});
  return <><div style={{height:34,display:'flex',gap:10,padding:5,background:'#e7ece8',color:'#263d31'}}><b>Synthetic QA</b><select aria-label="Fixture" value={id??''} onChange={e=>setView(v=>({...v,selectedEventId:e.target.value}))}>{Object.keys(cases).map(id=><option key={id}>{id}</option>)}</select><button onClick={()=>setLanguage('zh')}>中文</button><button onClick={()=>setLanguage('en')}>English</button><button onClick={()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark'}>Theme</button></div><TraceUI snapshot={snapshot} view={view} detail={detail} trackingPaused={false} onTrackingPause={()=>{}} threads={[]} threadsMore={false} threadScope="user" matchedIds={null} loading={false} detailLoading={false} error="" exportMessage="" exporting={false} onTask={()=>{}} onTaskSearch={()=>{}} onMoreThreads={()=>{}} onThreadScope={()=>{}} onView={p=>setView(v=>({...v,...p}))} onSelect={id=>setView(v=>({...v,selectedEventId:id}))} onLoadEarlier={()=>{}} onMoreDetail={()=>setLoaded(true)} onExport={()=>{}} onRetry={()=>{}} /></>;
}
createRoot(document.getElementById('root')!).render(<PreferencesProvider><App/></PreferencesProvider>);
const style=document.createElement('style');style.textContent='.cx-panel{height:calc(100vh - 34px)}';document.head.append(style);
