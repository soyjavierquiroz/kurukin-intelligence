/* Bounded, byte-preserving WAV handoff helpers shared by content and worker. */
(function(root){
  'use strict';
  const MAX_AUDIO_MB=10;
  const MAX_AUDIO_BYTES=MAX_AUDIO_MB*1024*1024;
  const fail=code=>Object.assign(new Error(code),{code,handoff:true});
  const arrayBuffer=value=>Object.prototype.toString.call(value)==='[object ArrayBuffer]';
  const base64=value=>typeof value==='string'&&value.length>0&&value.length<=Math.ceil(MAX_AUDIO_BYTES/3)*4&&value.length%4===0&&/^[A-Za-z0-9+/]*={0,2}$/.test(value)&&!/=.+/.test(value.slice(0,-2));
  function checkSize(value){if(!arrayBuffer(value))throw fail('WAV_INVALID');if(value.byteLength>MAX_AUDIO_BYTES)throw fail('WAV_TOO_LARGE');return value;}
  function encode(value){
    const bytes=new Uint8Array(checkSize(value));let binary='';
    try{for(let offset=0;offset<bytes.length;offset+=0x8000)binary+=String.fromCharCode(...bytes.subarray(offset,offset+0x8000));return btoa(binary);}catch{throw fail('BASE64_ENCODE_FAILED');}
  }
  function decode(value){
    if(!base64(value))throw fail('WAV_BASE64_INVALID');
    let binary;
    try{binary=atob(value);}catch{throw fail('WAV_BASE64_INVALID');}
    if(binary.length>MAX_AUDIO_BYTES)throw fail('WAV_TOO_LARGE');
    const bytes=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);binary='';return bytes;
  }
  const api=Object.freeze({MAX_AUDIO_MB,MAX_AUDIO_BYTES,checkSize,encode,decode});
  root.KurukinWavHandoff=api;
  if(typeof module!=='undefined')module.exports=api;
})(globalThis);
