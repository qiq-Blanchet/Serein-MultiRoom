import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const webRoot=resolve(process.cwd()),require=createRequire(join(webRoot,'package.json'));
const {build,stop}=require('esbuild'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'https://synthetic.invalid'});
for(const key of ['window','document','navigator','HTMLElement','Event','Node'])Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const channels=[],NativeChannel=globalThis.MessageChannel;
globalThis.MessageChannel=class extends NativeChannel{constructor(){super();channels.push(this);}};
const directory=await mkdtemp(join(tmpdir(),'serein-pending-dom-'));
const output=await build({stdin:{contents:`import React,{act} from 'react';import {createRoot} from 'react-dom/client';import {PendingMaterials} from './src/components/PendingMaterials.jsx';export {act};export function mount(work){const root=createRoot(document.getElementById('root'));root.render(<PendingMaterials work={work}/>);return root;}`,resolveDir:webRoot,loader:'jsx'},jsx:'automatic',bundle:true,platform:'node',format:'esm',write:false,define:{'process.env.NODE_ENV':'"development"'}});
const path=join(directory,'bundle.mjs');await writeFile(path,output.outputFiles[0].text);
const {act,mount}=await import(pathToFileURL(path));let root,busy=false,skipped=false,previewWait=null,previewError=null,skipError=null,statusError=null,staleStatus=false,alreadyProcessed=false;const requests=[];
const item={id:17,batch_id:'synthetic',status:'open',kind:'writing',reason:'Synthetic writer failure',summary:'Book club',originals:2,original_chars:100,can_skip:true,
  locator:{source:'synthetic',session_id:'book-chat',first_at:'2025-01-01T00:00:00Z',last_at:'2025-01-01T00:01:00Z',first_raw_id:101,last_raw_id:102,preview:'FIRST_SNIPPET',longest_message:{id:102,role:'assistant',chars:2000}},
  resolved_event_ids:[],last_attempt:{role:'event_writer:0:0',error:'Synthetic error',output_chars:20}};
globalThis.fetch=async(url,options={})=>{
  requests.push({url,body:options.body?JSON.parse(options.body):null});let result;
  if(url.endsWith('/pending/17'))result={id:17,batch_id:'synthetic',messages:[{id:101,role:'user',content:'SYNTHETIC_ORIGINAL'},
    {id:102,role:'assistant',content:'L'.repeat(2000)}],last_attempt:{output_text:'SYNTHETIC_BAD_OUTPUT'}};
  else if(url.endsWith('/resume-pending'))result={status:busy?'busy':'resumed'};
  else if(url.endsWith('/next'))result={status:'queued'};
  else if(url.endsWith('/status')&&statusError)return {ok:false,json:async()=>({detail:statusError})};
  else if(url.endsWith('/status')&&staleStatus)result={pending_materials:[item,{...item,id:18,batch_id:'other'}]};
  else if(url.endsWith('/status'))result={pending_materials:[{...item,status:skipped?'resolved_skipped':'resolved',resolved_event_ids:skipped?[]:['event-synthetic']}]};
  else if(url.endsWith('/recovery-preview')){if(previewWait)await previewWait;if(previewError)return {ok:false,json:async()=>({detail:previewError})};result={pending_id:17,batch_id:'synthetic',originals:2,original_chars:100,max_input_chars:5000,batches:2,oversized_batches:0,can_rebatch:true,skip_revision:'synthetic-revision',...(alreadyProcessed?{already_processed:2,skipped_originals:0}: {})};}
  else if(url.endsWith('/rebatch'))result={status:'rebatched'};
  else if(url.endsWith('/skip-batch')){if(skipError)return {ok:false,json:async()=>({detail:skipError})};skipped=true;result={status:'skipped_batch',skipped_originals:alreadyProcessed?0:2,...(alreadyProcessed?{already_processed:2}:{})};}
  else throw new Error('Unexpected request '+url);
  return {ok:true,json:async()=>structuredClone(result)};
};
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent===text);
async function render(status,attempts=[]){if(root)await act(async()=>root.unmount());requests.length=0;skipped=false;previewWait=previewError=skipError=statusError=null;staleStatus=alreadyProcessed=false;await act(async()=>{root=mount({status,attempts,pending_materials:[item]});});}
async function click(text){await act(async()=>{button(text).click();});}
after(async()=>{if(root)await act(async()=>root.unmount());dom.window.close();stop();for(const c of channels){c.port1.close();c.port2.close();}globalThis.MessageChannel=NativeChannel;await rm(directory,{recursive:true,force:true});});
test('pending summaries do not fetch originals until explicitly opened',async()=>{
  await render();assert.match(document.body.textContent,/已切分待补写/);assert(!requests.length);
  assert.match(document.body.textContent,/book-chat/);assert.match(document.body.textContent,/raw:101/);assert.match(document.body.textContent,/FIRST_SNIPPET/);
  assert(!document.body.textContent.includes('SYNTHETIC_ORIGINAL'));
  await click('查看原话与最后输出');assert.match(document.body.textContent,/SYNTHETIC_BAD_OUTPUT/);assert.match(document.body.textContent,/SYNTHETIC_ORIGINAL/);
  assert.equal(document.querySelectorAll('.pending-originals article').length,2);
  assert.equal(document.querySelector('.pending-originals article pre').textContent,'SYNTHETIC_ORIGINAL');
});
test('continuing a pending item sends its id and starts work only after resume',async()=>{
  await render();await click('从失败步骤继续');assert.deepEqual(requests[0].body,{pending_id:17,replan:false});assert(requests.some(r=>r.url.endsWith('/next')));assert.match(document.body.textContent,/event-synthetic/);
});
test('a busy pipeline does not start another task',async()=>{
  await render();busy=true;await click('从失败步骤继续');busy=false;assert(!requests.some(r=>r.url.endsWith('/next')));assert.match(document.body.textContent,/整理正在运行/);
});
test('smaller batches require preview and an explicit confirmation',async()=>{
  await render();await click('按更小材料重新整理');assert(!requests.some(r=>r.url.endsWith('/rebatch')));await click('确认拆小并整理');assert.deepEqual(requests.find(r=>r.url.endsWith('/rebatch')).body,{batch_id:'synthetic',max_input_chars:5000,confirm:'REBATCH_PIPELINE_BATCH'});
});

