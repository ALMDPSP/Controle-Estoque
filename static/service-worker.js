const CACHE = 'controle-estoque-mobile-v4';
const STATIC = [
  '/static/favicon.ico?v=4',
  '/static/favicon-32.png?v=4',
  '/static/app-icon-192.png?v=4',
  '/static/app-icon-512.png?v=4',
  '/static/mobile-icon-192.png?v=4',
  '/static/mobile-icon-512.png?v=4',
  '/static/manifest.webmanifest?v=4'
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/static/')) return;
  event.respondWith(caches.match(event.request).then(hit => hit || fetch(event.request).then(resp => {
    const copy = resp.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return resp;
  }).catch(() => hit)));
});
