/* Local YAMNet event classifier. It consumes the existing Float32 mono 16 kHz PCM. */
(function (root) {
  'use strict';
  const CLASSIFIER = 'yamnet', VERSION = 'tfhub-yamnet-1-tf2onnx-1.16.1';
  const CONFIG_VERSION = 'audio-classifier-config-v1';
  const CONFIG = Object.freeze({patchScore: .35, strong: .55, low: .20, topClasses: 3});
  // These are AudioSet display-name rules, not TikTok metadata. Singing is deliberately excluded.
  const GROUPS = Object.freeze({
    speech: Object.freeze(['Speech','Child speech, kid speaking','Conversation','Narration, monologue','Whispering','Speech synthesizer','Chatter','Speech noise','Shout','Yell','Bellow','Children shouting']),
    singing: Object.freeze(['Singing','Choir','Child singing','Synthetic singing','Yodeling','Chant','Mantra','Humming']),
    rapping: Object.freeze(['Rapping']),
    musicRoots: Object.freeze(['Music','Musical instrument'])
  });
  let sessionPromise = null, labelsPromise = null, initMs = null;
  const finiteScore = n => typeof n === 'number' && Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0;
  const localBase = base => typeof base === 'string' && /^chrome-extension:\/\/[a-p]{32}\/lib\/audio-classifier\/$/.test(base) ? base : null;
  const vadBase = base => typeof base === 'string' && /^chrome-extension:\/\/[a-p]{32}\/lib\/vad\/$/.test(base) ? base : null;
  const isMusic = label => GROUPS.musicRoots.includes(label) || /\bmusic\b/i.test(label);
  function groups(labels) {
    const find = list => new Set(labels.map((label,index)=>list.includes(label) ? index : -1).filter(index=>index >= 0));
    return Object.freeze({speech:find(GROUPS.speech),singing:find(GROUPS.singing),rapping:find(GROUPS.rapping),music:new Set(labels.map((label,index)=>isMusic(label) ? index : -1).filter(index=>index >= 0))});
  }
  async function labels(base, fetcher = root.fetch) {
    if (!labelsPromise) labelsPromise=(async()=>{
      const response=await fetcher(base+'yamnet_class_map.csv',{cache:'no-store'});
      if(!response.ok) throw Error('AUDIO_CLASSIFIER_CLASS_MAP');
      const text=await response.text(), rows=text.trim().split(/\r?\n/).slice(1);
      const parsed=rows.map(row=>{ const m=row.match(/^\d+,\/[^,]+,(?:"([\s\S]*)"|([^,]*))$/); return m ? (m[1] || m[2]) : null; });
      if(parsed.length!==521 || parsed.some(value=>!value)) throw Error('AUDIO_CLASSIFIER_CLASS_MAP');
      return Object.freeze(parsed);
    })();
    return labelsPromise;
  }
  async function session(base, runtime = root.ort) {
    if(!runtime?.InferenceSession) throw Error('AUDIO_CLASSIFIER_RUNTIME_UNAVAILABLE');
    if(!sessionPromise) sessionPromise=(async()=>{
      const started=performance.now();
      // Reuses the globally packaged ONNX Runtime Web 1.22.0 WASM runtime from lib/vad/.
      runtime.env.wasm.wasmPaths=vadBase(base.replace('/audio-classifier/','/vad/'));
      runtime.env.wasm.numThreads=1; runtime.env.wasm.proxy=false;
      const value=await runtime.InferenceSession.create(base+'yamnet.onnx',{executionProviders:['wasm']});
      initMs=Math.round(performance.now()-started); return value;
    })();
    return sessionPromise;
  }
  function aggregate(scores, labelsList) {
    const index=groups(labelsList), sums={speech:0,music:0,singing:0,rapping:0}, maxes={speech:0,music:0,singing:0,rapping:0}, hits={speech:0,music:0,singing:0,rapping:0};
    const patches=Math.max(1,Math.floor(scores.length/521));
    const top=new Float32Array(521);
    for(let patch=0;patch<patches;patch++) {
      const values={speech:0,music:0,singing:0,rapping:0};
      for(let col=0;col<521;col++) { const score=finiteScore(scores[patch*521+col]); top[col]+=score; for(const group of Object.keys(values)) if(index[group].has(col)) values[group]=Math.max(values[group],score); }
      for(const group of Object.keys(values)) { sums[group]+=values[group]; maxes[group]=Math.max(maxes[group],values[group]); if(values[group]>=CONFIG.patchScore) hits[group]++; }
    }
    const metric=group=>({score:finiteScore(sums[group]/patches),max:finiteScore(maxes[group]),ratio:finiteScore(hits[group]/patches)});
    const speech=metric('speech'), music=metric('music'), singing=metric('singing'), rapping=metric('rapping');
    const rank=[...top].map((sum,index)=>({label:labelsList[index],score:finiteScore(sum/patches)})).sort((a,b)=>b.score-a.score || a.label.localeCompare(b.label)).slice(0,CONFIG.topClasses);
    return Object.freeze({...decide(speech.score,music.score,singing.score),speech_score:speech.score,music_score:music.score,singing_score:singing.score,rapping_score:rapping.score,speech_score_max:speech.max,music_score_max:music.max,singing_score_max:singing.max,speech_patch_ratio:speech.ratio,music_patch_ratio:music.ratio,singing_patch_ratio:singing.ratio,top_classes:Object.freeze(rank),patches});
  }
  function decide(speech,music,singing) {
    // Conservative V1: scores are model scores, never calibrated probabilities.
    let audio_classification='ambiguous';
    if(singing>=CONFIG.strong && speech<CONFIG.strong && music<CONFIG.strong) audio_classification='singing';
    else if(speech>=CONFIG.strong && music>=CONFIG.strong) audio_classification='mixed';
    else if(music>=CONFIG.strong && speech<=CONFIG.low && singing<CONFIG.strong) audio_classification='music';
    else if(speech>=CONFIG.strong && singing<CONFIG.strong) audio_classification='speech';
    else if((speech>=CONFIG.strong && singing>=CONFIG.strong) || (music>=CONFIG.strong && singing>=CONFIG.strong)) audio_classification='mixed';
    return {audio_classification};
  }
  async function analyze(samples,{assetBase,runtime=root.ort,fetcher=root.fetch}={}) {
    if(!(samples instanceof Float32Array) || !samples.length) throw new TypeError('PCM must be non-empty Float32Array');
    const base=localBase(assetBase); if(!base) throw Error('AUDIO_CLASSIFIER_ASSET_INVALID');
    const started=performance.now(); const [model,map]=await Promise.all([session(base,runtime),labels(base,fetcher)]);
    const inputName=model.inputNames?.[0] || 'waveform'; const output=await model.run({[inputName]:new runtime.Tensor('float32',samples,[samples.length])});
    const value=output[model.outputNames?.[0] || 'output_0']; if(!value?.data || value.data.length%521) throw Error('AUDIO_CLASSIFIER_OUTPUT');
    const result=aggregate(value.data,map); for(const tensor of Object.values(output)) tensor.dispose?.();
    return Object.freeze({classifier:CLASSIFIER,classifier_version:VERSION,classifier_config_version:CONFIG_VERSION,...result,yamnet_ms:Math.round(performance.now()-started),yamnet_init_ms:initMs});
  }
  root.KurukinAudioClassifier=Object.freeze({CLASSIFIER,VERSION,CONFIG_VERSION,CONFIG,GROUPS,localBase,groups,aggregate,decide,analyze});
  if(typeof module!=='undefined') module.exports=root.KurukinAudioClassifier;
})(globalThis);