test('locating the longest original opens its complete text and focuses the correct message',async()=>{
  await render();await click('定位这条原话');
  assert.equal(document.activeElement.dataset.rawId,'102');
  const message=document.querySelector('[data-raw-id="102"]');
  assert.equal(message.querySelector('details').open,true);
  assert.equal(message.querySelector('pre').textContent.length,2000);
});

test('attempt history shows the recorded model and prompt and opens only its own batch',async()=>{
  const attempt={id:42,batch_id:'synthetic',role:'track_router:0',model:'previous-model',prompt_chars:90000,
    attempt:1,output_chars:0,error:'Upstream returned HTTP 400'};
  await render('completed',[attempt]);
  const history=document.querySelector('summary').parentElement;
  history.open=true;
  assert.match(history.textContent,/上游拒绝了归线请求/);
  assert.match(history.textContent,/previous-model/);
  assert(history.textContent.includes((90000).toLocaleString()));
  assert.match(history.textContent,/本批 2 条原话/);
  assert.equal(requests.length,0);
  await click('查看最长原话');
  assert.equal(document.activeElement.dataset.rawId,'102');
  await render('completed',[{...attempt,batch_id:'other',model:null,prompt_chars:null}]);
  assert.equal(button('查看最长原话'),undefined);
  assert(!document.querySelector('summary').parentElement.textContent.includes('请求提示词'));
});

test('skipping is limited to the selected batch and requires a preview plus confirmation',async()=>{
  await render();await click('跳过本批，保留原话');
  assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('取消');assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  await click('跳过本批，保留原话');await click('确认仅跳过本批');
  assert.deepEqual(requests.find(r=>r.url.endsWith('/skip-batch')).body,{batch_id:'synthetic',confirm:'SKIP_PIPELINE_BATCH',skip_revision:'synthetic-revision'});
  assert.match(document.body.textContent,/已手动跳过，原话保留/);
});

test('a running task still permits material inspection while preventing competing recovery actions',async()=>{
  await render('running');
  for(const label of ['从失败步骤继续','重新切分，保留归线','按更小材料重新整理','跳过本批，保留原话'])assert.equal(button(label).disabled,true);
  assert.match(document.querySelector('.pending-material').textContent,/后台正在整理，暂不能跳过/);
  await click('查看原话与最后输出');assert.equal(requests.length,1);
});

test('skip preview immediately shows progress on its own card and focuses the confirmation',async()=>{
  await render();
  let release;previewWait=new Promise(resolve=>{release=resolve;});
  await click('跳过本批，保留原话');
  try{
    const card=document.querySelector('.pending-material');
    assert.match(card.textContent,/正在.*跳过.*预览/);
    assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  }finally{await act(async()=>release());}
  const confirmation=document.querySelector('[aria-label="确认跳过这批材料"]');
  assert.equal(document.activeElement,confirmation);
  assert(button('确认仅跳过本批'));
});

test('skip preview and submission errors appear at the selected material, not below the list',async()=>{
  await render();
  previewError='原话已变化，请刷新并核对批次';
  await click('跳过本批，保留原话');
  assert(document.querySelector('.pending-material').textContent.includes(previewError));
  assert(!requests.some(r=>r.url.endsWith('/skip-batch')));
  previewError=null;
  await click('跳过本批，保留原话');
  skipError='请稍后重试本批';
  await click('确认仅跳过本批');
  assert(document.querySelector('.pending-material').textContent.includes(skipError));
  assert(button('确认仅跳过本批'));
  assert(!requests.some(r=>r.url.endsWith('/next')));
});

test('a confirmed skip leaves the open list even if refreshing work fails',async()=>{
  await render();
  statusError='Synthetic status request failed';
  await click('跳过本批，保留原话');
  await click('确认仅跳过本批');
  assert.equal(document.querySelector('.pending-material'),null);
  assert.match(document.body.textContent,/已手动跳过，原话保留/);
  assert.match(document.body.textContent,/已跳过这批 2 条原话/);
});

test('an older status response cannot reopen the confirmed skip or hide another pending batch',async()=>{
  await render();
  staleStatus=true;
  await click('跳过本批，保留原话');
  await click('确认仅跳过本批');
  assert.equal(document.querySelector('[aria-label="待办 17"]'),null);
  assert(document.querySelector('[aria-label="待办 18"]'));
  assert.match(document.body.textContent,/待办 17 · 已手动跳过，原话保留/);
});

test('already processed originals use a skip-only preview and the confirmed todo disappears',async()=>{
  await render();
  alreadyProcessed=true;
  await click('跳过本批，保留原话');
  assert.deepEqual(requests.find(r=>r.url.endsWith('/recovery-preview')).body,{batch_id:'synthetic',action:'skip'});
  assert.match(document.querySelector('[aria-label="确认跳过这批材料"]').textContent,/2 条.*已处理/);
  await click('确认仅跳过本批');
  assert.equal(document.querySelector('.pending-material'),null);
  assert.match(document.body.textContent,/已从待办.*移除/);
});
