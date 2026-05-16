/* ex-memory PWA Service Worker — Stale-While-Revalidate + 离线降级 */

const CACHE_NAME = 'ex-memory-v2';
const STATIC_FILES = [
    '/',
    '/static/style.css',
    '/static/app.js',
    '/static/offline.html',
    '/static/manifest.json',
];

// 安装时预缓存静态资源
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_FILES))
    );
    self.skipWaiting();
});

// 清理旧版本缓存
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);

    // API 请求不走缓存
    if (url.pathname.startsWith('/api/')) {
        // 离线时返回网络错误提示
        event.respondWith(
            fetch(event.request).catch(() =>
                new Response(JSON.stringify({ error: '网络连接已断开' }), {
                    status: 503,
                    headers: { 'Content-Type': 'application/json' }
                })
            )
        );
        return;
    }

    // 静态资源：Stale-While-Revalidate
    event.respondWith(
        caches.match(event.request).then(cached => {
            const fetchPromise = fetch(event.request).then(response => {
                // 成功时更新缓存
                if (response.ok && event.request.method === 'GET') {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                }
                return response;
            }).catch(() => {
                // 网络失败且无缓存时，返回离线页面
                if (!cached) {
                    return caches.match('/static/offline.html');
                }
                return cached;
            });

            // 有缓存立即返回，同时后台更新
            return cached || fetchPromise;
        })
    );
});
