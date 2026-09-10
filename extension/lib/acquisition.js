/* Isolated relay for backend operations; requests execute in the service worker. */
(function(root){
  'use strict';
  const handoff=root.KurukinWavHandoff;
  const failure=(code='BACKEND_UNAVAILABLE')=>Object.assign(new Error(code),{code,diagnostic:{baseHost:'intelligence.kuruk.in',httpStatus:null,errorCode:'RUNTIME_ERROR'}});
  async function call(runtime,type,payload){
    let response;
    try{response=await runtime.sendMessage({type,...payload});}catch{throw failure();}
    if(!response?.ok){const error=failure(response?.error?.code);if(response?.error?.diagnostic)error.diagnostic=response.error.diagnostic;if(response?.error?.stage)error.stage=response.error.stage;if(Number.isSafeInteger(response?.error?.handoffMs))error.handoffMs=response.error.handoffMs;throw error;}
    return response.value;
  }
  const reserve=(runtime,videos)=>call(runtime,'KURUKIN_BACKEND_RESERVE',{videos});
  const reportFailure=(runtime,analysisId,tiktokId,code)=>call(runtime,'KURUKIN_BACKEND_FAILURE',{analysisId,tiktokId,code});
  async function upload(runtime,analysisId,tiktokId,buffer){
    let wavBase64=null;
    const started=Date.now();
    try{wavBase64=handoff.encode(buffer);const result=await call(runtime,'KURUKIN_BACKEND_UPLOAD',{analysisId,tiktokId,wavBase64,mime:'audio/wav'});return {...result,handoffMs:Math.max(0,Date.now()-started-(result.uploadMs||0))};}
    catch(error){if(error?.handoff||!error?.stage)error.stage='HANDOFF_WAV';if(error?.stage==='HANDOFF_WAV')error.handoffMs=Math.max(0,Date.now()-started);throw error;}
    finally{wavBase64=null;buffer=null;}
  }
  const diagnostic=error=>{const value=error?.diagnostic;return {baseHost:'intelligence.kuruk.in',httpStatus:Number.isInteger(value?.httpStatus)&&value.httpStatus>=100&&value.httpStatus<=599?value.httpStatus:null,errorCode:['NETWORK_ERROR','HTTP_RESPONSE','INVALID_RESPONSE','RUNTIME_ERROR'].includes(value?.errorCode)?value.errorCode:'RUNTIME_ERROR'};};
  const api=Object.freeze({reserve,upload,reportFailure,diagnostic});root.KurukinAcquisition=api;if(typeof module!=='undefined')module.exports=api;
})(globalThis);
