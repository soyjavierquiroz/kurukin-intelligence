const {test} = require('node:test');
const assert = require('node:assert/strict');
const {N,item} = require('./helpers.cjs');
for(const [key,value] of Object.entries({id:'12345',author:'martamarcilla',nickname:'Marta',caption:'Caption <script>literal</script>',created_at:1700000000,views:10,likes:2,comments:3,shares:4,favorites:5,duration:12,url:'https://www.tiktok.com/@martamarcilla/video/12345'})) test(`normalize ${key}`,()=>assert.equal(N.normalize(item())[key],value));
test('duration absent is null and stats absent are zero',()=>{
  const raw=item();raw.video={};delete raw.stats;const n=N.normalize(raw);assert.equal(n.duration,null);assert.equal(n.views,0);
});
test('unsafe numeric ID is rejected',()=>{const raw=item();raw.id=9999999999999999999;assert.equal(N.normalize(raw),null);});
test('numeric stats strings are supported',()=>{const raw=item();raw.stats.playCount='123';assert.equal(N.normalize(raw).views,123);});
test('normalization rejects invalid author',()=>{const raw=item();raw.author.uniqueId='bad?token=secret';assert.equal(N.normalize(raw),null);});
test('normalizes only public music metadata',()=>{
  const normalized=N.normalize(item());
  assert.deepEqual({music_id:normalized.music_id,music_title:normalized.music_title,music_author:normalized.music_author,music_original:normalized.music_original},
    {music_id:'music-123',music_title:'Fixture sound',music_author:'Fixture author',music_original:true});
});
test('music metadata is null when absent and original is never inferred from title',()=>{
  const raw=item(); raw.music={id:'music-123',title:'original sound - not evidence',authorName:'Author'};
  const normalized=N.normalize(raw);
  assert.equal(normalized.music_original,null); assert.equal(normalized.music_title,'original sound - not evidence');
  raw.music={id:12,title:3,authorName:false,original:1};
  assert.deepEqual(N.musicMetadata(raw),{music_id:null,music_title:null,music_author:null,music_original:null});
});
