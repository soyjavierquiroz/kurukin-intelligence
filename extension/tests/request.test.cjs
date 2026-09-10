const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createContext,env,app,biz,A} = require('./helpers.cjs');
const referencePath = path.join(__dirname,'../../reference/myfavett-1.12.63/s.js');
const expectedKeys = 'aid app_name browser_language browser_name browser_online browser_platform browser_version channel cookie_enabled device_platform focus_state history_len is_fullscreen is_page_visible referer screen_height screen_width tz_name verifyFp data_collection_enabled user_is_login clientABVersions app_language device_id os priority_region region webcast_language WebIdLastTime odinId'.split(' ');
test('exact 30 common parameter names', () => assert.deepEqual(Object.keys(createContext(env()).buildCommonParams(app())).sort(), expectedKeys.sort()));
test('dynamic common parameters use real fixture values', () => {
  const p = createContext(env()).buildCommonParams(app());
  assert.equal(p.aid,'1988'); assert.equal(p.device_id,'private-device'); assert.equal(p.os,'linux');
  assert.equal(p.priority_region,'GB'); assert.equal(p.region,'ES'); assert.equal(p.WebIdLastTime,'12345');
  assert.equal(p.odinId,'private-odin'); assert.equal(p.browser_name,'Mozilla'); assert.equal(p.history_len,3);
});
test('client AB versions exact concatenation', () => {
  const e = env({__$UNIVERSAL_DATA$__:{__DEFAULT_SCOPE__:{'webapp.app-context':{abTestVersion:{versionName:'a,b'}},'seo.abtest':{parameters:{clientABVersions:{x:'c',y:''}},vidList:['d',null]}}}});
  assert.equal(createContext(e).buildCommonParams(app()).clientABVersions,'a,b,c,d');
});
test('reference OS and cover fallbacks', () => {
  const e = env(); delete e.SIGI_STATE.BizContext; e.navigator.userAgent='Mac';
  const c=createContext(e), p=c.buildCommonParams(app()); assert.equal(p.os,'mac');
  const u=A.createCollector(c,e).buildAuthorItemsRequest({secUid:'target'},app()); assert.equal(u.searchParams.get('coverFormat'),'2');
});
test('fixed endpoint ignores API domains', () => {
  const e=env(), c=createContext(e), url=A.createCollector(c,e).buildAuthorItemsRequest({secUid:'target'},app());
  assert.equal(url.origin+url.pathname,A.ENDPOINT);
});
test('exact authorItems parameters, initial cursor and count', () => {
  const e=env(), url=A.createCollector(createContext(e),e).buildAuthorItemsRequest({secUid:'target'},app());
  for (const [key,value] of Object.entries({secUid:'target',cursor:'0',count:'16',language:'es',from_page:'user',coverFormat:'1',enable_cache:'false',video_encoding:'dash',needPinnedItemIds:'true',post_item_list_request_type:'0'})) assert.equal(url.searchParams.get(key),value);
  assert.equal([...url.searchParams].length,40);
});
test('next cursor preserves decimal string precision', () => {
  const e=env(), url=A.createCollector(createContext(e),e).buildAuthorItemsRequest({secUid:'target',cursor:'9999999999999999999'},app());
  assert.equal(url.searchParams.get('cursor'),'9999999999999999999');
});
test('absent optional values serialize empty, not invented', () => {
  const e=env(), a=app(); delete a.odinId; delete a.webIdCreatedTime; e.document.cookie='';
  const u=A.createCollector(createContext(e),e).buildAuthorItemsRequest({secUid:'target'},a);
  for (const key of ['odinId','WebIdLastTime','verifyFp']) assert.equal(u.searchParams.get(key),'');
});
test('confirmed count, delay and retries', () => {assert.equal(A.COUNT,16);assert.equal(A.DELAY_MS,7000);assert.equal(A.MAX_RETRIES,2);});
for (const variant of ['full','optional missing']) test(`request parity against actual local myFaveTT builder: ${variant}`, {skip:!fs.existsSync(referencePath)}, () => {
  const source=fs.readFileSync(referencePath,'utf8'), e=env(), a=app(), b=biz();
  if (variant==='optional missing') { delete a.odinId; delete a.webIdCreatedTime; delete a.user.region; e.document.cookie=''; delete b.os; delete b.videoCoverSettings; }
  const c=createContext(e);
  const sandbox={...e, window:e, URL, we:{forParams:{app_language:a.language,device_id:a.wid,language:a.language,os:b.os||(e.navigator.userAgent.includes('Mac')?'mac':'windows'),priority_region:a.user.region,region:a.region,webcast_language:a.language,WebIdLastTime:a.webIdCreatedTime,coverFormat:b.videoCoverSettings?.format||2,odinId:a.odinId}}};
  vm.createContext(sandbox);
  // Execute only the isolated URL builder from read-only reference; never vendor proprietary code.
  const builder=source.slice(source.indexOf('function he('),source.indexOf('const je='));
  vm.runInContext(builder+'; result = new Se("authorItems").ht(16).ft("0").vt("target").bt();',sandbox);
  const ours=A.createCollector(c,e).buildAuthorItemsRequest({secUid:'target'},a,b);
  assert.deepEqual(Object.fromEntries(ours.searchParams),Object.fromEntries(new URL(sandbox.result).searchParams));
});
