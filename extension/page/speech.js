/* MAIN-world local speech screening. Silero VAD runs only against 16 kHz PCM. */
(function (root) {
  'use strict';
  const DETECTOR = 'silero-vad', VERSION = '0.0.29-legacy';
  // Conservative policy: only a very short, sparse VAD result is no_speech.
  const THRESHOLDS = Object.freeze({positive: .65, negative: .45, redemptionMs: 800,
    minSpeechMs: 250, noSpeechSeconds: .75, noSpeechRatio: .03, noSpeechLongest: .5,
    candidateSeconds: 2, candidateRatio: .12, candidateLongest: 1.2});
  const MODES = Object.freeze(['observe', 'enforce']);
  const finite = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;
  const localBase = base => typeof base === 'string' && /^chrome-extension:\/\/[a-p]{32}\/lib\/vad\/$/.test(base) ? base : null;
  function merge(segments) {
    const clean = segments.filter(s => finite(s.start) && finite(s.end) && s.end > s.start)
      .map(s => ({start:s.start / 1000, end:s.end / 1000})).sort((a,b)=>a.start-b.start);
    const result=[];
    for(const segment of clean) {
      const previous=result.at(-1);
      if(previous && segment.start <= previous.end) previous.end=Math.max(previous.end,segment.end);
      else result.push(segment);
    }
    return result;
  }
  function classify(duration, segments) {
    const speechDuration=segments.reduce((total,s)=>total+s.end-s.start,0);
    const longest=segments.reduce((maximum,s)=>Math.max(maximum,s.end-s.start),0);
    const ratio=duration ? Math.min(1,speechDuration/duration) : 0;
    let classification='ambiguous';
    if(speechDuration < THRESHOLDS.noSpeechSeconds && ratio < THRESHOLDS.noSpeechRatio && longest < THRESHOLDS.noSpeechLongest) classification='no_speech';
    else if(speechDuration >= THRESHOLDS.candidateSeconds || ratio >= THRESHOLDS.candidateRatio || longest >= THRESHOLDS.candidateLongest) classification='speech_candidate';
    return Object.freeze({classification,speech_ratio:ratio,speech_duration_seconds:speechDuration,
      speech_segments:segments.length,longest_speech_segment_seconds:longest,
      audio_duration_seconds:duration,detector:DETECTOR,detector_version:VERSION});
  }
  async function localRunner(samples, base) {
    if(!root.vad || !root.ort) throw Object.assign(new Error('AUDIO_SPEECH_DETECTOR_UNAVAILABLE'),{code:'AUDIO_SPEECH_DETECTOR_UNAVAILABLE'});
    root.ort.env.wasm.wasmPaths=base;
    root.ort.env.wasm.numThreads=1;
    root.ort.env.wasm.proxy=false;
    const instance=await root.vad.NonRealTimeVAD.new({modelURL:base+'silero-vad-legacy.onnx',
      positiveSpeechThreshold:THRESHOLDS.positive,negativeSpeechThreshold:THRESHOLDS.negative,
      redemptionMs:THRESHOLDS.redemptionMs,minSpeechMs:THRESHOLDS.minSpeechMs});
    const segments=[];
    try { for await(const segment of instance.run(samples,16000)) segments.push({start:segment.start,end:segment.end}); }
    finally { instance.destroy?.(); }
    return segments;
  }
  async function analyze(samples, {assetBase, runner=localRunner} = {}) {
    if(!(samples instanceof Float32Array)) throw new TypeError('PCM must be Float32Array');
    const base=localBase(assetBase);
    if(!base) throw Object.assign(new Error('AUDIO_SPEECH_ASSET_INVALID'),{code:'AUDIO_SPEECH_ASSET_INVALID'});
    const raw=await runner(samples,base);
    return classify(samples.length/16000,merge(raw));
  }
  function shouldSkip(metrics, mode) { return mode==='enforce' && metrics.classification==='no_speech'; }
  root.KurukinSpeech=Object.freeze({DETECTOR,VERSION,THRESHOLDS,MODES,classify,analyze,shouldSkip,localBase});
  if(typeof module!=='undefined') module.exports=root.KurukinSpeech;
})(globalThis);
