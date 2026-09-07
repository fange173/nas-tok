/* NasTok Service Worker — 只缓存应用壳;/api/** 与媒体流一律直连网络,绝不落盘。
   大文件/Range 请求严禁经 SW 缓存,否则 quota 会被打爆。 */
const CACHE = "nastok-shell-v1";
const SHELL = ["/", "/static/manifest.webmanifest", "/static/icon.svg", "/static/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const { request } = e;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;

  // API、视频流、分享流:直连网络,不缓存
  if (url.pathname.startsWith("/api/")) return;

  // JS 子模块:network-first。main.js?v=mtime 入口一变,子模块(无版本号)必须同批更新,
  // cache-first 会造成「新 main + 旧子模块」混合图导致白屏(评审 M1)
  if (url.pathname.startsWith("/static/js/") || url.pathname === "/static/style.css") {
    e.respondWith(
      fetch(request)
        .then((resp) => {
          if (resp.ok && resp.type === "basic") {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(request, copy));
          }
          return resp;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // 页面导航:网络优先,成功时回写壳缓存(否则离线回退的永远是安装时刻的旧 HTML,
  // 会引用已被淘汰的 main.js?v=旧时间戳),离线回退壳(由 SPA 自行处理未登录态)
  if (request.mode === "navigate") {
    e.respondWith(
      fetch(request)
        .then((resp) => {
          if (resp.ok && resp.type === "basic" && !resp.redirected) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put("/", copy));
          }
          return resp;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // 其余静态资源(CSS/图标/manifest):缓存优先 + 后台刷新
  e.respondWith(
    caches.match(request).then((cached) => {
      const refresh = fetch(request)
        .then((resp) => {
          if (resp.ok && resp.type === "basic" && !resp.redirected) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(request, copy));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || refresh;
    })
  );
});
