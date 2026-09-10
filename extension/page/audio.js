/* MAIN-only MP4 -> PCM mono16k -> WAV. Normal acquisition never loads ML. */
(function (root) {
  'use strict';
  const codes = ['VIDEO_NOT_AVAILABLE_IN_CURRENT_SCAN','AUDIO_MEDIA_NOT_FOUND','AUDIO_MEDIA_FETCH_FAILED','AUDIO_MEDIA_HTTP_ERROR','AUDIO_MEDIA_TIMEOUT','AUDIO_MEDIA_CONTENT_TYPE','AUDIO_DECODE_FAILED','AUDIO_RESAMPLE_FAILED','AUDIO_SPEECH_DETECTOR_FAILED','AUDIO_CLASSIFIER_FAILED','AUDIO_WAV_FAILED','AUDIO_CONTEXT_INVALIDATED'];
  const fail = code => Object.assign(new Error(code), {code});
  const num = n => Number.isFinite(Number(n)) && Number(n) >= 0 ? Number(n) : 0;
  function candidates(item) {
    const v=item?.video || {}, list=[];
    const add=(value,source,meta={})=>{
      const urls=typeof value==='string'?[value]:value?.UrlList || value?.urlList || [];
      if(!Array.isArray(urls) || meta.HasAudio===false || meta.hasAudio===false) return;
      for(const url of urls) { try { const u=new URL(url); if(u.protocol!=='https:' || u.username || u.password) continue; } catch { continue; }
        list.push({url,source,width:num(value?.Width || meta.width),height:num(value?.Height || meta.height),bitrate:num(meta.Bitrate || meta.bitrate),size:num(value?.DataSize),codec:String(meta.CodecType || meta.codecType || ''),audioKnown:meta.HasAudio===true || meta.hasAudio===true}); }
    };
    for(const b of Array.isArray(v.bitrateInfo)?v.bitrateInfo:[]) if(b && typeof b==='object' && (!b.Format || typeof b.Format==='string' && b.Format.toLowerCase()==='mp4')) add(b.PlayAddr || b.playAddr,'bitrateInfo',b);
    add(v.playAddr,'playAddr',v); add(v.PlayAddrStruct,'PlayAddrStruct',v); add(v.downloadAddr,'downloadAddr',v);
    const rank=c=>c.source==='bitrateInfo'?0:c.source==='downloadAddr'?2:1, seen=new Set();
    return list.sort((a,b)=>rank(a)-rank(b) || Number(b.audioKnown)-Number(a.audioKnown) || Number(/265|hevc/i.test(a.codec))-Number(/265|hevc/i.test(b.codec)) || (a.size||Infinity)-(b.size||Infinity) || a.bitrate-b.bitrate).filter(c=>!seen.has(c.url)&&seen.add(c.url)).slice(0,3);
  }
  const frames=duration=>Math.max(1,Math.ceil(duration*16000));
  const topVideo=videos=>[...videos].sort((a,b)=>b.views-a.views)[0]||null;
  function downmix(buffer) { const mono=new Float32Array(buffer.length); for(let c=0;c<buffer.numberOfChannels;c++){const source=buffer.getChannelData(c);for(let i=0;i<mono.length;i++)mono[i]+=source[i]/buffer.numberOfChannels;} return mono; }
  function wav(samples) { const result=new ArrayBuffer(44+samples.length*2),d=new DataView(result),str=(offset,text)=>{for(let i=0;i<text.length;i++)d.setUint8(offset+i,text.charCodeAt(i));}; str(0,'RIFF');d.setUint32(4,result.byteLength-8,true);str(8,'WAVE');str(12,'fmt ');d.setUint32(16,16,true);d.setUint16(20,1,true);d.setUint16(22,1,true);d.setUint32(24,16000,true);d.setUint32(28,32000,true);d.setUint16(32,2,true);d.setUint16(34,16,true);str(36,'data');d.setUint32(40,samples.length*2,true);for(let i=0;i<samples.length;i++){const x=Math.max(-1,Math.min(1,samples[i]||0));d.setInt16(44+i*2,Math.round(x*(x<0?32768:32767)),true);}return result; }
  function freshMeta(info) { return {candidate:0,source:'none',httpStatus:null,contentType:'unknown',contentLength:null,bytes:0,expectedDuration:info?.duration ?? null,sampleRate:0,channels:0,duration:0,wavBytes:0,fetchMp4Ms:null,decodeResampleMs:null,wavEncodeMs:null,totalAcquisitionMs:null}; }
  async function extract(videoId,{media,signal,emit=()=>{},env=root,timeoutMs=60000,onPcm}={}) {
    const info=media.get(videoId), started=Date.now(), state=freshMeta(info); let phase='fetch', mp4=null,decoded=null,rendered=null,mono=null,offline=null,context=null;
    const check=()=>{if(signal?.aborted)throw fail('AUDIO_CONTEXT_INVALIDATED');};
    const error=(code,terminal=true)=>({...state,terminal,code,phase,name:'AudioError',message:code,stack:`KurukinAudio.extract:${phase}`});
    if(!info){emit('AUDIO_ERROR',error('VIDEO_NOT_AVAILABLE_IN_CURRENT_SCAN'));throw fail('VIDEO_NOT_AVAILABLE_IN_CURRENT_SCAN');}
    emit('AUDIO_START',{}); let last=fail('AUDIO_MEDIA_NOT_FOUND');
    try { for(const [index,candidate] of info.candidates.entries()) { phase='fetch';Object.assign(state,{candidate:index+1,source:candidate.source,httpStatus:null,contentType:'unknown',contentLength:null,bytes:0});
      try { check();emit('AUDIO_MEDIA_FETCHING',{...state});const began=Date.now(),controller=new AbortController();let timedOut=false;const abort=()=>controller.abort();signal?.addEventListener('abort',abort,{once:true});const timer=env.setTimeout(()=>{timedOut=true;controller.abort();},timeoutMs);
        try { const response=await env.fetch(candidate.url,{credentials:'include',cache:'no-store',signal:controller.signal});state.httpStatus=response.status;const type=(response.headers.get('content-type')||'').split(';')[0].trim().toLowerCase(),allowed=['video/mp4','video/webm','video/quicktime','application/octet-stream','binary/octet-stream','application/mp4'];state.contentType=allowed.includes(type)?type:type?'other':'unknown';const length=response.headers.get('content-length');state.contentLength=length&&/^\d+$/.test(length)&&Number.isSafeInteger(Number(length))?Number(length):null;if(!response.ok)throw fail('AUDIO_MEDIA_HTTP_ERROR');if(type&&!allowed.includes(type))throw fail('AUDIO_MEDIA_CONTENT_TYPE');mp4=await response.arrayBuffer();state.bytes=mp4.byteLength;if(!state.bytes)throw fail('AUDIO_MEDIA_FETCH_FAILED'); }
        catch(e){throw signal?.aborted?fail('AUDIO_CONTEXT_INVALIDATED'):timedOut?fail('AUDIO_MEDIA_TIMEOUT'):codes.includes(e.code)?e:fail('AUDIO_MEDIA_FETCH_FAILED');}
        finally {env.clearTimeout(timer);signal?.removeEventListener('abort',abort);state.fetchMp4Ms=Date.now()-began;}
        check();emit('AUDIO_MEDIA_FETCHED',{...state});phase='decode';emit('AUDIO_DECODING',{...state});const conversion=Date.now();
        try {context=new env.AudioContext();const pending=context.decodeAudioData(mp4);mp4=null;decoded=await pending;if(!decoded.numberOfChannels||!decoded.length)throw fail('AUDIO_DECODE_FAILED');}catch{throw fail('AUDIO_DECODE_FAILED');}finally{mp4=null;if(context){await context.close().catch(()=>{});context=null;}}
        Object.assign(state,{sampleRate:decoded.sampleRate,channels:decoded.numberOfChannels,duration:decoded.duration});check();phase='resample';emit('AUDIO_RESAMPLING',{...state});
        try {offline=new env.OfflineAudioContext(1,frames(decoded.duration),16000);mono=offline.createBuffer(1,decoded.length,decoded.sampleRate);mono.copyToChannel(downmix(decoded),0);decoded=null;const source=offline.createBufferSource();source.buffer=mono;source.connect(offline.destination);source.start();rendered=await offline.startRendering();source.disconnect();source.buffer=null;offline=null;}catch{throw fail('AUDIO_RESAMPLE_FAILED');}
        state.decodeResampleMs=Date.now()-conversion;check();const pcm=rendered.getChannelData(0);if(onPcm) await onPcm(pcm,state);phase='wav';const wavStarted=Date.now();let buffer;try{buffer=wav(pcm);}catch{throw fail('AUDIO_WAV_FAILED');}finally{state.wavEncodeMs=Date.now()-wavStarted;rendered=null;mono=null;}
        state.wavBytes=buffer.byteLength;state.totalAcquisitionMs=Date.now()-started;return {buffer,meta:state};
      } catch(e) {mp4=null;decoded=null;rendered=null;mono=null;offline=null;last=signal?.aborted?fail('AUDIO_CONTEXT_INVALIDATED'):e;emit('AUDIO_ERROR',error(codes.includes(last.code)?last.code:'AUDIO_WAV_FAILED',signal?.aborted||!['fetch','decode'].includes(phase)||index===info.candidates.length-1));if(signal?.aborted||!['fetch','decode'].includes(phase))throw last;}
    } throw last; } finally {mp4=null;decoded=null;rendered=null;mono=null;offline=null;if(context)await context.close().catch(()=>{});}
  }
  async function loadExperimental(base,doc=root.document) { if(root.KurukinSpeech&&root.KurukinAudioClassifier)return;const load=src=>new Promise((resolve,reject)=>{const node=doc.createElement('script');node.src=src;node.onload=resolve;node.onerror=reject;doc.head.appendChild(node);});await load(base+'lib/vad/ort-wasm-1.22.0.min.js');await load(base+'lib/vad/vad-web-0.0.29.min.js');await load(base+'page/speech.js');await load(base+'lib/audio-classifier/classifier.js'); }
  async function extractExperimental(videoId,options={}) { const base=options.extensionBase;if(typeof base!=='string')throw fail('AUDIO_CLASSIFIER_FAILED');await loadExperimental(base,options.env?.document || root.document);return extract(videoId,{...options,onPcm:async(pcm,state)=>{const speechStarted=Date.now();const speech=await root.KurukinSpeech.analyze(pcm,{assetBase:base+'lib/vad/'});state.sileroMs=Date.now()-speechStarted;const yamnetStarted=Date.now();const classifier=await root.KurukinAudioClassifier.analyze(pcm,{assetBase:base+'lib/audio-classifier/'});state.yamnetMs=Date.now()-yamnetStarted;Object.assign(state,speech,classifier);}}); }
  root.KurukinAudio=Object.freeze({candidates,topVideo,frames,downmix,wav,extract,extractExperimental,loadExperimental,codes});
  if(typeof module!=='undefined')module.exports=root.KurukinAudio;
})(globalThis);
