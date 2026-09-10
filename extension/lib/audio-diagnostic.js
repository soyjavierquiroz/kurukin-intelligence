/* Safe, display-only diagnostics for the normal audio acquisition path. */
(function(root){
  'use strict';
  const STAGES=Object.freeze(['FETCH_MP4','DECODE_AUDIO','RESAMPLE_PCM','ENCODE_WAV','HANDOFF_WAV','HTTP_UPLOAD']);
  const codes=Object.freeze(['VIDEO_NOT_AVAILABLE','MEDIA_NOT_FOUND','FETCH_FAILED','FETCH_TIMEOUT','UNSUPPORTED_CONTENT_TYPE','DECODE_FAILED','RESAMPLE_FAILED','WAV_ENCODE_FAILED','WAV_TOO_LARGE','WAV_INVALID','WAV_BASE64_INVALID','BASE64_ENCODE_FAILED','RUNTIME_MESSAGE_FAILED','NETWORK_ERROR','INVALID_RESPONSE','UPLOAD_FAILED','ACQUISITION_CANCELLED']);
  const integer=value=>Number.isSafeInteger(value)&&value>=0;
  const http=value=>Number.isInteger(value)&&value>=100&&value<=599?`HTTP_${value}`:null;
  const result=(stage,code)=>Object.freeze({stage,code});
  function fromAudioError(payload){
    const phase=payload?.phase, code=payload?.code;
    if(code==='AUDIO_MEDIA_HTTP_ERROR')return result('FETCH_MP4',http(payload?.httpStatus)||'FETCH_FAILED');
    if(['VIDEO_NOT_AVAILABLE_IN_CURRENT_SCAN','AUDIO_MEDIA_NOT_FOUND'].includes(code))return result('FETCH_MP4',code==='VIDEO_NOT_AVAILABLE_IN_CURRENT_SCAN'?'VIDEO_NOT_AVAILABLE':'MEDIA_NOT_FOUND');
    if(code==='AUDIO_MEDIA_TIMEOUT')return result('FETCH_MP4','FETCH_TIMEOUT');
    if(code==='AUDIO_MEDIA_CONTENT_TYPE')return result('FETCH_MP4','UNSUPPORTED_CONTENT_TYPE');
    if(code==='AUDIO_MEDIA_FETCH_FAILED')return result('FETCH_MP4','FETCH_FAILED');
    if(code==='AUDIO_DECODE_FAILED')return result('DECODE_AUDIO','DECODE_FAILED');
    if(code==='AUDIO_RESAMPLE_FAILED')return result('RESAMPLE_PCM','RESAMPLE_FAILED');
    if(code==='AUDIO_WAV_FAILED')return result('ENCODE_WAV','WAV_ENCODE_FAILED');
    if(code==='AUDIO_CONTEXT_INVALIDATED')return result({fetch:'FETCH_MP4',decode:'DECODE_AUDIO',resample:'RESAMPLE_PCM',wav:'ENCODE_WAV'}[phase]||'FETCH_MP4','ACQUISITION_CANCELLED');
    return result('FETCH_MP4','FETCH_FAILED');
  }
  function fromUploadError(error){
    if(error?.stage==='HANDOFF_WAV')return result('HANDOFF_WAV',codes.includes(error?.code)?error.code:'RUNTIME_MESSAGE_FAILED');
    const status=http(error?.diagnostic?.httpStatus);
    if(status)return result('HTTP_UPLOAD',status);
    const code=error?.diagnostic?.errorCode;
    return result('HTTP_UPLOAD',code==='NETWORK_ERROR'?'NETWORK_ERROR':code==='INVALID_RESPONSE'?'INVALID_RESPONSE':'UPLOAD_FAILED');
  }
  function timings(meta={},handoffMs=null,uploadMs=null){
    return {fetchMp4Ms:integer(meta.fetchMp4Ms)?meta.fetchMp4Ms:null,decodeMs:integer(meta.decodeResampleMs)?meta.decodeResampleMs:null,wavMs:integer(meta.wavEncodeMs)?meta.wavEncodeMs:null,handoffMs:integer(handoffMs)?handoffMs:null,uploadMs:integer(uploadMs)?uploadMs:null};
  }
  const api=Object.freeze({STAGES,codes,fromAudioError,fromUploadError,timings});
  root.KurukinAudioDiagnostic=api;
  if(typeof module!=='undefined')module.exports=api;
})(globalThis);
