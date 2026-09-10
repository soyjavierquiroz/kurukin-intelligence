(function (root) {
  'use strict';
  const S = root.KurukinSecurity, N = root.KurukinNormalizer;
  const ENDPOINT = 'https://www.tiktok.com/api/post/item_list/';
  const COUNT = 16, DELAY_MS = 7000, MAX_RETRIES = 2;
  function wait(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) return reject(S.fail('DIRECT_CANCELLED'));
      const cancel = () => { clearTimeout(timer); reject(S.fail('DIRECT_CANCELLED')); };
      const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve(); }, ms);
      signal?.addEventListener('abort', cancel, {once: true});
    });
  }
  function createCollector(context, env = root, sleep = wait) {
    // Shared across successive scans: repeated clicks cannot accelerate requests.
    let nextRequestAt = 0;
    function buildAuthorItemsRequest({secUid, cursor = '0', count = COUNT}, app, biz = context.getBizContext()) {
      if (typeof secUid !== 'string' || !/^[\w=-]{1,256}$/.test(secUid)) throw S.fail('DIRECT_TARGET_MISSING');
      if (!/^(0|[1-9]\d{0,39})$/.test(String(cursor)) || count !== COUNT) throw S.fail('DIRECT_RESPONSE_INVALID');
      const params = {...context.buildCommonParams(app, biz), language: app.language, from_page: 'user',
        coverFormat: biz.videoCoverSettings?.format || 2, enable_cache: false, video_encoding: 'dash',
        needPinnedItemIds: true, post_item_list_request_type: 0, secUid, cursor, count};
      const url = new URL(ENDPOINT);
      for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value == null ? '' : String(value));
      return url;
    }
    async function request(url, signal, debug) {
      const controller = new AbortController();
      const cancel = () => controller.abort();
      signal?.addEventListener('abort', cancel, {once: true});
      if (signal?.aborted) controller.abort();
      const timer = setTimeout(cancel, 30000);
      try {
        const response = await env.fetch(String(url), {credentials: 'include', signal: controller.signal});
        debug.httpStatus = response.status;
        if ([403,429].includes(response.status)) throw S.fail(`DIRECT_${response.status}`);
        if ([502,503,504].includes(response.status)) throw S.fail('DIRECT_NETWORK');
        if (!response.ok) throw S.fail('DIRECT_HTTP');
        const body = await response.text();
        // The reference identifies successful, zero-length responses as a challenge signal.
        if (!body || (response.headers.get('content-type')?.includes('text/html') && /captcha|challenge|verify/i.test(body))) throw S.fail('DIRECT_CHALLENGE');
        let data;
        try { data = JSON.parse(body); } catch { throw S.fail('DIRECT_RESPONSE_INVALID'); }
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw S.fail('DIRECT_RESPONSE_INVALID');
        debug.tiktokStatusCode = S.integer(data.statusCode) ? data.statusCode : null;
        debug.itemCount = Array.isArray(data.itemList) ? data.itemList.length : 0;
        debug.cursorPresent = data.cursor !== undefined && data.cursor !== null && data.cursor !== '';
        debug.hasMore = data.hasMore === true || data.hasMore === 1;
        if (data.type === 'verify' || data.type === 'captcha' || data.type === 'challenge') throw S.fail('DIRECT_CHALLENGE');
        if (data.statusCode !== 0) throw S.fail('DIRECT_TIKTOK_STATUS');
        if (!Array.isArray(data.itemList) || ![true,false,0,1].includes(data.hasMore) ||
            (typeof data.cursor !== 'string' && !Number.isSafeInteger(data.cursor))) throw S.fail('DIRECT_RESPONSE_INVALID');
        return data;
      } catch (error) {
        if (signal?.aborted) throw S.fail('DIRECT_CANCELLED');
        if (error?.code && S.codes.includes(error.code)) throw error;
        throw S.fail('DIRECT_NETWORK');
      } finally {
        // Full delay after completion is intentionally more conservative than reference Un().
        nextRequestAt = Date.now() + DELAY_MS;
        clearTimeout(timer);
        signal?.removeEventListener('abort', cancel);
      }
    }
    async function scan({target, signal, onProgress = () => {}, onItem = () => {}}) {
      const start = Date.now(), videos = new Map(), cursors = new Set(['0']);
      let page = 0, cursor = '0', emptyPages = 0, debug = S.debug();
      const progress = phase => onProgress({phase, total: videos.size, page, elapsedMs: Date.now() - start, debug: S.debug(debug)});
      const check = username => {
        if (signal?.aborted) throw S.fail('DIRECT_CANCELLED');
        if (context.targetUsername() !== username) throw S.fail('DIRECT_TARGET_MISSING');
      };
      try {
        if (![50,100,200].includes(target)) throw S.fail('DIRECT_INTERNAL');
        progress('preparing');
        const username = context.targetUsername();
        if (!username) throw S.fail('DIRECT_TARGET_MISSING');
        // Fail closed before any authenticated API request when login is not established.
        if (!context.isLoggedIn()) throw S.fail('DIRECT_LOGIN_REQUIRED');
        const app = await context.getAppContext(signal);
        check(username);
        if (!context.isLoggedIn(app)) throw S.fail('DIRECT_LOGIN_REQUIRED');
        const profile = context.getTargetProfileData(username);
        if (!profile?.secUid) throw S.fail('DIRECT_TARGET_MISSING');
        while (videos.size < target) {
          check(username);
          if (!context.isLoggedIn(app)) throw S.fail('DIRECT_LOGIN_REQUIRED');
          page++;
          let data;
          for (let attempt = 0; ; attempt++) {
            const delay = Math.max(0, nextRequestAt - Date.now());
            if (delay) { progress('waiting'); await sleep(delay, signal); }
            check(username);
            debug = S.debug({page});
            progress('requesting');
            try {
              data = await request(buildAuthorItemsRequest({secUid: profile.secUid, cursor, count: COUNT}, app), signal, debug);
              break;
            } catch (error) {
              progress('received');
              if (error.code !== 'DIRECT_NETWORK' || attempt >= MAX_RETRIES) throw error;
            }
          }
          check(username);
          const before = videos.size;
          for (const item of data.itemList) {
            const video = N.normalize(item);
            if (video && video.author.toLowerCase() === username.toLowerCase() && videos.size < target) { videos.set(video.id, video); onItem(item, video); }
          }
          progress('received');
          emptyPages = videos.size === before ? emptyPages + 1 : 0;
          if (!debug.hasMore || videos.size >= target) break;
          const next = String(data.cursor);
          if (!/^[1-9]\d{0,39}$/.test(next) || cursors.has(next) || emptyPages >= 3 || page >= 1000) throw S.fail('DIRECT_CURSOR_STALLED');
          cursors.add(next);
          cursor = next;
        }
        return {videos: [...videos.values()], cancelled: false, elapsedMs: Date.now() - start};
      } catch (error) {
        if (signal?.aborted || error?.code === 'DIRECT_CANCELLED') return {videos: [...videos.values()], cancelled: true, elapsedMs: Date.now() - start};
        throw S.fail(S.safeError(error));
      }
    }
    return Object.freeze({buildAuthorItemsRequest, scan});
  }
  root.KurukinAuthorItems = Object.freeze({createCollector, wait, ENDPOINT, COUNT, DELAY_MS, MAX_RETRIES});
  if (typeof module !== 'undefined') module.exports = root.KurukinAuthorItems;
})(globalThis);
