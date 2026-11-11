/** Verify the paused-query state is explained in the actual list and footer. */
import { before, after, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import { defaultView, searchKeys, searchVisibility } from '../../frontend/src/state/session.ts';

const fixture=JSON.parse(readFileSync(new URL('../fixtures/contracts/snapshot-running.json',import.meta.url),'utf8'));
let server,TraceUI,PreferencesProvider;
const originalWindow=globalThis.window;
before(async()=>{
  globalThis.window={matchMedia:()=>({matches:true})};
  server=await createServer({root:fileURLToPath(new URL('../..',import.meta.url)),configFile:false,
    appType:'custom',server:{middlewareMode:true,hmr:false,ws:false,watch:null}});
  ({default:TraceUI}=await server.ssrLoadModule('/frontend/src/components/TraceUI.tsx'));
  ({PreferencesProvider}=await server.ssrLoadModule('/frontend/src/preferences.tsx'));
});
after(async()=>{
  await server?.close();
  if(originalWindow===undefined)delete globalThis.window;else globalThis.window=originalWindow;
});
function render(paused,cached=null) {
  const view={...defaultView(),filters:{...defaultView().filters,text:'pending term'}};
  return renderToStaticMarkup(React.createElement(PreferencesProvider,null,React.createElement(TraceUI,{
    snapshot:fixture,view,trackingPaused:paused,...searchVisibility(view.filters.text,searchKeys(view,fixture),cached,paused),
    detail:null,threads:[],threadsMore:false,threadScope:'user',loading:false,detailLoading:false,error:'',exportMessage:'',exporting:false,
    onTask(){},onTaskSearch(){},onMoreThreads(){},onThreadScope(){},onView(){},onTrackingPause(){},onSelect(){},onLoadEarlier(){},onMoreDetail(){},onExport(){},onRetry(){},
  })));
}

test('a query typed while paused shows resume guidance instead of searching or a false no-match result',()=>{
  const html=render(true);
  assert.match(html,/<strong>Search paused<\/strong>/);
  assert.match(html,/Resume tracking in the toolbar to automatically search for the current text\./);
  assert.match(html,/Search paused · Resume tracking to search/);
  assert.doesNotMatch(html,/Searching loaded content|No matches in loaded records/);
});

test('resuming removes the paused guidance and a confirmed empty query can then show no matches',()=>{
  const waiting=render(false);
  assert.match(waiting,/Searching loaded content/);
  assert.doesNotMatch(waiting,/Search paused|Resume tracking in the toolbar/);
  const view={...defaultView(),filters:{...defaultView().filters,text:'pending term'}};
  const completed=render(false,{...searchKeys(view,fixture),ids:new Set()});
  assert.match(completed,/No matches in loaded records/);
  assert.doesNotMatch(completed,/Search paused|Searching loaded content/);
});
