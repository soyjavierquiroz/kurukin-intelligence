const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const V=require('../page/speech.js');

test('silence and obvious non-voice metrics classify no_speech',()=>{
  for(const segments of [[],[{start:0,end:.2}]]) assert.equal(V.classify(30,segments).classification,'no_speech');
});
test('speech PCM metrics classify candidate and a middle result remains ambiguous',()=>{
  assert.equal(V.classify(10,[{start:1,end:4}]).classification,'speech_candidate');
  assert.equal(V.classify(20,[{start:1,end:1.8}]).classification,'ambiguous');
});
test('assets are local and packaged; no remote executable resources',()=>{
  const root=path.join(__dirname,'..'), manifest=require('../manifest.json');
  for(const file of ['lib/vad/ort-wasm-1.22.0.min.js','lib/vad/ort-wasm-simd-threaded.wasm','lib/vad/ort-wasm-simd-threaded.mjs','lib/vad/silero-vad-legacy.onnx']) assert.ok(fs.statSync(path.join(root,file)).isFile(),file);
  const source=fs.readFileSync(path.join(root,'page/speech.js'),'utf8');
  assert.doesNotMatch(source,/https?:\/\//);assert.doesNotMatch(source,/eval\s*\(/);
  assert.ok(manifest.web_accessible_resources[0].resources.includes('lib/vad/silero-vad-legacy.onnx'));
});
