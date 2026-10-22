/* shell.js — 应用外壳:侧栏 / 底部导航 / 手机顶部标签 / 设置页入口 / 宽屏信息栏 */
import { openAdmin } from './admin.js';
import { isStaff, openChangePassword } from './auth.js';
import { applyFilter, currentTabValue, downloadUrl, goTo, pauseAll, playCurrent, refreshLibraryOptions, showFeedToast, streamUrl, syncAccountUi, syncFeedTabSelect } from './feed.js';
import { MEDIA_MODE_ICON } from './icons.js';
import { openSearch, posterAllowed, releasePosters } from './search.js';
import { shareVideo, unshareVideo } from './share-page.js';
import { albumImageCount, currentItem, itemKind, state } from './state.js';
import { openTagModal } from './tags.js';
import { $, $$, api, copyText, escapeHtml, fmtBytes, fmtDateTime, fmtUtc, fmtTime, isMobileFeed, showPage } from './util.js';

    const KIND_LABEL = { video: "视频", image: "图片", album: "相册" };
    // 视图名 → 页面 id
    const VIEW_PAGE = { feed: "page-feed", library: "page-search", me: "page-me", admin: "page-admin" };

    /** 切换主区视图 */
    export function setView(view) {
      if (view === "feed") {
        showPage("page-feed");
        playCurrent();
      } else if (view === "library") {
        openSearch({ focus: !isMobileFeed() });
      } else if (view === "me") {
        pauseAll();
        showPage("page-me");
        syncAccountUi();
        refreshLibraryOptions();
        $("#me-status").textContent = "";
      } else if (view === "admin") {
        openAdmin();
      }
    }

    /* ---------- 侧栏 / 底部导航 ---------- */
    $(".sidebar")?.addEventListener("click", (e) => {
      const el = e.target.closest("[data-filter], [data-view]");
      if (!el) return;
      if (el.dataset.filter) applyFilter(el.dataset.filter);
      else setView(el.dataset.view);
    });
    $("#tabbar")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-view]");
      if (btn) setView(btn.dataset.view);
    });

    /** 页面切换后同步导航高亮;离开播放页时暂停,离开媒体库时释放封面视频 */
    document.addEventListener("nastok:page", (e) => {
      const id = e.detail;
      const view = Object.keys(VIEW_PAGE).find((k) => VIEW_PAGE[k] === id) || null;
      $$(".sidebar [data-view]").forEach((el) => {
        if (view && el.dataset.view === view) el.setAttribute("aria-current", "page");
        else el.removeAttribute("aria-current");
      });
      $$("#tabbar [data-view]").forEach((el) => {
        const on = el.dataset.view === view || (el.dataset.view === "me" && view === "admin");
        if (on) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
      });
      syncFeedTabSelect();
      if (view && view !== "feed") pauseAll();
      if (view !== "library") releasePosters();
      if (view === "feed") renderInfoPanel();
    });

    /* ---------- 手机顶部标签 ---------- */
    $("#feed-tabs")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-filter]");
      if (!btn) return;
      const f = btn.dataset.filter;
      if (f !== "tagged") return applyFilter(f);
      const cur = currentTabValue();
      const inTagMode = cur === "__all__" || cur.startsWith("tag:");
      // 已在标记筛选或还没有标记时,弹出标记列表供选择
      if (inTagMode || !(state.tags || []).length) openTagFilter();
      else applyFilter("__all__");
    });
    function openTagFilter() {
      $("#tag-filter-modal").classList.add("open");
    }
    function closeTagFilter() {
      $("#tag-filter-modal").classList.remove("open");
    }
    $("#tag-filter-list")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-filter]");
      if (!btn) return;
      closeTagFilter();
      applyFilter(btn.dataset.filter);
    });
    $("#tag-filter-close")?.addEventListener("click", closeTagFilter);
    $("#tag-filter-modal")?.addEventListener("click", (e) => {
      if (e.target === e.currentTarget) closeTagFilter();
    });
    $("#m-search-btn")?.addEventListener("click", () => openSearch({ focus: true }));

    /* ---------- 设置页 ---------- */
    $("#me-admin")?.addEventListener("click", () => setView("admin"));
    $("#me-pwd")?.addEventListener("click", () => openChangePassword(true));
    $("#me-help")?.addEventListener("click", () => {
      setView("feed");
      $("#help-overlay").classList.remove("hidden");
    });

    /* ---------- 宽屏信息栏 ---------- */
    const panel = $("#info-panel");
    const tagCache = new Map();
    const posterCache = new Map();
    let _infoGen = 0;

    function panelVisible() {
      return !!panel && getComputedStyle(panel).display !== "none";
    }

    /** 按当前条目刷新信息栏;不可见时跳过,避免多余请求 */
    export function renderInfoPanel() {
      if (!panelVisible()) return;
      const gen = ++_infoGen;
      const v = currentItem();
      $("#ip-title").textContent = v ? v.title || v.path : "暂无内容";
      $("#ip-sub").textContent = v ? [KIND_LABEL[itemKind(v)], v.library].filter(Boolean).join(" · ") : "";
      renderKv(v);
      const dl = $("#ip-download");
      dl.classList.toggle("hidden", !v || itemKind(v) === "album");
      if (v && itemKind(v) !== "album") dl.href = downloadUrl(v);
      renderQueue();
      renderTags(v, gen);
      renderShare(v, gen);
    }

    function renderKv(v) {
      const kv = $("#ip-kv");
      if (!v) {
        kv.innerHTML = "";
        return;
      }
      const rows = [["路径", v.path || "—"], ["修改", fmtDateTime(v.mtime || v.mtime_iso)], ["大小", fmtBytes(v.size || 0)]];
      const kind = itemKind(v);
      if (kind === "album") rows.splice(1, 0, ["图片", `${albumImageCount(v)} 张`]);
      if (kind === "video" && state.currentVideo && state.currentVideo.duration) rows.splice(1, 0, ["时长", fmtTime(state.currentVideo.duration)]);
      kv.innerHTML = rows.map(([k, val]) => `<dt>${k}</dt><dd>${escapeHtml(String(val))}</dd>`).join("");
    }

    async function renderTags(v, gen) {
      const box = $("#ip-tags");
      const addBtn = '<button type="button" class="chip chip-add" id="ip-tag-edit">+ 标记</button>';
      if (!v) {
        box.innerHTML = "";
        return;
      }
      const cached = tagCache.get(v.path);
      if (cached) {
        box.innerHTML = cached.map((n) => `<span class="chip">${escapeHtml(n)}</span>`).join("") + addBtn;
        return;
      }
      box.innerHTML = addBtn;
      try {
        const data = await api(`/api/videos/tags?path=${encodeURIComponent(v.path)}`);
        const names = (data.items || []).filter((t) => t.assigned).map((t) => t.name);
        tagCache.set(v.path, names);
        if (gen === _infoGen) box.innerHTML = names.map((n) => `<span class="chip">${escapeHtml(n)}</span>`).join("") + addBtn;
      } catch (_) { /* 标记读取失败时只保留添加入口 */ }
    }
    $("#ip-tags")?.addEventListener("click", (e) => {
      if (e.target.closest("#ip-tag-edit") && currentItem()) openTagModal(currentItem());
    });
    document.addEventListener("nastok:tags", (e) => {
      tagCache.delete(e.detail);
      renderInfoPanel();
    });

    async function renderShare(v, gen) {
      const sec = $("#ip-share-sec");
      const box = $("#ip-share");
      sec.classList.toggle("hidden", !v || !isStaff());
      if (!v || !isStaff()) return;
      if (!v.shared) {
        box.innerHTML = '<div class="ip-share-state"><span class="badge badge-off">未分享</span></div><div class="ip-btns"><button type="button" class="btn btn-ghost btn-sm" data-share="create">创建分享链接</button></div>';
        return;
      }
      box.innerHTML = '<div class="ip-share-state"><span class="badge badge-on">已分享</span></div>';
      try {
        const data = await api(`/api/admin/shares?${new URLSearchParams({ path: v.path, limit: "5" })}`);
        if (gen !== _infoGen) return;
        const s = (data.items || []).find((it) => it.is_active) || (data.items || [])[0];
        if (!s) return;
        const url = location.origin + s.url;
        const bits = [`打开 ${s.view_count} 次`, s.expires_at ? `有效期至 ${fmtUtc(s.expires_at)}` : "永久有效"];
        if (s.has_password) bits.push("需密码");
        box.innerHTML = `<div class="ip-share-state"><span class="badge ${s.is_active ? "badge-on" : "badge-off"}">${s.is_active ? "已分享" : "已停用"}</span><span>${escapeHtml(bits.join(" · "))}</span></div>`
          + `<a class="ip-share-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url)}</a>`
          + `<div class="ip-btns"><button type="button" class="btn btn-ghost btn-sm" data-share="copy" data-url="${escapeHtml(url)}">复制链接</button><button type="button" class="btn btn-danger-ghost btn-sm" data-share="revoke">取消分享</button></div>`;
      } catch (_) { /* 详情拉取失败时保留「已分享」状态 */ }
    }
    $("#ip-share")?.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-share]");
      const v = currentItem();
      if (!btn || !v) return;
      const act = btn.dataset.share;
      if (act === "create") await shareVideo(v);
      else if (act === "revoke") await unshareVideo(v);
      else if (act === "copy") showFeedToast((await copyText(btn.dataset.url)) ? "分享链接已复制" : btn.dataset.url, 2000);
      document.dispatchEvent(new CustomEvent("nastok:item"));
    });

    function queueThumb(it) {
      const kind = itemKind(it);
      if (kind === "image") return `<img src="${streamUrl(it)}" alt="" loading="lazy" />`;
      if (kind === "album" && it.cover) return `<img src="${streamUrl({ path: it.cover })}" alt="" loading="lazy" />`;
      return (MEDIA_MODE_ICON[kind === "album" ? "images" : "video"] || MEDIA_MODE_ICON.video)();
    }

    /** 视频首帧封面元素按路径缓存复用,避免每次切换条目重复请求 */
    function videoPoster(it) {
      if (!posterAllowed() || it.playable === false) return null;
      let el = posterCache.get(it.path);
      if (!el) {
        el = document.createElement("video");
        el.muted = true;
        el.playsInline = true;
        el.preload = "metadata";
        el.src = streamUrl(it).split("?")[0] + "#t=1";
        posterCache.set(it.path, el);
        if (posterCache.size > 30) {
          const [oldPath, oldEl] = posterCache.entries().next().value;
          oldEl.removeAttribute("src");
          oldEl.load();
          posterCache.delete(oldPath);
        }
      }
      return el;
    }

    function renderQueue() {
      const box = $("#ip-queue");
      const next = state.videos.slice(state.index + 1, state.index + 6);
      if (!next.length) {
        box.innerHTML = `<div class="queue-empty">${state.hasMore ? "正在加载更多…" : "已经是最后一条了"}</div>`;
        return;
      }
      box.innerHTML = next.map((it, k) => {
        const kind = itemKind(it);
        const sub = kind === "album" ? `相册 · ${albumImageCount(it)} 张` : KIND_LABEL[kind] || "视频";
        return `<button type="button" class="q-item" data-go="${state.index + 1 + k}"><span class="q-thumb">${queueThumb(it)}</span><span class="q-text"><b>${escapeHtml(it.title || "")}</b><small>${escapeHtml(sub)}</small></span></button>`;
      }).join("");
      next.forEach((it, k) => {
        if (itemKind(it) !== "video") return;
        const el = videoPoster(it);
        const holder = box.querySelector(`[data-go="${state.index + 1 + k}"] .q-thumb`);
        if (el && holder) holder.appendChild(el);
      });
    }
    $("#ip-queue")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-go]");
      if (!btn) return;
      goTo(Number(btn.dataset.go), true);
      playCurrent();
    });

    document.addEventListener("nastok:item", () => renderInfoPanel());
    let _resizeT = 0;
    window.addEventListener("resize", () => {
      clearTimeout(_resizeT);
      _resizeT = setTimeout(renderInfoPanel, 200);
    });
