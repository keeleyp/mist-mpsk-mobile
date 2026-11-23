// Mist MPSK Mobile service worker.
// Pages hold live keys, so they are never cached: network only, with an
// offline page when the server can't be reached. Static files are network-first
// too (so updates show at once), with the cached copy only used offline.
const CACHE = "mpsk-static-v2";
const STATIC = ["/offline", "/static/style.css", "/static/app.js", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(url.pathname, copy));
          }
          return resp;
        })
        .catch(() => caches.match(url.pathname))
    );
  }
});
