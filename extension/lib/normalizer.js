(function (root) {
  'use strict';
  const username = v => typeof v === 'string' && /^[\w.]{1,64}$/.test(v) ? v : null;
  const text = (v, max) => typeof v === 'string' ? v.slice(0, max) : '';
  function number(v, fallback = 0) {
    if (typeof v !== 'number' && !(typeof v === 'string' && /^\d+(\.\d+)?$/.test(v))) return fallback;
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 ? n : fallback;
  }
  const nullableText = (v, max) => typeof v === 'string' ? v.slice(0, max) : null;
  function musicMetadata(item) {
    // `music` is already present in the author item response. Deliberately read
    // only this public allowlist: never retain the music object or its URLs.
    const music = item?.music;
    return {
      music_id: nullableText(music?.id, 64),
      music_title: nullableText(music?.title, 512),
      music_author: nullableText(music?.authorName, 256),
      // Do not derive this from a label such as "original sound".
      music_original: typeof music?.original === 'boolean' ? music.original :
        typeof music?.isOriginal === 'boolean' ? music.isOriginal : null
    };
  }
  function normalize(item) {
    if (!item || item.imagePost || item.imagePostInfo || !item.video) return null;
    const id = typeof item.id === 'string' ? item.id : Number.isSafeInteger(item.id) ? String(item.id) : '';
    const author = username(item.author?.uniqueId);
    if (!/^\d{5,30}$/.test(id) || !author) return null;
    const stats = item.stats || {};
    return {id, author, nickname: text(item.author.nickname, 256), caption: text(item.desc, 10000),
      created_at: number(item.createTime), views: number(stats.playCount), likes: number(stats.diggCount),
      comments: number(stats.commentCount), shares: number(stats.shareCount), favorites: number(stats.collectCount),
      duration: number(item.video.duration, null), url: `https://www.tiktok.com/@${author}/video/${id}`,
      ...musicMetadata(item)};
  }
  // Reconstruct the public shape even when called with already validated results.
  function exportVideos(videos) {
    return videos.map(v => normalize({id: v.id, author: {uniqueId: v.author, nickname: v.nickname},
      desc: v.caption, createTime: v.created_at, stats: {playCount: v.views, diggCount: v.likes,
        commentCount: v.comments, shareCount: v.shares, collectCount: v.favorites}, video: {duration: v.duration},
      music: {id: v.music_id, title: v.music_title, authorName: v.music_author, original: v.music_original}})).filter(Boolean);
  }
  const api = Object.freeze({username, normalize, exportVideos, musicMetadata});
  root.KurukinNormalizer = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
