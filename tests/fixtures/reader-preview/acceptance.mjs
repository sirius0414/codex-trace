/** 统一阅读浏览器验收；只使用合成数据，捕获复制参数而不修改用户剪贴板。 */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
export async function runAcceptance(page, artifactDir) {
  const checks=[];
  const check=(name,condition)=>{assert.ok(condition,name);checks.push(name);};
  const settle=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const select=async id=>{await page.selectOption('[aria-label="Fixture"]',id);await settle();};
  const copy=async part=>{await page.click(`[data-reader-part="${part}"] .cx-reader-copy`);await settle();check(`Exact full copy: ${part}`,await page.evaluate(part=>window.qaCopied===window.readerFixture.currentParts()[part].text,part));};
  const search=async query=>{await page.fill('[aria-label="Search loaded entry body"]',query);await settle();await page.click('.cx-reader-search-results button >> nth=0');await settle();check(`Search reveals ${query}`,await page.evaluate(query=>document.querySelector('[data-reader-hit]')?.textContent===query,query));};
  const expanded=()=>page.evaluate(()=>!!document.querySelector('.cx-reader-expanded'));
  const expand=async()=>{if(!await expanded())await page.click('.cx-reader-expand');await settle();};
  const shot=async name=>{const size=await page.evaluate(()=>({width:Math.ceil(innerWidth*devicePixelRatio),height:Math.ceil(innerHeight*devicePixelRatio)}));const result=await page.cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,...size,scale:1}});await writeFile(`${artifactDir}/${name}.png`,Buffer.from(result.data,'base64'));};
  await mkdir(artifactDir,{recursive:true});
  await page.reload();await page.click('text="English"');
  await page.evaluate(()=>{document.documentElement.dataset.theme='light';Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.qaCopied=text;}}});});
  await select('ordinary_json');await expand();
  check('Ordinary nested JSON is immediately readable and preserves number/key spelling',await page.evaluate(()=>{const text=document.querySelector('.cx-json-text').textContent;return text.includes('900719925474099312345')&&text.includes('1.00e+009')&&text.includes('"i\\u0064"')&&text.includes('"ok": true')&&!document.querySelector('.cx-json-fold');}));
  check('One entry search and one body copy; no tree or fixed notices',await page.evaluate(()=>document.querySelectorAll('.cx-reader input[type="search"]').length===1&&document.querySelectorAll('.cx-reader-copy').length===1&&!document.querySelector('.cx-reader-integrity,.cx-reader-json-toolbar,.cx-reader-json-search,.cx-reader-code-head')));
  check('Manual format and language controls are hidden until More',await page.evaluate(()=>[...document.querySelectorAll('.cx-reader-more select')].every(x=>!x.checkVisibility())));
  await copy('output');await shot('01-ordinary-json');
  await page.click('[data-reader-part="output"] .cx-reader-switch button:text-is("Source")');
  check('Body source preserves loaded text exactly',await page.evaluate(()=>document.querySelector('.cx-reader-source').textContent===window.readerFixture.currentParts().output.text));
  await copy('output');
  await select('long_values');await expand();
  check('Long object, array and string fold locally, root and sibling stay visible',await page.evaluate(()=>document.querySelector('[aria-label="Expand /object"]').textContent==='{…}'&&document.querySelector('[aria-label="Expand /array"]').textContent==='[…]'&&document.querySelector('[aria-label="Expand /string"]').textContent.includes('prefix')&&document.querySelector('.cx-json-text').textContent.includes('visible sibling')));
  await copy('output');
  await page.focus('[aria-label="Expand /object"]');await page.press('[aria-label="Expand /object"]','Enter');await settle();
  check('Local fold supports keyboard expansion',await page.evaluate(()=>!!document.querySelector('[aria-label="Collapse /object"]')));
  await page.click('[aria-label="Collapse /object"] >> nth=0');await settle();
  check('Collapse returns keyboard focus to the local value',await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')==='Expand /object'));
  await search('OBJECT_TARGET');
  check('Search expands all containing long values without switching to source',await page.evaluate(()=>document.querySelector('[data-reader-part="output"] .cx-reader-switch button[aria-pressed="true"]').textContent==='Read'&&!!document.querySelector('[aria-label="Collapse /object"]')));
  await search('STRING_TARGET');await copy('output');
  check('Late long-string search uses bounded text window',await page.evaluate(()=>document.querySelector('.cx-json-text').textContent.length<105000));
  await select('ordinary_json');await select('long_values');
  check('Expanded values and search location survive entry switch',await page.evaluate(()=>document.querySelector('[data-reader-hit]')?.textContent==='STRING_TARGET'&&!!document.querySelector('[aria-label="Collapse /object"]')));
  await page.evaluate(()=>window.readerFixture.refresh());await settle();
  check('Refresh preserves local expansion',await page.evaluate(()=>!!document.querySelector('[aria-label="Collapse /object"]')));
  await page.click('.cx-reader-search-results button >> nth=0');await settle();
  check('Repeating the same hit restores focus',await page.evaluate(()=>document.activeElement?.hasAttribute('data-reader-hit')));
  await shot('02-search-long-string');
  await select('large');await search('row-19999');
  check('Late array match is revealed with at most 400 rows',await page.evaluate(()=>document.querySelectorAll('.cx-json-text .cx-reader-code-line').length<=400&&!!document.querySelector('.cx-json-text [data-reader-hit]')));
  await copy('output');
  await select('partial');
  check('Partial JSON shows source and load actions',await page.evaluate(()=>!!document.querySelector('.cx-reader-source')&&!!document.querySelector('.cx-more-content')));
  await copy('input');await page.click('.cx-more-content >> nth=0');await settle();
  check('Loading the rest replaces fallback with continuous JSON',await page.evaluate(()=>!!document.querySelector('.cx-json-text')&&!document.querySelector('.cx-more-content')));
  await search('easy to find');await copy('input');
  await select('truncated');
  check('Known source truncation and fragment remain visible',await page.evaluate(()=>document.querySelector('.cx-reader-integrity').textContent.includes('Source was truncated')&&document.querySelector('.cx-reader-integrity').textContent.includes('Only a source fragment')));
  await select('oversized');
  check('Resource limit falls back to bounded source',await page.evaluate(()=>!!document.querySelector('.cx-reader-json-fallback')&&document.querySelector('.cx-reader-source').textContent.length<101000));
  await copy('output');
  for(const id of ['markdown_output','direct_command','auto_code','diff','environment','protocol']){
    await select(id);
    check(`${id} uses the expected readable presentation`,await page.evaluate(id=>!!document.querySelector(id==='markdown_output'||id==='protocol'?'.cx-reader-rich h1':id==='environment'?'.cx-json-text':'.cx-reader-code code'),id));
    check(`${id} has no duplicate code copy`,await page.evaluate(()=>!document.querySelector('.cx-reader-code-head')));
    await copy(['direct_command','diff','environment'].includes(id)?'input':'output');
  }
  await select('escaped_output');
  check('Unknown text wrapper keeps all fields rather than guessing',await page.evaluate(()=>!!document.querySelector('.cx-json-text')&&document.querySelector('.cx-json-text').textContent.includes('literal')));
  await select('environment');
  await page.evaluate(()=>window.readerFixture.setExplanation('当前块为已读取内容。'));await settle();
  check('Known boilerplate is removed',await page.evaluate(()=>!document.querySelector('.cx-detail-body').textContent.includes('当前块为已读取内容')));
  await page.evaluate(()=>window.readerFixture.setExplanation('当前块为已读取内容。 加载失败，请重试。'));await settle();
  check('Real explanation remains visible next to removed boilerplate',await page.evaluate(()=>document.querySelector('.cx-note').textContent==='加载失败，请重试。'));
  await page.evaluate(()=>window.readerFixture.setExplanation(''));
  await select('ordinary_json');
  await page.click('.cx-reader-more summary');await page.selectOption('[aria-label="Output format"]','code');
  await page.selectOption('[aria-label="Output code language"]','python');
  await page.press('[aria-label="Output code language"]','Escape');await settle();
  check('More supports format/language override and Escape without exiting expanded reading',await expanded()&&await page.evaluate(()=>!document.querySelector('.cx-reader-more').open&&!!document.querySelector('.cx-reader-code')));
  await page.click('.cx-reader-more summary');await page.selectOption('[aria-label="Output format"]','auto');await page.press('[aria-label="Output format"]','Escape');
  await page.click('.cx-reader-settings-trigger');
  await page.selectOption('.cx-reader-settings select','18');
  await page.click('.cx-reader-settings input >> nth=1');
  await page.press('.cx-reader-settings select','Escape');
  check('Font and line-number preferences still apply to JSON',await page.evaluate(()=>document.querySelector('.cx-json-text').classList.contains('numbered')&&document.querySelector('.cx-reader').style.getPropertyValue('--cx-reader-font')==='18px'));
  await page.click('.cx-reader-settings-trigger');await page.selectOption('.cx-reader-settings select','15');await page.click('.cx-reader-settings input >> nth=1');await page.press('.cx-reader-settings select','Escape');
  await page.click('.cx-reader-expand');await settle();
  await page.focus('[aria-label="Resize content preview"]');await page.press('[aria-label="Resize content preview"]','ArrowLeft');
  check('Keyboard resizing still works',await page.evaluate(()=>Number(document.querySelector('.cx-inspector-resizer').getAttribute('aria-valuenow'))>0));
  await writeFile(`${artifactDir}/checks.json`,JSON.stringify(checks,null,2));
  return checks;
}

