/* Backend HTTPS is confined to this service worker; MAIN world never sees it. */
importScripts('lib/backend.js','lib/wav-handoff.js','lib/service-worker-upload.js');
importScripts('lib/auto-curator.js');

const backend=globalThis.KurukinBackend;
const workerUpload=globalThis.KurukinServiceWorkerUpload;
const AUTO_CURATOR_V1_ENABLED=true;
let auto=null;
const curator=()=>auto||(auto=globalThis.KurukinAutoCurator.create({
  storage:{get:async key=>(await chrome.storage.local.get(key))[key],set:(key,value)=>chrome.storage.local.set({[key]:value})},
  tabs:{navigate:(tabId,url)=>Number.isInteger(tabId)?chrome.tabs.update(tabId,{url}):Promise.reject(Error('AUTO_TAB_MISSING')),ensure:async(tabId,channel)=>{if(!Number.isInteger(tabId))throw Error('AUTO_TAB_MISSING');const tab=await chrome.tabs.get(tabId),target=`/@${channel.channel}`.toLowerCase(),path=new URL(tab.url||'https://www.tiktok.com/').pathname.toLowerCase();if(path===target||path.startsWith(`${target}/`))return true;await chrome.tabs.update(tabId,{url:channel.profile_url});return false;},run:(tabId,channel)=>Number.isInteger(tabId)?chrome.tabs.sendMessage(tabId,{type:'KURUKIN_AUTO_RUN',channel:{id:channel.id,channel:channel.channel,target:'full',analysisId:channel.analysis_id||null,scanId:channel.scan_id||null,resume:channel.resume_state||null,acquisition:channel.acquisition||null}}).catch(()=>{}):Promise.resolve(),pause:tabId=>Number.isInteger(tabId)?chrome.tabs.sendMessage(tabId,{type:'KURUKIN_AUTO_PAUSE'}).catch(()=>{}):Promise.resolve()},
  alarms:{create:(name,info)=>chrome.alarms.create(name,info),clear:name=>chrome.alarms.clear(name)},instanceId:'extension-service-worker'
}));
const exact=(value,keys)=>!!value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
const tiktokSender=sender=>/^https:\/\/www\.tiktok\.com\//.test(sender?.url||sender?.tab?.url||'');
const video=value=>!!value&&typeof value==='object'&&typeof value.id==='string'&&/^\d{5,30}$/.test(value.id)&&typeof value.author==='string'&&typeof value.nickname==='string'&&typeof value.url==='string'&&/^https:\/\/www\.tiktok\.com\/@[\w.]+\/video\/\d{5,30}$/.test(value.url);
const reserveMessage=value=>exact(value,['type','videos'])&&value.type==='KURUKIN_BACKEND_RESERVE'&&Array.isArray(value.videos)&&value.videos.length>0&&value.videos.length<=200&&value.videos.every(video);
const uploadMessage=value=>exact(value,['type','analysisId','tiktokId','wavBase64','mime'])&&value.type==='KURUKIN_BACKEND_UPLOAD'&&typeof value.analysisId==='string'&&value.analysisId.length>0&&value.analysisId.length<=256&&typeof value.tiktokId==='string'&&/^\d{5,30}$/.test(value.tiktokId)&&typeof value.wavBase64==='string'&&value.wavBase64.length>0&&value.wavBase64.length<=Math.ceil(globalThis.KurukinWavHandoff.MAX_AUDIO_BYTES/3)*4&&value.mime==='audio/wav';
const failureCodes=['FETCH_MP4_VIDEO_NOT_AVAILABLE','FETCH_MP4_FAILED','DECODE_FAILED','WAV_FAILED','HANDOFF_FAILED','HTTP_UPLOAD_FAILED'];
const failureMessage=value=>exact(value,['type','analysisId','tiktokId','code'])&&value.type==='KURUKIN_BACKEND_FAILURE'&&typeof value.analysisId==='string'&&value.analysisId.length>0&&value.analysisId.length<=256&&typeof value.tiktokId==='string'&&/^\d{5,30}$/.test(value.tiktokId)&&failureCodes.includes(value.code);
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
const checkpointTarget=value=>value==='full'||Number.isSafeInteger(value)&&value>=1&&value<=200;
const checkpointMessage=value=>exact(value,['type','payload'])&&value.type==='KURUKIN_BACKEND_CHECKPOINT'&&!!value.payload&&typeof value.payload==='object'&&uuid(value.payload.analysis_id)&&uuid(value.payload.scan_id)&&Number.isSafeInteger(value.payload.checkpoint_number)&&Number.isSafeInteger(value.payload.checkpoint_count)&&Number.isSafeInteger(value.payload.discovered_count)&&checkpointTarget(value.payload.target)&&typeof value.payload.has_more==='boolean'&&Array.isArray(value.payload.videos)&&value.payload.videos.length>0&&value.payload.videos.length<=50&&value.payload.videos.every(video);
const nextBatchMessage=value=>exact(value,['type','analysisId','discoveryComplete'])&&value.type==='KURUKIN_BACKEND_NEXT_BATCH'&&uuid(value.analysisId)&&typeof value.discoveryComplete==='boolean';
const panelSender=sender=>sender?.url?.startsWith(chrome.runtime.getURL('panel/'));
const panelTab=async sender=>{if(Number.isInteger(sender?.tab?.id))return sender.tab.id;const [tab]=await chrome.tabs.query({active:true,lastFocusedWindow:true});return /^https:\/\/www\.tiktok\.com\//.test(tab?.url||'')&&Number.isInteger(tab.id)?tab.id:null;};
const autoEvent=value=>!!value&&typeof value==='object'&&value.type==='KURUKIN_AUTO_EVENT'&&typeof value.event==='object'&&typeof value.event.type==='string';
const autoCommand=value=>!!value&&typeof value==='object'&&['KURUKIN_AUTO_QUEUE','KURUKIN_AUTO_PAUSE','KURUKIN_AUTO_RESUME','KURUKIN_AUTO_STOP','KURUKIN_AUTO_CLEAR','KURUKIN_AUTO_REMOVE_PENDING','KURUKIN_AUTO_SKIP','KURUKIN_AUTO_GET'].includes(value.type);

chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  if(AUTO_CURATOR_V1_ENABLED&&autoCommand(message)&&panelSender(sender)){
    (async()=>{try{const runner=curator(),tabId=await panelTab(sender);let value;if(message.type==='KURUKIN_AUTO_QUEUE'){if(!Number.isInteger(tabId))throw Error('AUTO_TIKTOK_TAB_REQUIRED');value=await runner.enqueue(message.channels,tabId);}else if(message.type==='KURUKIN_AUTO_PAUSE')value=await runner.pause();else if(message.type==='KURUKIN_AUTO_RESUME')value=await runner.resume();else if(message.type==='KURUKIN_AUTO_STOP')value=await runner.stop();else if(message.type==='KURUKIN_AUTO_CLEAR')value=await runner.clear();else if(message.type==='KURUKIN_AUTO_REMOVE_PENDING'&&typeof message.channelId==='string')value=await runner.removePending(message.channelId);else if(message.type==='KURUKIN_AUTO_SKIP')value=await runner.skip();else value=await runner.load();sendResponse({ok:true,value});}catch{sendResponse({ok:false,error:'AUTO_CURATOR_ERROR'});}})();return true;
  }
  if(AUTO_CURATOR_V1_ENABLED&&autoEvent(message)&&tiktokSender(sender)&&Number.isInteger(sender.tab?.id)){
    (async()=>{try{const event=message.event,runner=curator();const value=event.type==='READY'?await runner.ready(sender.tab.id):await runner.event(event);sendResponse({ok:true,value});}catch{sendResponse({ok:false,error:'AUTO_CURATOR_ERROR'});}})();return true;
  }
  if(!tiktokSender(sender)||(!reserveMessage(message)&&!checkpointMessage(message)&&!nextBatchMessage(message)&&!uploadMessage(message)&&!failureMessage(message))){sendResponse({ok:false,error:backend.safeError()});return false;}
  (async()=>{
    try{
      const value=message.type==='KURUKIN_BACKEND_RESERVE'?await backend.reserve(fetch,message.videos):message.type==='KURUKIN_BACKEND_CHECKPOINT'?await backend.checkpoint(fetch,message.payload):message.type==='KURUKIN_BACKEND_NEXT_BATCH'?await backend.nextBatch(fetch,message.analysisId,message.discoveryComplete):message.type==='KURUKIN_BACKEND_FAILURE'?await backend.reportFailure(fetch,message.analysisId,message.tiktokId,message.code):await workerUpload.upload(fetch,message,{onTransferred:()=>Number.isInteger(sender.tab?.id)?chrome.tabs.sendMessage(sender.tab.id,{type:'KURUKIN_AUTO_TRANSFERRED',videoId:message.tiktokId}).catch(()=>{}):undefined});
      sendResponse({ok:true,value});
    }catch(error){const handoffFailure=message.type==='KURUKIN_BACKEND_UPLOAD'&&error?.stage==='HANDOFF_WAV',safe=handoffFailure?{code:['WAV_TOO_LARGE','WAV_INVALID','WAV_BASE64_INVALID','BASE64_ENCODE_FAILED'].includes(error?.code)?error.code:'RUNTIME_MESSAGE_FAILED',diagnostic:backend.safeError(error).diagnostic}:backend.safeError(error);if(message.type==='KURUKIN_BACKEND_UPLOAD'){safe.stage=handoffFailure?'HANDOFF_WAV':'HTTP_UPLOAD';if(Number.isSafeInteger(error?.handoffMs))safe.handoffMs=error.handoffMs;}sendResponse({ok:false,error:safe});}
    finally{if(message.type==='KURUKIN_BACKEND_UPLOAD')message.wavBase64=null;}
  })();
  return true;
});

chrome.alarms?.onAlarm.addListener(alarm=>{if(AUTO_CURATOR_V1_ENABLED&&alarm.name==='kurukin-auto-curator-retry')void curator().tick();});

chrome.action.onClicked.addListener(async tab=>{
  if(!tab.id)return;
  try{await chrome.tabs.sendMessage(tab.id,{type:'KURUKIN_PANEL_TOGGLE'});}catch{/* Non-TikTok tabs and tabs awaiting reload have no content receiver. */}
});
