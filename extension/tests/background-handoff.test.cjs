const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'..');
function worker(fetcher){
  let listener;const sandbox={Blob,FormData,URL,btoa,atob,fetch:fetcher,chrome:{runtime:{onMessage:{addListener:fn=>{listener=fn;}},getURL:()=>''},action:{onClicked:{addListener(){}}},tabs:{sendMessage:async()=>{}}}};
  sandbox.globalThis=sandbox;vm.createContext(sandbox);
  sandbox.importScripts=(...files)=>files.forEach(file=>vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),sandbox,{filename:file}));
  vm.runInContext(fs.readFileSync(path.join(root,'background.js'),'utf8'),sandbox,{filename:'background.js'});
  return {listener};
}
const wav=Uint8Array.from([82,73,70,70,38,0,0,0,87,65,86,69,102,109,116,32,16,0,0,0,1,0,1,0,128,62,0,0,0,125,0,0,2,0,16,0,100,97,116,97,2,0,0,0,0,0]).buffer;
test('actual service-worker listener decodes handoff and reaches fetch',async()=>{let request;const w=worker(async(url,options)=>{request={url,options};return {status:202};});const base64=btoa(String.fromCharCode(...new Uint8Array(wav))),response=await new Promise(resolve=>{assert.equal(w.listener({type:'KURUKIN_BACKEND_UPLOAD',analysisId:'analysis-1',tiktokId:'7678773331130125582',wavBase64:base64,mime:'audio/wav'},{url:'https://www.tiktok.com/@owner/video/7678773331130125582'},resolve),true);});assert.equal(response.ok,true);assert.equal(response.value.status,202);assert.equal(new URL(request.url).origin,'https://intelligence.kuruk.in');assert.equal(request.options.body.get('audio').type,'audio/wav');});
test('service worker relays only the safe failure report to the backend',async()=>{let request;const w=worker(async(url,options)=>{request={url,options};return {status:202,json:async()=>({released:true})};});const response=await new Promise(resolve=>{assert.equal(w.listener({type:'KURUKIN_BACKEND_FAILURE',analysisId:'analysis-1',tiktokId:'7678773331130125582',code:'FETCH_MP4_FAILED'},{url:'https://www.tiktok.com/@owner/video/7678773331130125582'},resolve),true);});assert.equal(response.ok,true);assert.equal(response.value.released,true);assert.match(request.url,/acquisition-failure$/);assert.deepEqual(JSON.parse(request.options.body),{code:'FETCH_MP4_FAILED'});});
