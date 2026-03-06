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

// Pass through all fetch requests without caching
self.addEventListener('fetch', function(event) {
  // Just pass through to network - no caching
  event.respondWith(fetch(event.request));
});