/* MAIN world: raw TikTok media URLs are confined to this Map and never leave it. */
(()=>{
  'use strict';
  if(window!==top||location.origin!=='https://www.tiktok.com')return;
  const P=globalThis.KurukinProtocol,S=globalThis.KurukinSecurity,A=globalThis.KurukinAudio,context=globalThis.KurukinContext.createContext(),collector=globalThis.KurukinAuthorItems.createCollector(context),origin=location.origin;
  let active=null,completed=null,job=null;const media=new Map();
  const send=(type,scanId,payload)=>{const message=P.message('page',type,scanId,payload);if(P.valid(message,'page'))window.postMessage(message,origin,payload?.buffer?[payload.buffer]:[]);};
  function clear(){job?.controller.abort();job=null;completed=null;media.clear();}
  window.addEventListener('message',async event=>{
    if(event.source!==window||event.origin!==origin||!P.valid(event.data,'content'))return;const {type,scanId,payload}=event.data;
    if(type==='KURUKIN_CONTEXT_GET'){send('CONTEXT',scanId,{username:context.targetUsername(),loggedIn:context.isLoggedIn()});return;}
    if(type==='KURUKIN_SCAN_CANCEL'){if(active?.id===scanId)active.controller.abort();return;}
    if(type==='KURUKIN_SCAN_START'){
      if(active){send('SCAN_ERROR',scanId,{code:'DIRECT_BUSY'});return;}clear();const scan={id:scanId,controller:new AbortController()};active=scan;
      try{const result=await collector.scan({target:payload.target,signal:scan.controller.signal,onProgress:p=>send('SCAN_PROGRESS',scanId,p),onItem:(item,video)=>media.set(video.id,{videoId:video.id,duration:video.duration,candidates:A.candidates(item)})});if(!result.cancelled)completed={scanId,username:context.targetUsername()};else media.clear();send('SCAN_COMPLETE',scanId,result);}catch(error){clear();send('SCAN_ERROR',scanId,{code:S.safeError(error)});}finally{if(active===scan)active=null;}return;
    }
    if(type!=='KURUKIN_AUDIO_REQUEST'||active||job||completed?.scanId!==scanId||completed.username!==context.targetUsername())return;
    const task={controller:new AbortController()};job=task;const emit=(name,data)=>{if(job===task)send(name,scanId,{videoId:payload.videoId,...data});};
    try{const options={media,signal:task.controller.signal,emit,extensionBase:payload.extensionBase};const result=payload.experimental?await A.extractExperimental(payload.videoId,options):await A.extract(payload.videoId,options);if(job===task)emit('AUDIO_READY',{...result.meta,buffer:result.buffer});}catch{/* safe error emitted by extractor */}finally{if(job===task)job=null;}
  });
  window.addEventListener('pagehide',()=>{active?.controller.abort();clear();});
})();
