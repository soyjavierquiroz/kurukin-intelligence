const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path');
const C=require('../lib/audio-classifier/classifier.js');
const root=path.join(__dirname,'..'), map=fs.readFileSync(path.join(root,'lib/audio-classifier/yamnet_class_map.csv'),'utf8').trim().split(/\r?\n/).slice(1).map(row=>row.replace(/^\d+,\/[^,]+,(?:"(.*)"|(.*))$/,'$1$2'));
test('YAMNet package is local, pinned and includes notices',()=>{
 for(const file of ['yamnet.onnx','yamnet_class_map.csv','THIRD_PARTY_NOTICES.md','YAMNET_APACHE-2.0.txt']) assert.ok(fs.statSync(path.join(root,'lib/audio-classifier',file)).isFile(),file);
 assert.equal(fs.statSync(path.join(root,'lib/audio-classifier/yamnet.onnx')).size,16093355);assert.equal(map.length,521);
 const src=fs.readFileSync(path.join(root,'lib/audio-classifier/classifier.js'),'utf8');assert.doesNotMatch(src,/https?:\/\//);assert.doesNotMatch(src,/eval\s*\(/);
});
test('groups keep singing distinct from speech and identify Music root',()=>{
 const g=C.groups(map);for(const label of ['Speech','Conversation','Narration, monologue','Whispering'])assert.ok(g.speech.has(map.indexOf(label)));
 for(const label of ['Singing','Choir','Humming'])assert.ok(g.singing.has(map.indexOf(label))&&!g.speech.has(map.indexOf(label)));
 assert.ok(g.music.has(map.indexOf('Music')));assert.equal(C.GROUPS.speech.includes('Singing'),false);
});
test('aggregation is finite, bounded, patch-aware and classification is conservative',()=>{
 const scores=new Float32Array(521*2);scores[map.indexOf('Speech')]=.9;scores[521+map.indexOf('Speech')]=.8;scores[map.indexOf('Music')]=.1;scores[521+map.indexOf('Music')]=.1;
 const r=C.aggregate(scores,map);assert.equal(r.audio_classification,'speech');assert.equal(r.speech_patch_ratio,1);assert.ok(r.top_classes.length<=3);for(const key of ['speech_score','music_score','singing_score','speech_patch_ratio','music_patch_ratio','singing_patch_ratio'])assert.ok(Number.isFinite(r[key])&&r[key]>=0&&r[key]<=1);
 assert.equal(C.decide(.8,.8,.1).audio_classification,'mixed');assert.equal(C.decide(.1,.8,.1).audio_classification,'music');assert.equal(C.decide(.1,.1,.8).audio_classification,'singing');assert.equal(C.decide(.3,.3,.3).audio_classification,'ambiguous');
});
test('ground-truth fixture is labels only, not a claimed dataset',()=>{
 const rows=JSON.parse(fs.readFileSync(path.join(root,'tests/fixtures/audio-ground-truth.json')));assert.equal(rows.length,9);assert.equal(rows.filter(r=>r.label==='music').length,5);assert.equal(rows.filter(r=>r.label==='speech').length,4);assert.ok(rows.every(r=>/^\d+$/.test(r.id)&&Object.keys(r).length===2));
});
