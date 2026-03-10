// Minimal service worker for PWA compliance
const SW_VERSION = '2.0';

self.addEventListener('install', function(event) {
  console.log('Service Worker v' + SW_VERSION + ': Installing...');
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  console.log('Service Worker v' + SW_VERSION + ': Activating...');
  event.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.map(function(name) { return caches.delete(name); }));
    }).then(function() { return self.clients.claim(); })
  );
});

// No fetch handler -- let all requests pass through to the network naturally.
// Intercepting with event.respondWith(fetch(event.request)) can strip
// Authorization headers on some browsers (especially mobile).