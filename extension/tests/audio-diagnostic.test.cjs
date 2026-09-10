const {test}=require('node:test'),assert=require('node:assert/strict');
const D=require('../lib/audio-diagnostic.js');
test('FETCH_MP4 reports a safe HTTP error code only',()=>assert.deepEqual(D.fromAudioError({code:'AUDIO_MEDIA_HTTP_ERROR',phase:'fetch',httpStatus:403,mediaUrl:'https://private.test/?token=secret'}),{stage:'FETCH_MP4',code:'HTTP_403'}));
test('DECODE_AUDIO reports its safe error code',()=>assert.deepEqual(D.fromAudioError({code:'AUDIO_DECODE_FAILED',phase:'decode'}),{stage:'DECODE_AUDIO',code:'DECODE_FAILED'}));
test('RESAMPLE_PCM reports its safe error code',()=>assert.deepEqual(D.fromAudioError({code:'AUDIO_RESAMPLE_FAILED',phase:'resample'}),{stage:'RESAMPLE_PCM',code:'RESAMPLE_FAILED'}));
test('ENCODE_WAV reports its safe error code',()=>assert.deepEqual(D.fromAudioError({code:'AUDIO_WAV_FAILED',phase:'wav'}),{stage:'ENCODE_WAV',code:'WAV_ENCODE_FAILED'}));
test('HTTP_UPLOAD reports a safe HTTP error code only',()=>assert.deepEqual(D.fromUploadError({diagnostic:{httpStatus:422,errorCode:'HTTP_RESPONSE',headers:'secret'}}),{stage:'HTTP_UPLOAD',code:'HTTP_422'}));
test('HANDOFF_WAV reports its safe code',()=>assert.deepEqual(D.fromUploadError({stage:'HANDOFF_WAV',code:'WAV_TOO_LARGE'}),{stage:'HANDOFF_WAV',code:'WAV_TOO_LARGE'}));
test('unexecuted timings remain absent and never render a misleading zero',()=>assert.deepEqual(D.timings({fetchMp4Ms:18,decodeResampleMs:24,wavEncodeMs:3}),{fetchMp4Ms:18,decodeMs:24,wavMs:3,handoffMs:null,uploadMs:null}));
