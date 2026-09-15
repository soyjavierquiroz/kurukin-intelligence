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
    async function scan({target, signal, onProgress = () => {}, onItem = () => {}, onCheckpoint = null, checkpointSize = 50, resume = null}) {
      const start = Date.now(), videos = new Map(), cursors = new Set();
      const fullChannel = target === 'full', limit = fullChannel ? null : target;
      const restored=resume&&typeof resume==='object'?resume:null;
      const resumeValid=typeof restored?.cursor==='string'&&/^(0|[1-9]\d{0,39})$/.test(restored.cursor);
      // A stale cursor falls back to a complete safe replay.  The backend
      // upsert boundary keeps already-confirmed snapshots intact.
      const base=resumeValid&&Number.isSafeInteger(restored?.discoveredCount)&&restored.discoveredCount>=0&&(fullChannel||restored.discoveredCount<=limit)?restored.discoveredCount:0;
      let page = 0, cursor = resumeValid?restored.cursor:'0', emptyPages = 0, debug = S.debug(), checkpointNumber=resumeValid&&Number.isSafeInteger(restored?.checkpointNumber)?restored.checkpointNumber:0;
      const restoredSeen=new Set(resumeValid&&Array.isArray(restored?.seenIds)?restored.seenIds.filter(id=>/^\d{5,30}$/.test(id)):[]), pending=[];
      cursors.add(cursor);
      const progress = phase => onProgress({phase, total: base + videos.size, page, elapsedMs: Date.now() - start, debug: S.debug(debug)});
      const check = username => {
        if (signal?.aborted) throw S.fail('DIRECT_CANCELLED');
        if (context.targetUsername() !== username) throw S.fail('DIRECT_TARGET_MISSING');
      };
      try {
        if (!(fullChannel||Number.isSafeInteger(limit)&&limit>=1&&limit<=200)||!Number.isSafeInteger(checkpointSize)||checkpointSize<1||checkpointSize>50) throw S.fail('DIRECT_INTERNAL');
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
        while (limit === null || base + videos.size < limit) {
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
          const before = videos.size, pageCursor=cursor, pageSeen=[], pendingAtPageStart=pending.length;
          for (const item of data.itemList) {
            const video = N.normalize(item);
            if (!video || video.author.toLowerCase() !== username.toLowerCase() || (limit !== null && base + videos.size >= limit) || restoredSeen.has(video.id)) continue;
            if (!videos.has(video.id)) {videos.set(video.id, video);pending.push(video);pageSeen.push(video.id);onItem(item, video);}
          }
          progress('received');
          emptyPages = videos.size === before ? emptyPages + 1 : 0;
          // Emit after a complete page.  This makes an exact final batch a
          // durable completed checkpoint, while `seenIds` still covers only
          // the page prefix actually persisted (not the in-memory tail).
          if (onCheckpoint && pending.length >= checkpointSize) {
            const persistedFromPage = Math.max(0, checkpointSize - pendingAtPageStart);
            checkpointNumber++;
            await onCheckpoint({videos:pending.splice(0,checkpointSize),checkpointNumber,checkpointCount:checkpointNumber,discoveredCount:base+videos.size-(pending.length),target,resume:{cursor:pageCursor,seenIds:pageSeen.slice(0,persistedFromPage),discoveredCount:base+videos.size-(pending.length),checkpointNumber},hasMore:debug.hasMore,complete:!debug.hasMore && pending.length===0});
          }
          if (!debug.hasMore || (limit !== null && base + videos.size >= limit)) break;
          const next = String(data.cursor);
          if (!/^[1-9]\d{0,39}$/.test(next) || cursors.has(next) || emptyPages >= 3) throw S.fail('DIRECT_CURSOR_STALLED');
          cursors.add(next);
          cursor = next;
          restoredSeen.clear();
        }
        if(onCheckpoint&&pending.length){checkpointNumber++;await onCheckpoint({videos:pending.splice(0),checkpointNumber,checkpointCount:checkpointNumber,discoveredCount:base+videos.size,target,resume:{cursor:debug.hasMore?cursor:'0',seenIds:[],discoveredCount:base+videos.size,checkpointNumber},hasMore:debug.hasMore,complete:true});}
        return {videos: [...videos.values()], cancelled: false, elapsedMs: Date.now() - start};
      } catch (error) {
        if (signal?.aborted || error?.code === 'DIRECT_CANCELLED') return {videos: [...videos.values()], cancelled: true, elapsedMs: Date.now() - start};
        throw S.fail(S.safeError(error));
      }
    }
    async function reacquire({ids, signal, onItem = () => {}, maxPages = 8}) {
      const wanted=new Set(Array.isArray(ids)?ids.filter(id=>/^\d{5,30}$/.test(id)).slice(0,10):[]);
      if(!wanted.size||!Number.isSafeInteger(maxPages)||maxPages<1||maxPages>8) throw S.fail('DIRECT_INTERNAL');
      const username=context.targetUsername();
      if(!username) throw S.fail('DIRECT_TARGET_MISSING');
      if(!context.isLoggedIn()) throw S.fail('DIRECT_LOGIN_REQUIRED');
      const app=await context.getAppContext(signal);
      if(context.targetUsername()!==username||!context.isLoggedIn(app)) throw S.fail('DIRECT_LOGIN_REQUIRED');
      const profile=context.getTargetProfileData(username);
      if(!profile?.secUid) throw S.fail('DIRECT_TARGET_MISSING');
      let cursor='0', pages=0;
      while(wanted.size&&pages<maxPages){
        if(signal?.aborted) throw S.fail('DIRECT_CANCELLED');
        if(context.targetUsername()!==username) throw S.fail('DIRECT_TARGET_MISSING');
        const delay=Math.max(0,nextRequestAt-Date.now());if(delay) await sleep(delay,signal);
        const debug=S.debug({page:pages+1}),data=await request(buildAuthorItemsRequest({secUid:profile.secUid,cursor,count:COUNT},app),signal,debug);
        pages++;
        for(const item of data.itemList){const video=N.normalize(item);if(video&&video.author.toLowerCase()===username.toLowerCase()&&wanted.delete(video.id))onItem(item,video);}
        if(!debug.hasMore) break;
        const next=String(data.cursor);if(!/^[1-9]\d{0,39}$/.test(next)) break;cursor=next;
      }
      return {found:[...ids].filter(id=>!wanted.has(id)),missing:[...wanted],pages};
    }
    return Object.freeze({buildAuthorItemsRequest, scan, reacquire});
  }
  root.KurukinAuthorItems = Object.freeze({createCollector, wait, ENDPOINT, COUNT, DELAY_MS, MAX_RETRIES});
  if (typeof module !== 'undefined') module.exports = root.KurukinAuthorItems;
})(globalThis);
