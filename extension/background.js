/* Backend HTTPS is confined to this service worker; MAIN world never sees it. */
importScripts('lib/backend.js','lib/wav-handoff.js','lib/service-worker-upload.js');

const backend=globalThis.KurukinBackend;
const workerUpload=globalThis.KurukinServiceWorkerUpload;
const exact=(value,keys)=>!!value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
const tiktokSender=sender=>/^https:\/\/www\.tiktok\.com\//.test(sender?.url||sender?.tab?.url||'');
const video=value=>!!value&&typeof value==='object'&&typeof value.id==='string'&&/^\d{5,30}$/.test(value.id)&&typeof value.author==='string'&&typeof value.nickname==='string'&&typeof value.url==='string'&&/^https:\/\/www\.tiktok\.com\/@[\w.]+\/video\/\d{5,30}$/.test(value.url);
const reserveMessage=value=>exact(value,['type','videos'])&&value.type==='KURUKIN_BACKEND_RESERVE'&&Array.isArray(value.videos)&&value.videos.length>0&&value.videos.length<=200&&value.videos.every(video);
const uploadMessage=value=>exact(value,['type','analysisId','tiktokId','wavBase64','mime'])&&value.type==='KURUKIN_BACKEND_UPLOAD'&&typeof value.analysisId==='string'&&value.analysisId.length>0&&value.analysisId.length<=256&&typeof value.tiktokId==='string'&&/^\d{5,30}$/.test(value.tiktokId)&&typeof value.wavBase64==='string'&&value.wavBase64.length>0&&value.wavBase64.length<=Math.ceil(globalThis.KurukinWavHandoff.MAX_AUDIO_BYTES/3)*4&&value.mime==='audio/wav';

chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  if(!tiktokSender(sender)||(!reserveMessage(message)&&!uploadMessage(message))){sendResponse({ok:false,error:backend.safeError()});return false;}
  (async()=>{
    try{
      const value=message.type==='KURUKIN_BACKEND_RESERVE'?await backend.reserve(fetch,message.videos):await workerUpload.upload(fetch,message);
      sendResponse({ok:true,value});
    }catch(error){const handoffFailure=message.type==='KURUKIN_BACKEND_UPLOAD'&&error?.stage==='HANDOFF_WAV',safe=handoffFailure?{code:['WAV_TOO_LARGE','WAV_INVALID','WAV_BASE64_INVALID','BASE64_ENCODE_FAILED'].includes(error?.code)?error.code:'RUNTIME_MESSAGE_FAILED',diagnostic:backend.safeError(error).diagnostic}:backend.safeError(error);if(message.type==='KURUKIN_BACKEND_UPLOAD'){safe.stage=handoffFailure?'HANDOFF_WAV':'HTTP_UPLOAD';if(Number.isSafeInteger(error?.handoffMs))safe.handoffMs=error.handoffMs;}sendResponse({ok:false,error:safe});}
    finally{if(message.type==='KURUKIN_BACKEND_UPLOAD')message.wavBase64=null;}
  })();
  return true;
});

chrome.action.onClicked.addListener(async tab=>{
  if(!tab.id)return;
  try{await chrome.tabs.sendMessage(tab.id,{type:'KURUKIN_PANEL_TOGGLE'});}catch{/* Non-TikTok tabs and tabs awaiting reload have no content receiver. */}
});
