// A minimal service worker to enable the "Add to Home Screen" feature.
// This version does NOT cache any files. Its only purpose is to register a fetch handler.

self.addEventListener('fetch', event => {
  // A service worker's fetch handler is required for a site to be installable.
  // This basic handler just passes the request to the network.
  event.respondWith(fetch(event.request));
});
