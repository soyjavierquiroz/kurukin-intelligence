/* The worker decodes a bounded base64 WAV and is the only code that fetches it. */
(function(root){
  'use strict';
  const handoff=root.KurukinWavHandoff,backend=root.KurukinBackend;
  async function upload(fetcher,message,{BlobCtor=Blob,now=Date.now,onRelease=()=>{}}={}){
    let wavBase64=message.wavBase64,bytes=null,audio=null;
    const handoffStarted=now();let handoffMs=null;
    try{
      try{bytes=handoff.decode(wavBase64);wavBase64=null;message.wavBase64=null;audio=new BlobCtor([bytes],{type:'audio/wav'});}catch(error){error.stage='HANDOFF_WAV';error.handoffMs=now()-handoffStarted;throw error;}
      handoffMs=now()-handoffStarted;const uploadStarted=now();
      try{return {status:await backend.upload(fetcher,message.analysisId,message.tiktokId,audio),handoffMs,uploadMs:now()-uploadStarted};}
      catch(error){error.stage='HTTP_UPLOAD';error.handoffMs=handoffMs;throw error;}
    }finally{wavBase64=null;bytes=null;audio=null;onRelease();}
  }
  const api=Object.freeze({upload});root.KurukinServiceWorkerUpload=api;if(typeof module!=='undefined')module.exports=api;
})(globalThis);
