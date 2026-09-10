(function (root) {
  'use strict';
  const {fail} = root.KurukinSecurity;
  function createContext(env = root) {
    const doc = env.document;
    function script(id) {
      try {
        const s = doc.getElementById(id)?.textContent;
        return s && s.length < 12000000 ? JSON.parse(s) : null;
      } catch { return null; }
    }
    const scope = v => v?.__DEFAULT_SCOPE__ || {};
    function sources() {
      return [env.SIGI_STATE, script('SIGI_STATE'), env.__$UNIVERSAL_DATA$__, script('__UNIVERSAL_DATA_FOR_REHYDRATION__')].filter(Boolean);
    }
    function appCandidates() {
      return sources().map(v => v.AppContext?.appContext || scope(v)['webapp.app-context'] ||
        (v.wid && v.language ? v : null)).filter(Boolean);
    }
    const appValid = a => !!(a?.wid && a.region && a.language);
    function localApp() { return appCandidates().find(appValid); }
    async function getAppContext(signal) {
      const local = localApp();
      if (local) return local;
      let response;
      try { response = await env.fetch('/node-webapp/api/common-app-context', {credentials: 'include', signal}); }
      catch { if (signal?.aborted) throw fail('DIRECT_CANCELLED'); throw fail('DIRECT_CONTEXT_MISSING'); }
      if (response.status === 403 || response.status === 429) throw fail(`DIRECT_${response.status}`);
      if (!response.ok) throw fail('DIRECT_CONTEXT_MISSING');
      let data;
      try {
        const body = await response.text();
        if (/^\s*</.test(body) && /captcha|challenge|verify/i.test(body)) throw fail('DIRECT_CHALLENGE');
        data = JSON.parse(body);
      } catch (error) { if (error?.code) throw error; throw fail('DIRECT_CONTEXT_MISSING'); }
      if (data.type === 'verify') throw fail('DIRECT_CHALLENGE');
      if (data.statusCode !== 0 || !appValid(data)) throw fail('DIRECT_CONTEXT_MISSING');
      return data;
    }
    function getBizContext() {
      return sources().map(v => v.BizContext?.bizContext || scope(v)['webapp.biz-context'] || v).find(v => v?.os) || {};
    }
    function getApiDomains() {
      const domains = getBizContext().domains;
      return domains?.mTApi && domains.rootApi ? domains : script('api-domains') || {};
    }
    function getVerifyFp() {
      // Reference request fingerprint only. Session detection never calls this function.
      return doc.cookie.match(/s_v_web_id=(\w+)/)?.[1];
    }
    function buildCommonParams(app, biz = getBizContext()) {
      if (!appValid(app)) throw fail('DIRECT_CONTEXT_MISSING');
      const nav = env.navigator, current = scope(env.__$UNIVERSAL_DATA$__);
      const ab = current['seo.abtest'];
      const versions = [ ...(current['webapp.app-context']?.abTestVersion?.versionName?.split(',') || []),
        ...Object.values(ab?.parameters?.clientABVersions || {}), ...(ab?.vidList || []) ].filter(Boolean).join(',');
      return {
        aid: '1988', app_name: 'tiktok_web', browser_language: nav.language, browser_name: nav.appCodeName,
        browser_online: nav.onLine, browser_platform: nav.platform, browser_version: nav.appVersion,
        channel: 'tiktok_web', cookie_enabled: nav.cookieEnabled, device_platform: 'web_pc', focus_state: true,
        history_len: env.history.length, is_fullscreen: env.matchMedia('(display-mode: fullscreen)').matches,
        is_page_visible: true, referer: doc.referrer, screen_height: env.screen.height, screen_width: env.screen.width,
        tz_name: env.Intl.DateTimeFormat().resolvedOptions().timeZone, verifyFp: getVerifyFp(),
        data_collection_enabled: true, user_is_login: true, clientABVersions: versions,
        app_language: app.language, device_id: app.wid, os: biz.os || (nav.userAgent?.includes('Mac') ? 'mac' : 'windows'),
        priority_region: app.user?.region, region: app.region, webcast_language: app.language,
        WebIdLastTime: app.webIdCreatedTime, odinId: app.odinId
      };
    }
    const targetUsername = () => env.location.pathname.match(/^\/@([\w.]{1,64})(?:\/|$)/)?.[1] || null;
    function getTargetProfileData(username) {
      if (!root.KurukinNormalizer.username(username)) return null;
      const pending = sources().map(value => ({value, depth: 0})), seen = new WeakSet();
      let visited = 0;
      while (pending.length && visited++ < 40000) {
        const {value, depth, keyedName} = pending.pop();
        if (!value || typeof value !== 'object' || seen.has(value) || depth > 18) continue;
        seen.add(value);
        if (appValid(value)) continue;
        const owner = root.KurukinNormalizer.username(value.uniqueId ?? value.unique_id) || keyedName;
        const secUid = value.secUid ?? value.sec_uid;
        if (owner?.toLowerCase() === username.toLowerCase() && typeof secUid === 'string' && /^[\w=-]{1,256}$/.test(secUid)) {
          return {uniqueId: owner, nickname: typeof value.nickname === 'string' ? value.nickname.slice(0,256) : '', secUid};
        }
        for (const [key, child] of Object.entries(value).slice(0, 2000)) {
          if (/cookie|token|password|authorization|session|headers|app.?context|biz.?context|user.?context/i.test(key)) continue;
          if (child && typeof child === 'object' && pending.length < 40000) pending.push({value: child, depth: depth + 1,
            keyedName: key.toLowerCase() === username.toLowerCase() ? key : null});
        }
      }
      return null;
    }
    function isLoggedIn(app = localApp()) {
      const visible = selector => [...doc.querySelectorAll(selector)].some(el => el.getClientRects().length > 0 && env.getComputedStyle(el).visibility !== 'hidden');
      if (visible('[data-e2e="top-login-button"], [data-e2e="nav-login-button"]')) return false;
      const candidates = app ? [app, ...appCandidates()] : appCandidates();
      for (const a of candidates) {
        for (const field of ['isLogin','isLoggedIn','loggedIn']) {
          if (a?.[field] === false || a?.user?.[field] === false) return false;
          if (a?.[field] === true || a?.user?.[field] === true) return true;
        }
        if (a?.user?.uid && String(a.user.uid) !== '0' && a.user.uniqueId) return true;
      }
      return visible('[data-e2e="profile-icon"], [data-e2e="nav-profile"], [data-e2e="inbox-icon"]');
    }
    return Object.freeze({getAppContext, getBizContext, getApiDomains, getVerifyFp, buildCommonParams,
      getTargetProfileData, targetUsername, isLoggedIn});
  }
  root.KurukinContext = Object.freeze({createContext});
  if (typeof module !== 'undefined') module.exports = root.KurukinContext;
})(globalThis);
