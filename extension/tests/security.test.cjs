const {test} = require('node:test');
const assert = require('node:assert/strict');
const {S,N,P,item,collector,response} = require('./helpers.cjs');
const scanId='scan-test-1234';
test('valid start message',()=>assert.equal(P.valid(P.message('panel','SCAN_START',scanId,{target:50}),'panel'),true));
test('full channel start message is valid',()=>assert.equal(P.valid(P.message('panel','SCAN_START',scanId,{target:'full'}),'panel'),true));
for(const bad of [null,{},[],{type:'KURUKIN_SCAN_START'},P.message('panel','SCAN_START','',{target:50}),P.message('panel','SCAN_START',scanId,{target:500}),P.message('page','SCAN_START',scanId,{target:50}),{...P.message('panel','SCAN_START',scanId,{target:50}),extra:'secret'}]) test(`invalid message rejected ${JSON.stringify(bad)}`,()=>assert.equal(P.valid(bad,'panel'),false));
test('valid progress schema',()=>assert.equal(P.valid(P.message('page','SCAN_PROGRESS',scanId,{phase:'received',total:1,page:1,elapsedMs:5,debug:S.debug({page:1})}),'page'),true));
test('valid complete schema',()=>assert.equal(P.valid(P.message('page','SCAN_COMPLETE',scanId,{videos:[N.normalize(item())],cancelled:false,elapsedMs:5}),'page'),true));
test('safeError accepts undefined and does not serialize raw error',()=>{
  assert.equal(S.safeError(undefined),'DIRECT_INTERNAL');assert.equal(S.safeError(new Error('secUid=secret')),'DIRECT_INTERNAL');assert.equal(S.safeError({code:'DIRECT_403'}),'DIRECT_403');
});
test('safeError handles hostile getters',()=>assert.equal(S.safeError({get code(){throw new Error();}}),'DIRECT_INTERNAL'));
const sensitive=['secUid','verifyFp','device_id','odinId','cookies','tokens','Authorization','playAddr','downloadAddr','requestUrl'];
for(const key of sensitive) test(`security boundary excludes ${key}`,async()=>{
  const raw=item(), secret='PRIVATE_SENTINEL'; raw[key]=secret; raw.author[key]=secret; raw.video[key]=secret;
  const video=N.normalize(raw);assert.equal(JSON.stringify(video).includes(secret),false);
  const sanitized=N.exportVideos([{...video,[key]:secret}]);assert.equal(JSON.stringify(sanitized).includes(secret),false);
  assert.equal(JSON.stringify(S.debug({page:1,[key]:secret})).includes(secret),false);
  assert.equal(P.valid(P.message('page','SCAN_COMPLETE',scanId,{videos:[{...video,[key]:secret}],cancelled:false,elapsedMs:0}),'page'),false);
});
test('music whitelist permits metadata but excludes music and media URLs across the boundary',()=>{
  const raw=item(); raw.music={id:'sound-id',title:'Public title',authorName:'Public author',original:false,
    playUrl:'https://private.test/music-play?token=PRIVATE_MUSIC_URL',downloadUrl:'https://private.test/music-download?token=PRIVATE_MUSIC_URL',coverLarge:'https://private.test/cover'};
  raw.video.playAddr='https://private.test/play?token=PRIVATE_MEDIA_URL'; raw.video.downloadAddr='https://private.test/download?token=PRIVATE_MEDIA_URL';
  const video=N.normalize(raw), data=JSON.stringify(video);
  assert.deepEqual(Object.keys(video).filter(key=>key.startsWith('music_')).sort(),['music_author','music_id','music_original','music_title']);
  for(const forbidden of ['PRIVATE_MUSIC_URL','PRIVATE_MEDIA_URL','playUrl','downloadUrl','coverLarge','playAddr','downloadAddr']) assert.equal(data.includes(forbidden),false,forbidden);
  assert.equal(P.valid(P.message('page','SCAN_COMPLETE',scanId,{videos:[video],cancelled:false,elapsedMs:0}),'page'),true);
  assert.equal(P.valid(P.message('page','SCAN_COMPLETE',scanId,{videos:[{...video,music:{playUrl:'secret'}}],cancelled:false,elapsedMs:0}),'page'),false);
});
test('request query and raw responses never appear in scan output or progress',async()=>{
  const raw=item();raw.secUid='sensitive';const c=collector([response({statusCode:0,itemList:[raw],cursor:'987654321',hasMore:false,token:'secret'})]), progress=[];
  const result=await c.instance.scan({target:50,onProgress:p=>progress.push(p)});
  const data=JSON.stringify({result,progress});
  for(const secret of ['private-target','private-device','private-odin','private_fp','987654321','playAddr','downloadAddr','?secUid','item_list/'])assert.equal(data.includes(secret),false,secret);
});
test('raw error cannot be used as a protocol payload',()=>assert.equal(P.valid(P.message('page','SCAN_ERROR',scanId,{code:'https://www.tiktok.com/?token=secret'}),'page'),false));
test('backend unavailable diagnostic is constrained to non-sensitive fields',()=>{const good=P.message('content','ACQUISITION_ERROR',scanId,{code:'BACKEND_UNAVAILABLE',diagnostic:{baseHost:'intelligence.kuruk.in',httpStatus:503,errorCode:'HTTP_RESPONSE'}});assert.equal(P.valid(good,'content'),true);for(const bad of [{baseHost:'https://private.test/?token=x',httpStatus:503,errorCode:'HTTP_RESPONSE'},{baseHost:'intelligence.kuruk.in',httpStatus:503,errorCode:'https://private.test/?token=x'},{baseHost:'intelligence.kuruk.in',httpStatus:503,errorCode:'HTTP_RESPONSE',headers:'secret'}])assert.equal(P.valid({...good,payload:{code:'BACKEND_UNAVAILABLE',diagnostic:bad}},'content'),false);});
test('acquisition state accepts only enumerated stage and safe error code',()=>{const good=P.message('content','ACQUISITION_STATE',scanId,{requested:10,uploaded:2,failed:1,released:1,currentVideoId:'12345',lastStatus:0,lastUploadMs:null,lastErrorStage:'FETCH_MP4',lastErrorCode:'HTTP_403',fetchMp4Ms:12,decodeMs:null,wavMs:null,handoffMs:null,uploadMs:null,phase:'failed'});assert.equal(P.valid(good,'content'),true);for(const bad of ['https://private.test/?token=x','FETCH_MP4 · HTTP_403'])assert.equal(P.valid({...good,payload:{...good.payload,lastErrorCode:bad}},'content'),false);assert.equal(P.valid({...good,payload:{...good.payload,released:2}},'content'),false);});
