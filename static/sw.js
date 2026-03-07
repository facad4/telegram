// Minimal service worker for PWA compliance
// This service worker doesn't cache anything as requested

self.addEventListener('install', function(event) {
  console.log('Service Worker: Installing...');
  self.skipWaiting();
});

self.addEventListener('activate', function(event) {
  console.log('Service Worker: Activating...');
  event.waitUntil(self.clients.claim());
});

// No fetch handler -- let all requests pass through to the network naturally.
// Intercepting with event.respondWith(fetch(event.request)) can strip
// Authorization headers on some browsers (especially mobile).