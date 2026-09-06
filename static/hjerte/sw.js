const CACHE = 'hjerte-public-v1';
const PUBLIC = ['/offline/', '/static/hjerte/app.css', '/static/hjerte/app.js', '/static/hjerte/icon-192.png', '/static/hjerte/icon-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(PUBLIC)));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('hjerte-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (PUBLIC.includes(url.pathname)) {
    event.respondWith(fetch(event.request).catch(() => caches.match(url.pathname)));
  } else if (event.request.mode === 'navigate') {
    // Private HTML, questions, results and source PDFs are never written to a cache.
    event.respondWith(fetch(event.request).catch(() => caches.match('/offline/')));
  }
});
