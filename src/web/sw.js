// RECALL service worker: keeps the app shell (page, scripts, styles, fonts, icons) so the installed app opens
// while the server is unreachable. It never touches data: the API, event streams, logins, uploads, downloads,
// media and transcripts always go straight to the network. Served at /sw.js (scope /) by src/web/app.py, which
// replaces the version below so every release gets a fresh cache.
"use strict";

const CACHE_PREFIX = "recall-shell-";
const CACHE_NAME = CACHE_PREFIX + "__RECALL_VERSION__";

const SHELL = [
  "/",
  "/static/index.html",
  "/static/style.css",
  "/static/app.js",
  "/static/appearance.js",
  "/static/vendor/marked.umd.js",
  "/static/vendor/purify.min.js",
  "/static/vendor/panzoom.min.js",
  "/static/fonts/inter.woff2",
  "/static/fonts/fraunces.woff2",
  "/static/fonts/geist-mono.woff2",
  "/static/icons/manifest.webmanifest",
  "/static/icons/favicon-32.png",
  "/static/icons/apple-touch-icon-180.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/recall-logo.svg",
];

// What this worker must never answer or store, even if it were ever listed above.
const NEVER_PREFIXES = ["/api/", "/static/uploads/", "/static/media/"];

const OFFLINE_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RECALL</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 16px system-ui, sans-serif;
         background: #16181d; color: #e8e8ea; text-align: center; }
  p { opacity: .7; margin: .4em 0 0; }
</style></head>
<body><main><h1>RECALL</h1><p>Offline, reconnecting...</p></main>
<script>
  const retry = () => fetch("/", { cache: "no-store" }).then((r) => { if (r.ok) location.reload(); }).catch(() => {});
  addEventListener("online", retry);
  setInterval(retry, 5000);
</script></body></html>`;

function neverHandle(request, url) {
  if (request.method !== "GET") return true; // writes pass through untouched
  if (url.origin !== self.location.origin) return true;
  if (NEVER_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))) return true;
  if ((request.headers.get("accept") || "").includes("text/event-stream")) return true; // SSE
  if (request.headers.has("range")) return true; // media and downloads
  return false;
}

function isShellRequest(request, url) {
  if (request.mode === "navigate") return url.pathname === "/";
  return SHELL.includes(url.pathname);
}

// Only a plain, successful, same-origin answer without a cookie is worth keeping.
function cacheable(response) {
  return response.ok && response.type === "basic" && !response.headers.has("set-cookie")
    && !(response.headers.get("content-type") || "").includes("text/event-stream");
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL.map((path) => new Request(path, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n.startsWith(CACHE_PREFIX) && n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

async function networkFirst(request, url) {
  const cache = await caches.open(CACHE_NAME);
  const key = request.mode === "navigate" ? "/" : request;
  try {
    const response = await fetch(request);
    if (cacheable(response)) await cache.put(key, response.clone());
    else if ([502, 503, 504].includes(response.status)) {
      const kept = await cache.match(key);
      if (kept) return kept; // the proxy answers while the server is down
    }
    return response;
  } catch (err) {
    const kept = await cache.match(key);
    if (kept) return kept;
    if (request.mode === "navigate") {
      return new Response(OFFLINE_PAGE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (neverHandle(event.request, url) || !isShellRequest(event.request, url)) return; // the browser handles it
  event.respondWith(networkFirst(event.request, url));
});
