/* ex-memory PWA Service Worker — Network-First API + Stale-While-Revalidate 静态资源 */

const CACHE_VERSION = 'v6';
const CACHE_NAME = `ex-memory-${CACHE_VERSION}`;
const API_CACHE_NAME = `ex-memory-api-${CACHE_VERSION}`;

const STATIC_FILES = [
    '/',
    '/static/style.css',
    '/static/app.js',
    '/static/offline.html',
    '/static/manifest.json',
    '/static/icon-192.svg',
    '/static/icon-512.svg',
];

// ── 安装时预缓存静态资源 ──
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(STATIC_FILES))
            .then(() => self.skipWaiting())
    );
});

// ── 清理旧版本缓存 ──
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(k => k !== CACHE_NAME && k !== API_CACHE_NAME)
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

// ── 获取请求处理 ──
self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // 只处理 GET 请求
    if (event.request.method !== 'GET') return;

    // API 请求：Network-First，离线回退缓存
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(networkFirstWithCache(event.request));
        return;
    }

    // 静态资源：Stale-While-Revalidate
    event.respondWith(staleWhileRevalidate(event.request));
});

// ── Network-First + API 缓存回退 ──
async function networkFirstWithCache(request) {
    const cache = await caches.open(API_CACHE_NAME);
    try {
        const response = await fetch(request);
        if (response.ok) {
            cache.put(request, response.clone());
        }
        return response;
    } catch {
        const cached = await cache.match(request);
        if (cached) return cached;
        // 无缓存时返回离线 JSON
        return new Response(
            JSON.stringify({ error: '网络连接已断开，请稍后重试' }),
            { status: 503, headers: { 'Content-Type': 'application/json' } }
        );
    }
}

// ── Stale-While-Revalidate ──
async function staleWhileRevalidate(request) {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);

    const fetchPromise = fetch(request).then(response => {
        if (response.ok) {
            cache.put(request, response.clone());
        }
        return response;
    }).catch(() => cached || caches.match('/static/offline.html'));

    return cached || fetchPromise;
}
