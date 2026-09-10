const S = require('../lib/security.js');
const N = require('../lib/normalizer.js');
const P = require('../lib/protocol.js');
const {createContext} = require('../page/context.js');
const A = require('../page/author-items.js');
const app = () => ({wid: 'private-device', region: 'ES', language: 'es', webIdCreatedTime: '12345', odinId: 'private-odin',
  user: {uid: '900', uniqueId: 'viewer', secUid: 'private-viewer', nickName: 'Viewer', region: 'GB'}});
const biz = () => ({os: 'linux', domains: {mTApi: 'https://other.tiktok.com', rootApi: 'https://root.tiktok.com'}, videoCoverSettings: {format: 1}});
const user = (uniqueId = 'martamarcilla', secUid = 'private-target') => ({id: '12345', uniqueId, nickname: 'Marta', secUid});
const item = (id = '12345') => ({id, desc: 'Caption <script>literal</script>', createTime: 1700000000, author: user(),
  stats: {playCount: 10, diggCount: 2, commentCount: 3, shareCount: 4, collectCount: 5},
  video: {duration: 12, playAddr: 'https://private.test/play?token=secret', downloadAddr: 'https://private.test/download'},
  music: {id: 'music-123', title: 'Fixture sound', authorName: 'Fixture author', original: true, playUrl: 'https://private.test/music?token=secret'}});
function response(data, status = 200, type = 'application/json') {
  return {status, ok: status >= 200 && status < 300, headers: {get: k => k.toLowerCase() === 'content-type' ? type : null},
    text: async () => typeof data === 'string' ? data : JSON.stringify(data)};
}
function env(overrides = {}) {
  const scripts = {}, visible = new Set();
  const e = {SIGI_STATE: {AppContext: {appContext: app()}, BizContext: {bizContext: biz()}, UserModule: {users: {martamarcilla: user(), viewer: user('viewer','private-viewer')}}},
    document: {getElementById: id => scripts[id] ? {textContent: scripts[id]} : null, cookie: 's_v_web_id=private_fp; other=secret', referrer: 'https://www.tiktok.com/',
      querySelectorAll: selector => visible.has(selector) ? [{getClientRects: () => [1]}] : []},
    navigator: {language: 'es-ES', appCodeName: 'Mozilla', onLine: true, platform: 'Linux x86_64', appVersion: '5.0 fixture', cookieEnabled: true, userAgent: 'Fixture Linux'},
    history: {length: 3}, matchMedia: () => ({matches: false}), screen: {height: 900,width: 1440}, Intl,
    location: {pathname: '/@martamarcilla'}, getComputedStyle: () => ({visibility: 'visible'}),
    fetch: async () => response({statusCode: 0, itemList: [], cursor: '0', hasMore: false}), scripts, visible};
  return Object.assign(e, overrides);
}
function collector(pages, options = {}) {
  const e = env(), requests = [], delays = [];
  e.fetch = async (url, init) => {
    requests.push({url: String(url), init});
    const value = pages.shift();
    if (value instanceof Error) throw value;
    if (typeof value === 'function') return value(url, init);
    return value || response({statusCode: 0,itemList: [],cursor: '0',hasMore: false});
  };
  const context = createContext(e);
  const instance = A.createCollector(context, e, async (ms, signal) => { delays.push(ms); if (options.onWait) await options.onWait(signal); });
  return {e, context, instance, requests, delays};
}
module.exports = {S,N,P,A,createContext,app,biz,user,item,response,env,collector};
