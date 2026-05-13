/* ex-memory PWA Service Worker — 离线缓存 */

const CACHE_NAME = 'ex-memory-v1';
const STATIC_FILES = [
    '/',
    '/static/style.css',
    '/static/app.js',
    '/static/manifest.json',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_FILES))
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    // API 请求不走缓存
    if (event.request.url.includes('/api/')) return;

    event.respondWith(
        caches.match(event.request).then(cached =>
            cached || fetch(event.request).then(response => {
                if (response.ok && event.request.method === 'GET') {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                }
                return response;
            })
        )
    );
});