export async function runNarrowAcceptance(page, artifactDir) {
  const checks=[];
  const settle=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const check=(name,value)=>{assert.ok(value,name);checks.push(name);};
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  const zoom=390/await page.evaluate(()=>innerWidth);
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:Math.round(390*zoom),height:Math.round(844*zoom),deviceScaleFactor:1,mobile:false});
  await page.reload();
  await settle();
  await page.evaluate(()=>window.readerFixture.select('json'));await settle();
  for(const language of ['en','zh'])for(const style of ['classic','soft','terminal'])for(const theme of ['light','dark']){
    await page.evaluate(({language,style,theme})=>{window.readerFixture.setLanguage(language);window.readerFixture.setUiStyle(style);document.documentElement.dataset.theme=theme;},{language,style,theme});await settle();
    check(`390px ${language}/${style}/${theme}: one compact control row and no body overflow`,await page.evaluate(()=>{const head=document.querySelector('.cx-reader-block-head').getBoundingClientRect(),pre=document.querySelector('.cx-json-text').getBoundingClientRect();return innerWidth===390&&head.height<46&&pre.right<=innerWidth+1&&document.documentElement.scrollWidth<=innerWidth+1;}));
  }
  const size=await page.evaluate(()=>({width:Math.ceil(innerWidth*devicePixelRatio),height:Math.ceil(innerHeight*devicePixelRatio)}));
  const result=await page.cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,...size,scale:1}});await writeFile(`${artifactDir}/03-narrow-dark.png`,Buffer.from(result.data,'base64'));
  await page.cdp('Emulation.clearDeviceMetricsOverride');
  await page.evaluate(()=>{window.readerFixture.setLanguage('en');window.readerFixture.setUiStyle('classic');document.documentElement.dataset.theme='light';});
  await writeFile(`${artifactDir}/narrow-checks.json`,JSON.stringify(checks,null,2));
  return checks;
}
