const CACHE = "botfarm-v1";
self.addEventListener("install", (e) => {
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (e) => {
  // network-first; never cache API/WS calls
  if (e.request.url.includes("/api/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
