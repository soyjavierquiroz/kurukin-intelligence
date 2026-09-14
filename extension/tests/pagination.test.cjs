const {test} = require('node:test');
const assert = require('node:assert/strict');
const {collector,response,item} = require('./helpers.cjs');
const page = (ids,cursor,hasMore) => response({statusCode:0,itemList:ids.map(item),cursor,hasMore});
test('page 1, page 2, next cursor, credentials and hasMore false', async () => {
  const c=collector([page(['10000'],'123',true),page(['10001'],'0',false)]), progress=[];
  const result=await c.instance.scan({target:50,onProgress:p=>progress.push(p)});
  assert.equal(result.videos.length,2);assert.equal(c.requests.length,2);
  assert.equal(new URL(c.requests[1].url).searchParams.get('cursor'),'123');
  assert.equal(c.requests[0].init.credentials,'include');assert.equal(c.delays.length,1);
  assert.ok(c.delays[0]>6900 && c.delays[0]<=7000);
  assert.deepEqual(progress.filter(p=>p.phase==='received').map(p=>p.page),[1,2]);
});
test('target reached truncates final page and stops', async () => {
  const pages=Array.from({length:4},(_,p)=>page(Array.from({length:16},(_,i)=>String(10000+p*16+i)),String(p+1),true));
  const c=collector(pages), result=await c.instance.scan({target:50});
  assert.equal(result.videos.length,50);assert.equal(c.requests.length,4);assert.equal(c.delays.length,3);
});
test('incremental checkpoints are emitted at 50, 100, and 150 without blocking later pages',async()=>{
  const pages=Array.from({length:10},(_,p)=>page(Array.from({length:16},(_,i)=>String(10000+p*16+i)),String(p+1),true));
  const c=collector(pages), checkpoints=[];
  const result=await c.instance.scan({target:150,onCheckpoint:async checkpoint=>checkpoints.push(checkpoint)});
  assert.equal(result.videos.length,150);
  assert.deepEqual(checkpoints.map(checkpoint=>checkpoint.discoveredCount),[50,100,150]);
  assert.ok(checkpoints.every(checkpoint=>checkpoint.videos.length===50));
  assert.equal(c.requests.length,10);
});
test('non-multiple target ends with a final partial checkpoint',async()=>{
  const pages=Array.from({length:8},(_,p)=>page(Array.from({length:16},(_,i)=>String(10000+p*16+i)),String(p+1),true));
  const checkpoints=[];
  await collector(pages).instance.scan({target:120,onCheckpoint:async checkpoint=>checkpoints.push(checkpoint)});
  assert.deepEqual(checkpoints.map(checkpoint=>checkpoint.discoveredCount),[50,100,120]);
  assert.deepEqual(checkpoints.map(checkpoint=>checkpoint.videos.length),[50,50,20]);
  assert.equal(checkpoints.at(-1).complete,true);
});
test('resume starts after a confirmed checkpoint and safely replays only its page tail',async()=>{
  const initial=Array.from({length:4},(_,p)=>page(Array.from({length:16},(_,i)=>String(10000+p*16+i)),String(p+1),true));
  let saved;
  await assert.rejects(collector(initial).instance.scan({target:100,onCheckpoint:async checkpoint=>{saved=checkpoint;throw Error('restart');}}));
  assert.equal(saved.discoveredCount,50);
  const replay=[page(Array.from({length:16},(_,i)=>String(10048+i)),'4',true),...Array.from({length:3},(_,p)=>page(Array.from({length:16},(_,i)=>String(10064+p*16+i)),String(5+p),true))];
  const checkpoints=[];
  await collector(replay).instance.scan({target:100,resume:saved.resume,onCheckpoint:async checkpoint=>checkpoints.push(checkpoint)});
  assert.deepEqual(checkpoints.map(checkpoint=>checkpoint.discoveredCount),[100]);
  assert.equal(checkpoints[0].videos.length,50);
});
test('invalid resume cursor safely falls back to a complete idempotent replay',async()=>{
  const pages=Array.from({length:4},(_,p)=>page(Array.from({length:16},(_,i)=>String(10000+p*16+i)),String(p+1),true));
  const checkpoints=[];
  await collector(pages).instance.scan({target:50,resume:{cursor:'not-a-cursor',seenIds:['10000'],discoveredCount:100,checkpointNumber:2},onCheckpoint:async checkpoint=>checkpoints.push(checkpoint)});
  assert.deepEqual(checkpoints.map(checkpoint=>[checkpoint.checkpointNumber,checkpoint.discoveredCount]),[[1,50]]);
});
test('dedupe across pages', async () => {
  const c=collector([page(['10000'],'1',true),page(['10000','10001'],'0',false)]);
  assert.equal((await c.instance.scan({target:50})).videos.length,2);
});
test('hasMore false stops even with positive cursor', async () => {
  const c=collector([page(['10000'],'123',false)]);await c.instance.scan({target:50});assert.equal(c.requests.length,1);
});
test('cancel before first request', async () => {
  const c=collector([]), a=new AbortController();a.abort();
  assert.equal((await c.instance.scan({target:50,signal:a.signal})).cancelled,true);assert.equal(c.requests.length,0);
});
test('cancel while waiting preserves partial results', async () => {
  const a=new AbortController(); const c=collector([page(['10000'],'1',true)],{onWait:()=>a.abort()});
  const result=await c.instance.scan({target:50,signal:a.signal});assert.equal(result.cancelled,true);assert.equal(result.videos.length,1);assert.equal(c.requests.length,1);
});
test('cancel in flight aborts fetch', async () => {
  const a=new AbortController(); const c=collector([(_url,init)=>new Promise((_,reject)=>{init.signal.addEventListener('abort',()=>reject(new Error('aborted')));a.abort();})]);
  assert.equal((await c.instance.scan({target:50,signal:a.signal})).cancelled,true);
});
test('navigation stops pagination', async () => {
  const c=collector([page(['10000'],'1',true)]);
  await assert.rejects(c.instance.scan({target:50,onProgress:p=>{if(p.phase==='received')c.e.location.pathname='/@someone';}}),{code:'DIRECT_TARGET_MISSING'});
  assert.equal(c.requests.length,1);
});
test('repeated cursor is rejected', async () => {
  const c=collector([page(['10000'],'1',true),page(['10001'],'1',true)]);
  await assert.rejects(c.instance.scan({target:50}),{code:'DIRECT_CURSOR_STALLED'});
});
test('foreign author and photo posts excluded', async () => {
  const foreign=item('10001');foreign.author.uniqueId='someone'; const photo={...item('10002'),imagePost:{}};
  const c=collector([response({statusCode:0,itemList:[item(),foreign,photo],cursor:'0',hasMore:false})]);
  assert.equal((await c.instance.scan({target:50})).videos.length,1);
});
test('rate delay persists across sequential scans', async () => {
  const c=collector([page(['10000'],'0',false),page(['10001'],'0',false)]);
  await c.instance.scan({target:50});await c.instance.scan({target:50});assert.equal(c.delays.length,1);
});
test('maximum two transient retries', async () => {
  const c=collector([new Error('secret-url'),response({},503),page(['10000'],'0',false)]);
  assert.equal((await c.instance.scan({target:50})).videos.length,1);assert.equal(c.requests.length,3);assert.equal(c.delays.length,2);
});
test('third transient failure stops', async () => {
  const c=collector([new Error(),new Error(),new Error(),page(['10000'],'0',false)]);
  await assert.rejects(c.instance.scan({target:50}),{code:'DIRECT_NETWORK'});assert.equal(c.requests.length,3);
});
for(const [status,code] of [[403,'DIRECT_403'],[429,'DIRECT_429'],[400,'DIRECT_HTTP']]) test(`HTTP ${status} has no retry and exposes safe diagnostics`,async()=>{
  const c=collector([response({},status)]), progress=[];
  await assert.rejects(c.instance.scan({target:50,onProgress:p=>progress.push(p)}),{code});assert.equal(c.requests.length,1);
  assert.equal(progress.at(-1).debug.httpStatus,status);
});
for(const [body,type,code] of [[{statusCode:123,itemList:[],cursor:'0',hasMore:false},'application/json','DIRECT_TIKTOK_STATUS'],
  [{type:'verify',statusCode:1},'application/json','DIRECT_CHALLENGE'],['<html>captcha</html>','text/html','DIRECT_CHALLENGE'],
  ['', 'application/json','DIRECT_CHALLENGE'],['broken','application/json','DIRECT_RESPONSE_INVALID']]) test(`response error ${code}: ${type}/${typeof body}`,async()=>{
  const c=collector([response(body,200,type)]);await assert.rejects(c.instance.scan({target:50}),{code});assert.equal(c.requests.length,1);
});
test('missing target',async()=>{
  const c=collector([]);delete c.e.SIGI_STATE.UserModule;
  await assert.rejects(c.instance.scan({target:50}),{code:'DIRECT_TARGET_MISSING'});assert.equal(c.requests.length,0);
});
test('missing context with visible login',async()=>{
  const c=collector([]);delete c.e.SIGI_STATE.AppContext;
  c.e.visible.add('[data-e2e="profile-icon"], [data-e2e="nav-profile"], [data-e2e="inbox-icon"]');
  await assert.rejects(c.instance.scan({target:50}),{code:'DIRECT_CONTEXT_MISSING'});
});
test('login required before requests',async()=>{
  const c=collector([]);c.e.SIGI_STATE.AppContext.appContext.isLogin=false;
  await assert.rejects(c.instance.scan({target:50}),{code:'DIRECT_LOGIN_REQUIRED'});assert.equal(c.requests.length,0);
});
