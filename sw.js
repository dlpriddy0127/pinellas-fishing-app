// Service worker: lets the app install to the home screen and open fast. Weather data is always fetched live.
const CACHE = 'fishing-app-v1';
const SHELL = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Live data (weather, tides, clarity): network first, saved copy only if offline
  const live = url.hostname !== location.hostname || url.pathname.endsWith('clarity.json');
  if (url.hostname === location.hostname && !url.pathname.endsWith('clarity.json')) {
    // the app itself: show the saved copy instantly and refresh it in the background
    e.respondWith(caches.match(req).then(hit => {
      const fresh = fetch(req).then(r => { if (r.ok) caches.open(CACHE).then(c => c.put(req, r.clone())); return r; }).catch(() => hit);
      return hit || fresh;
    }));
    return;
  }
  if (live) {
    e.respondWith(fetch(req).then(r => {
      if (r.ok && (url.pathname.endsWith('clarity.json') || /cdnjs|jsdelivr|unpkg/.test(url.hostname))) caches.open(CACHE).then(c => c.put(req, r.clone()));
      return r;
    }).catch(() => caches.match(req)));
  }
});
