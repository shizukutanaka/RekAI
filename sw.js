'use strict';
/*
 * RekAI — Service Worker（PWA / 共有シート受け口）
 *
 * 役割は2つだけ:
 * 1. アプリ本体（シェル）を端末にキャッシュし、オフラインでも起動できる
 *    ようにする（ネットワーク優先なので表示が古くならない）。
 * 2. 他のアプリからの「共有」を受け取る（Web Share Target API）。
 *    共有された画像は、この端末の中の専用キャッシュ（受信箱）に一度だけ
 *    置かれ、アプリ本体が取り込んだら消される。端末の外へは一切送らない。
 *    受け渡しにサーバーは使わない（静的配信のまま動く）。
 *
 * http(s) 配信時のみ登録される（index.html 側が判定）。file:// で開いた
 * 場合は従来どおり SW なしで動き、共有受け口も無効（OS の共有先に出ない）。
 */
var SW_VERSION = '1.0.0+1';
var SHELL_CACHE = 'rekai-shell-' + SW_VERSION;
/* 受信箱キャッシュ名とキーは index.html の REKAI CORE の定数と一致させる
   （tests/run-tests.js が両者の一致を検証する）。 */
var INBOX_CACHE = 'rekai-share-inbox';
var INBOX_IMAGE_KEY = '__rekai_shared_image';
var INBOX_META_KEY = '__rekai_shared_meta';
var SHARE_ACTION_SUFFIX = '/share-target';
var APP_SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './icon-512-maskable.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then(function (cache) { return cache.addAll(APP_SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.map(function (k) {
          /* 旧シェルだけを消す。受信箱（共有された画像）は絶対に消さない。 */
          if (k.indexOf('rekai-shell-') === 0 && k !== SHELL_CACHE) return caches.delete(k);
          return null;
        }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function inboxUrl(name) {
  return new URL(name, self.registration.scope).href;
}

function appUrl() {
  return new URL('index.html?shared=1', self.registration.scope).href;
}

/* 共有 POST の処理: 画像とテキストを取り出して受信箱へ置き、アプリへ転送する。
   何があってもアプリは開く（中身が空なら、アプリ側が理由を表示する）。 */
function handleShare(request) {
  return request.formData().then(function (fd) {
    var jobs = [];
    var file = null;
    try { file = fd.get('image'); } catch (e) { file = null; }
    if (file && typeof file === 'object' && typeof file.size === 'number' && file.size > 0 &&
        typeof file.type === 'string' && file.type.indexOf('image/') === 0) {
      jobs.push(caches.open(INBOX_CACHE).then(function (cache) {
        return cache.put(inboxUrl(INBOX_IMAGE_KEY),
          new Response(file, { headers: { 'Content-Type': file.type } }));
      }));
    }
    var meta = {};
    ['title', 'text', 'url'].forEach(function (k) {
      var v = null;
      try { v = fd.get(k); } catch (e) { v = null; }
      if (typeof v === 'string' && v) meta[k] = v;
    });
    if (Object.keys(meta).length > 0) {
      jobs.push(caches.open(INBOX_CACHE).then(function (cache) {
        return cache.put(inboxUrl(INBOX_META_KEY),
          new Response(JSON.stringify(meta), { headers: { 'Content-Type': 'application/json' } }));
      }));
    }
    return Promise.all(jobs);
  }).catch(function () {
    /* 解析に失敗しても黙って落とさない: アプリを開き、側で理由を表示させる */
  }).then(function () {
    return Response.redirect(appUrl(), 303);
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  var url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return; /* 外部リクエストは素通し */

  if (req.method === 'POST') {
    if (url.pathname.slice(-SHARE_ACTION_SUFFIX.length) === SHARE_ACTION_SUFFIX) {
      event.respondWith(handleShare(req));
    }
    return; /* それ以外の POST には一切関与しない */
  }
  if (req.method !== 'GET') return;

  /* 同一オリジンの GET: ネットワーク優先、失敗したらキャッシュ（オフライン起動） */
  event.respondWith(
    fetch(req).then(function (res) {
      if (res && res.ok && url.pathname.slice(-SHARE_ACTION_SUFFIX.length) !== SHARE_ACTION_SUFFIX) {
        var copy = res.clone();
        caches.open(SHELL_CACHE).then(function (cache) { cache.put(req, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(req, { ignoreSearch: true }).then(function (hit) {
        if (hit) return hit;
        if (req.mode === 'navigate') return caches.match(new URL('index.html', self.registration.scope).href);
        throw new Error('offline and not cached');
      });
    })
  );
});
