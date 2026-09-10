/* Only fixed codes and explicitly selected diagnostics may cross worlds. */
(function (root) {
  'use strict';
  const codes = Object.freeze(['DIRECT_403', 'DIRECT_429', 'DIRECT_TIKTOK_STATUS',
    'DIRECT_CHALLENGE', 'DIRECT_CONTEXT_MISSING', 'DIRECT_TARGET_MISSING',
    'DIRECT_NETWORK', 'DIRECT_RESPONSE_INVALID', 'DIRECT_CURSOR_STALLED',
    'DIRECT_LOGIN_REQUIRED', 'DIRECT_BUSY', 'DIRECT_CANCELLED', 'DIRECT_HTTP', 'DIRECT_INTERNAL']);
  function safeError(error) {
    try {
      const code = typeof error === 'string' ? error : error?.code;
      return codes.includes(code) ? code : 'DIRECT_INTERNAL';
    } catch { return 'DIRECT_INTERNAL'; }
  }
  const fail = code => Object.assign(new Error(safeError(code)), {code: safeError(code)});
  const integer = n => Number.isSafeInteger(n) && n >= 0;
  function debug(value = {}) {
    return {
      page: integer(value.page) ? value.page : 0,
      httpStatus: integer(value.httpStatus) ? value.httpStatus : null,
      tiktokStatusCode: integer(value.tiktokStatusCode) ? value.tiktokStatusCode : null,
      itemCount: integer(value.itemCount) ? value.itemCount : 0,
      cursorPresent: value.cursorPresent === true,
      hasMore: value.hasMore === true
    };
  }
  const api = Object.freeze({codes, safeError, fail, debug, integer});
  root.KurukinSecurity = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
