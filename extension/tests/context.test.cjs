const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createContext,env,app,biz,user,response} = require('./helpers.cjs');
for (const source of ['SIGI global','SIGI script','universal global','rehydration flat','rehydration scope']) {
  test(`app context: ${source}`, async () => {
    const e = env({SIGI_STATE: undefined});
    const scope = {__DEFAULT_SCOPE__: {'webapp.app-context': app(),'webapp.biz-context': biz()}};
    if (source === 'SIGI global') e.SIGI_STATE = {AppContext: {appContext: app()}};
    if (source === 'SIGI script') e.scripts.SIGI_STATE = JSON.stringify({AppContext: {appContext: app()}});
    if (source === 'universal global') e.__$UNIVERSAL_DATA$__ = scope;
    if (source === 'rehydration flat') e.scripts.__UNIVERSAL_DATA_FOR_REHYDRATION__ = JSON.stringify(app());
    if (source === 'rehydration scope') e.scripts.__UNIVERSAL_DATA_FOR_REHYDRATION__ = JSON.stringify(scope);
    e.fetch = () => { throw new Error('unexpected fetch'); };
    assert.deepEqual(await createContext(e).getAppContext(), app());
  });
}
test('fallback confirmed endpoint and credentials', async () => {
  const e = env({SIGI_STATE: undefined});
  e.fetch = async (url, init) => { assert.equal(url,'/node-webapp/api/common-app-context'); assert.equal(init.credentials,'include'); return response({...app(),statusCode:0}); };
  assert.equal((await createContext(e).getAppContext()).wid, app().wid);
});
test('missing context fails, never invents identifiers', async () => {
  await assert.rejects(createContext(env({SIGI_STATE: undefined})).getAppContext(), {code:'DIRECT_CONTEXT_MISSING'});
});
for (const source of ['SIGI global','SIGI script','universal','rehydration']) test(`biz context: ${source}`, () => {
  const e = env({SIGI_STATE: undefined});
  if (source === 'SIGI global') e.SIGI_STATE = {BizContext:{bizContext:biz()}};
  if (source === 'SIGI script') e.scripts.SIGI_STATE = JSON.stringify({BizContext:{bizContext:biz()}});
  if (source === 'universal') e.__$UNIVERSAL_DATA$__ = {__DEFAULT_SCOPE__:{'webapp.biz-context':biz()}};
  if (source === 'rehydration') e.scripts.__UNIVERSAL_DATA_FOR_REHYDRATION__ = JSON.stringify(biz());
  assert.deepEqual(createContext(e).getBizContext(),biz());
});
test('domains from biz context', () => assert.deepEqual(createContext(env()).getApiDomains(),biz().domains));
test('domains script fallback', () => {
  const e = env({SIGI_STATE:undefined}); e.scripts['api-domains'] = JSON.stringify(biz().domains);
  assert.deepEqual(createContext(e).getApiDomains(),biz().domains);
});
test('target username from current URL', () => assert.equal(createContext(env()).targetUsername(),'martamarcilla'));
test('target secUid differs from viewer', () => {
  const c = createContext(env());
  assert.deepEqual(c.getTargetProfileData('martamarcilla'), {uniqueId:'martamarcilla',nickname:'Marta',secUid:'private-target'});
});
test('target match case insensitive', () => assert.equal(createContext(env()).getTargetProfileData('MARTAMARCILLA').secUid,'private-target'));
test('never falls back to logged-in user', () => {
  const e = env(); delete e.SIGI_STATE.UserModule;
  assert.equal(createContext(e).getTargetProfileData('martamarcilla'),null);
  assert.equal(createContext(e).getTargetProfileData('viewer'),null);
});
test('flat app viewer is excluded from target search', () => {
  const e = env({SIGI_STATE:undefined}); e.scripts.__UNIVERSAL_DATA_FOR_REHYDRATION__ = JSON.stringify(app());
  assert.equal(createContext(e).getTargetProfileData('viewer'),null);
});
test('universal profile userInfo resolves target', () => {
  const e = env({SIGI_STATE:undefined});
  e.__$UNIVERSAL_DATA$__ = {__DEFAULT_SCOPE__:{'webapp.user-detail':{userInfo:{user:user()}}}};
  assert.equal(createContext(e).getTargetProfileData('martamarcilla').secUid,'private-target');
});
test('keyed SIGI profile without uniqueId is supported', () => {
  const e = env(); delete e.SIGI_STATE.UserModule.users.martamarcilla.uniqueId;
  assert.equal(createContext(e).getTargetProfileData('martamarcilla').uniqueId,'martamarcilla');
});
test('public target alone never proves login', () => {
  const e = env(); delete e.SIGI_STATE.AppContext;
  assert.equal(createContext(e).isLoggedIn(),false);
});
test('login does not read cookies', () => {
  const e = env(); Object.defineProperty(e.document,'cookie',{get(){throw new Error('cookie accessed');}});
  assert.equal(createContext(e).isLoggedIn(),true);
});
test('explicit logout overrides uid', () => {
  const e = env(); e.SIGI_STATE.AppContext.appContext.isLogin = false;
  assert.equal(createContext(e).isLoggedIn(),false);
});
test('verifyFp matches reference cookie extraction only when building request', () => assert.equal(createContext(env()).getVerifyFp(),'private_fp'));
test('malformed script safely falls through', async () => {
  const e = env({SIGI_STATE:undefined}); e.scripts.SIGI_STATE = '{broken';
  await assert.rejects(createContext(e).getAppContext(),{code:'DIRECT_CONTEXT_MISSING'});
});
