/* feed.js — Feed 列表/播放/滑动/轮滚/键盘/帮助(由 split_frontend.py 机械切割,勿手改顺序) */
import { openAdmin } from './admin.js';
import { isStaff, isSysadmin } from './auth.js';
import { ERROR_ICON, ICON, PLAY_ICON } from './icons.js';
import { bindGlobalProgress, bindMediaSession, bumpChrome, ensureAlbumImages, exitImmersive, fillAlbumTrack, getCurrentVideo, resumeWatchPos, revealChrome, saveWatchPos, setAlbumIndex, setMediaMode, setPlaybackRate, setPlayMode, setProgressUI, setDockTitle, syncMediaLabel, updateAlbumProgress, updateDockForKind } from './player.js';
import { setShareMenuItem, shareVideo, unshareVideo } from './share-page.js';
import { MEDIA_MODES, NEXT_PAGE_PREFETCH_THRESHOLD, VIDEO_KEEP_BEHIND, VIDEO_METADATA_AHEAD, albumImageCount, currentItem, itemKind, state } from './state.js';
import { openTagModal, refreshTagCache } from './tags.js';
import { $, $$, api, escapeHtml, fmtDateTime, isMobileFeed, lsGet, lsSet, showPage } from './util.js';

    /* ---------- Feed 标签 / 排序 ---------- */
    const KIND_LABEL = { video: "视频", image: "图片", album: "相册" };

    /** 登录后进入 Feed：装配入口可见性并首次加载（供 main.js/auth.js 调用） */
    export function enterFeed() {
      $("#admin-link").classList.toggle("hidden", !isStaff());
      $("#admin-link-m")?.classList.toggle("hidden", !isStaff());
      $("#backup-download-btn")?.classList.toggle("hidden", !isSysadmin());
      $("#backup-restore-label")?.classList.toggle("hidden", !isSysadmin());
      showPage("page-feed");
      setPlayMode(state.playMode, false);
      state.tagId = null;
      state.tagged = false;
      resetAndLoad();
      refreshTagCache().catch(() => {});
      if (!lsGet("nastok_help_hint")) {
        lsSet("nastok_help_hint", "1");
        setTimeout(() => showFeedToast("按 ? 或点击顶栏「?」查看快捷键", 3200), 800);
      }
    }

    /** 由 state 推导下拉框当前值：tag:{id} / __all__ / fav / all */
    export function currentTabValue() {
      if (state.tagId) return `tag:${state.tagId}`;
      if (state.tagged) return "__all__";
      return state.tab === "fav" ? "fav" : "all";
    }

    export function syncFeedTabSelect() {
      const sel = $("#feed-tab-select");
      if (sel) sel.value = currentTabValue();
    }

    function applyTabSelect(val) {
      if (val === "all" || val === "fav") {
        state.tab = val;
        state.tagId = null;
        state.tagged = false;
      } else if (val === "__all__") {
        state.tab = null;
        state.tagId = null;
        state.tagged = true;
      } else if (val.startsWith("tag:")) {
        state.tab = null;
        state.tagId = Number(val.slice(4)) || null;
        state.tagged = false;
      } else {
        return;
      }
      // 任何筛选切换都退出搜索结果态
      state.query = "";
      state.seed = null;
      resetAndLoad();
    }
    $("#feed-tab-select")?.addEventListener("change", (e) => applyTabSelect(e.target.value));

    // 存储库切换：全部存储库 或 单一存储库；切库后回到“全部”筛选并重载
    export function switchLibrary(id) {
      const n = id ? Number(id) : null;
      if (n === state.libraryId) return;
      state.libraryId = n || null;
      lsSet("nastok_library", state.libraryId ? String(state.libraryId) : "");
      state.tab = "all";
      state.tagId = null;
      state.tagged = false;
      state.query = "";
      state.seed = null;
      syncFeedTabSelect();
      resetAndLoad();
    }
    export async function refreshLibraryOptions() {
      const sel = $("#library-select");
      if (!sel) return;
      try {
        const data = await api("/api/libraries");
        const libs = Array.isArray(data.items) ? data.items : [];
        sel.innerHTML =
          '<option value="">全部存储库</option>' +
          libs.map((l) => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join("");
        sel.value = state.libraryId ? String(state.libraryId) : "";
      } catch (_) { /* 下拉取库失败不阻塞，保持“全部存储库” */ }
    }
    $("#library-select")?.addEventListener("change", (e) => switchLibrary(e.target.value));
    $("#settings-btn")?.addEventListener("click", () => refreshLibraryOptions());

    $("#media-mode-btn")?.addEventListener("click", () => {
      const i = MEDIA_MODES.indexOf(state.mediaMode);
      const next = MEDIA_MODES[(i + 1) % MEDIA_MODES.length];
      setMediaMode(next, true);
    });

    // 播放顺序三态循环：顺序 → 随机 → 单个循环
    const PLAY_MODE_NEXT = { order: "random", random: "loop", loop: "order" };
    $("#dock-sort-btn")?.addEventListener("click", () => {
      setPlayMode(PLAY_MODE_NEXT[state.playMode] || "order", true);
    });
    $("#dock-mute-btn")?.addEventListener("click", () => {
      revealChrome();
      $("#mute-btn").click();
    });

    // 顶栏下拉菜单（PC 一个，移动端功能/账户两个），互斥开合
    export function closeTopMore(except) {
      $$(".top-more.open").forEach((wrap) => {
        if (wrap === except) return;
        wrap.classList.remove("open");
        wrap.querySelector(".top-more-btn")?.setAttribute("aria-expanded", "false");
      });
    }
    export function toggleTopMore(e) {
      e.stopPropagation();
      const wrap = e.currentTarget.closest(".top-more");
      if (!wrap) return;
      const open = !wrap.classList.contains("open");
      closeTopMore(wrap);
      wrap.classList.toggle("open", open);
      e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        $("#page-feed").classList.remove("chrome-dimmed");
        if (state._chromeTimer) {
          clearTimeout(state._chromeTimer);
          state._chromeTimer = 0;
        }
      } else {
        bumpChrome();
      }
    }
    $$(".top-more > .top-more-btn").forEach((btn) => btn.addEventListener("click", toggleTopMore));
    $$(".top-more-panel").forEach((panel) => {
      panel.addEventListener("click", (e) => {
        if (e.target.closest("button, a")) closeTopMore();
      });
    });
    document.addEventListener("click", (e) => {
      if (e.target.closest(".top-more")) return;
      closeTopMore();
    });

    export function showFeedToast(msg, ms, compact) {
      const el = $("#feed-toast");
      if (!el) return;
      el.textContent = msg;
      el.classList.toggle("compact", !!compact);
      el.classList.add("show");
      clearTimeout(el._t);
      el._t = setTimeout(() => el.classList.remove("show"), ms || 2200);
    }

    export function updateFeedCounter() {
      const el = $("#feed-counter");
      if (!el) return;
      const cur = state.videos.length ? state.index + 1 : 0;
      const total = state.total || state.videos.length || 0;
      const more = state.hasMore ? "+" : "";
      el.textContent = `${cur} / ${total}${more}`;
      el.title = state.hasMore
        ? `当前第 ${cur} 个，已加载 ${state.videos.length} 个，共约 ${total} 个（还可继续加载）`
        : `当前第 ${cur} 个，共 ${total} 个（已全部看完）`;
    }

    // 清空 slide 轨道前先中止在途视频请求（pause + 去 src + load()）；
    // 直接 innerHTML="" 会让 preload=auto 的下载持续到元素被 GC
    export function clearFeedTrack() {
      $$("#feed-track video").forEach(unmountVideoSrc);
      $("#feed-track").innerHTML = "";
    }

    export function resetAndLoad(forceRefresh) {
      state.loadGen += 1;
      state.videos = [];
      state.index = 0;
      state.page = 1;
      state.hasMore = true;
      state.viewed.clear();
      state.currentVideo = null;
      clearFeedTrack();
      bindGlobalProgress(null);
      updateFeedCounter();
      updateSearchExit();
      return loadVideos(true, !!forceRefresh);
    }

    /** 作废在途分页请求并复位加载状态。
     * 删除条目后会重算 state.page，若放任在途响应返回，其末尾的
     * state.page = page + 1 会用旧值覆盖重算结果；而 loadVideos 的
     * finally 只在同代时清理，gen 递增后需在这里补清 loading/loader。 */
    export function invalidatePendingLoad() {
      state.loadGen += 1;
      state.loading = false;
      state.loadPromise = null;
      $("#feed-loader").classList.add("hidden");
    }

    export async function loadVideos(initial, forceRefresh, quiet = false) {
      if (!initial && !state.hasMore) return state.videos;
      // 加载更多可复用进行中的请求；初始加载绝不能复用「加载更多」的 Promise
      if (!initial && state.loading && state.loadPromise) return state.loadPromise;

      const myGen = state.loadGen;
      state.loading = true;
      if (initial) {
        $("#feed-loader").classList.remove("hidden");
        $("#feed-empty").classList.add("hidden");
      } else if (!quiet) {
        showFeedToast("正在加载更多视频…", 1200);
      }

      state.loadPromise = (async () => {
        try {
          const page = initial ? 1 : state.page;
          const qs = new URLSearchParams({
            sort: state.sort,
            page: String(page),
            limit: String(state.limit),
            media_mode: state.mediaMode || "video",
          });
          if (state.sort === "random" && state.seed) qs.set("seed", state.seed);
          if (forceRefresh) qs.set("refresh", "true");
          if (state.tab === "fav") qs.set("favorites", "true");
          if (state.tagId) qs.set("tag_id", String(state.tagId));
          else if (state.tagged) qs.set("tagged", "true");
          if (state.libraryId) qs.set("library_id", String(state.libraryId));
          if (state.query) qs.set("q", state.query);
          const data = await api(`/api/videos/list?${qs}`);
          // 已被刷新/切 Tab 作废
          if (myGen !== state.loadGen) return data;

          if (data.seed) state.seed = data.seed;
          state.total = data.total;
          if (Array.isArray(data.library_ids)) state.lastLibraryIds = data.library_ids;

          if (!data.items.length && page === 1) {
            renderFeedEmpty();
            state.hasMore = false;
          } else {
            $("#feed-empty").classList.add("hidden");
          }

          if (!data.items.length) {
            state.hasMore = false;
          } else {
            // 客户端删除过条目后服务端分页窗口整体前移，可能重复下发已加载项；
            // 按 path 去重，只追加新条目
            const known = new Set(state.videos.map((it) => it && it.path).filter(Boolean));
            const fresh = data.items.filter((it) => !known.has(it.path));
            const startIdx = state.videos.length;
            state.videos.push(...fresh);
            if (fresh.length) renderSlides(startIdx);
            state.page = page + 1;
            if (state.videos.length >= data.total) state.hasMore = false;
          }

          if (initial && state.videos.length) {
            goTo(0, false);
            playCurrent();
          } else if (data.items.length) {
            syncVideoWindow();
            ensureUpcomingPage();
          }
          updateFeedCounter();
          return data;
        } catch (ex) {
          if (myGen !== state.loadGen) throw ex;
          if (ex.status === 403 && state.user && state.user.must_change_password) {
            showPage("page-change-pwd");
          } else if (ex.status === 401) {
            showPage("page-login");
          } else {
            console.error(ex);
            showFeedToast(ex.message || "加载视频失败，请重试", 2800);
            if (initial) renderFeedEmpty();
          }
          throw ex;
        } finally {
          if (myGen === state.loadGen) {
            state.loading = false;
            state.loadPromise = null;
            $("#feed-loader").classList.add("hidden");
          }
        }
      })();

      return state.loadPromise;
    }

    export function streamUrl(v) {
      const rel = (v && (v.path || "")) || "";
      // 按段编码，保留真实 `/`，避免整路径 %2F 在部分环境下歧义
      const encoded = rel
        .split("/")
        .filter((p) => p.length)
        .map((p) => encodeURIComponent(p))
        .join("/");
      const base = `/api/videos/stream/${encoded || (v && v.encoded_path) || ""}`;
      if (v && v.mtime != null && v.mtime !== "") return `${base}?v=${encodeURIComponent(v.mtime)}`;
      return base;
    }

    export function downloadUrl(v) {
      return `${streamUrl(v).split("?")[0]}?download=1`;
    }

    export async function diagnoseMediaError(url) {
      if (!url) return null;
      try {
        const res = await fetch(url, {
          method: "GET",
          credentials: "include",
          headers: { Range: "bytes=0-0" },
        });
        if (res.status === 401 || res.status === 403) {
          let detail = "";
          try {
            const data = await res.json();
            detail = (data && data.detail) || "";
          } catch (_) {}
          if (res.status === 401) return "登录已失效，请重新登录";
          return detail || "无权限播放，请重新登录";
        }
        if (res.status >= 400) return `加载失败（${res.status}）`;
        return null;
      } catch (_) {
        return "网络错误，无法加载";
      }
      return null;
    }

    export function bumpLoadGen(video) {
      const g = Number(video.dataset.loadGen || 0) + 1;
      video.dataset.loadGen = String(g);
      return g;
    }

    export function isVideoDead(video) {
      if (!video) return true;
      if (video.error) return true;
      // 有 src 却完全无资源
      try {
        if (video.getAttribute("src") && video.networkState === HTMLMediaElement.NETWORK_NO_SOURCE) return true;
      } catch (_) {}
      return false;
    }

    export function mountVideoSrc(video, url, { force = false, preload = "auto" } = {}) {
      if (!video || !url) return 0;
      const cur = video.getAttribute("src");
      if (!force && cur === url && !isVideoDead(video)) {
        video.preload = preload;
        video.muted = state.muted;
        video.volume = state.volume;
        return Number(video.dataset.loadGen || 0);
      }
      const gen = bumpLoadGen(video);
      video.preload = preload;
      video.muted = state.muted;
      video.volume = state.volume;
      if (cur) {
        try { video.pause(); } catch (_) {}
        video.removeAttribute("src");
        try { video.load(); } catch (_) {}
      }
      video.src = url;
      try { video.load(); } catch (_) {}
      return gen;
    }

    export function unmountVideoSrc(video) {
      if (!video) return;
      bumpLoadGen(video);
      try { video.pause(); } catch (_) {}
      video.playbackRate = 1;
      if (video.getAttribute("src")) {
        video.removeAttribute("src");
        try { video.load(); } catch (_) {}
      }
      video.preload = "none";
    }


    export function renderFeedEmpty() {
      const box = $("#feed-empty");
      const title = $("#feed-empty-title");
      const sub = $("#feed-empty-sub");
      const actions = $("#feed-empty-actions");
      box.classList.remove("hidden");
      actions.innerHTML = "";
      if (state.query) {
        title.textContent = "没有匹配的内容";
        sub.textContent = `没有找到「${state.query}」`;
      } else if (state.tab === "fav") {
        title.textContent = "还没有喜欢的视频";
        sub.textContent = "播放时点开底部「更多」即可喜欢，之后可在这里连续观看";
        const go = document.createElement("button");
        go.type = "button";
        go.className = "btn btn-primary btn-sm";
        go.textContent = "去推荐看看";
        go.addEventListener("click", () => {
          state.tab = "all";
          state.tagId = null;
          state.tagged = false;
          syncFeedTabSelect();
          state.seed = null;
          resetAndLoad();
        });
        actions.appendChild(go);
      } else {
        const noLibs = Array.isArray(state.lastLibraryIds) && state.lastLibraryIds.length === 0;
        title.textContent = noLibs ? "未分配存储库" : "暂无视频";
        sub.textContent = noLibs
          ? "请联系管理员为你分配可访问的存储库"
          : "请将视频放入挂载目录，并确认存储库已启用";
        if (isStaff()) {
          const go = document.createElement("button");
          go.type = "button";
          go.className = "btn btn-primary btn-sm";
          go.textContent = noLibs ? "去分配存储库" : "去配置存储库";
          go.addEventListener("click", () => openAdmin(noLibs ? "users" : "libs"));
          actions.appendChild(go);
        }
        const refresh = document.createElement("button");
        refresh.type = "button";
        refresh.className = "btn btn-ghost btn-sm";
        refresh.textContent = "重新扫描";
        refresh.addEventListener("click", () => doPullRefresh());
        actions.appendChild(refresh);
      }
    }

    export function removeSlideAt(idx) {
      // 先作废在途分页：下面重算的 state.page 不能被在途响应的旧值覆盖
      invalidatePendingLoad();
      state.videos.splice(idx, 1);
      state.total = Math.max(0, (state.total || 0) - 1);
      // 服务端按 page*limit 偏移分页：已加载列表变短后必须重算下一页，
      // 否则窗口起点落后 1 位，会有一条视频永远漏掉
      state.page = Math.floor(state.videos.length / state.limit) + 1;
      clearFeedTrack();
      if (!state.videos.length) {
        state.hasMore = false;
        renderFeedEmpty();
        bindGlobalProgress(null);
        updateFeedCounter();
        return;
      }
      renderSlides(0);
      state.index = Math.min(idx, state.videos.length - 1);
      goTo(state.index, false);
      playCurrent();
    }

    export async function toggleFavorite(v, btn, idx) {
      const path = v && v.path;
      if (!path || state._favInflight[path]) return;
      const isFav = btn.classList.contains("active");
      state._favInflight[path] = true;
      btn.disabled = true;
      try {
        if (isFav) {
          await api("/api/favorites", {
            method: "DELETE",
            body: JSON.stringify({ path: v.path }),
          });
          btn.classList.remove("active");
          btn.innerHTML = ICON.heart(false);
          if (state.videos[idx]) state.videos[idx].favorited = false;
          showFeedToast("已取消喜欢", 1400);
          if (state.tab === "fav") removeSlideAt(idx);
        } else {
          await api("/api/favorites", {
            method: "POST",
            body: JSON.stringify({ path: v.path }),
          });
          btn.classList.add("active");
          btn.innerHTML = ICON.heart(true);
          if (state.videos[idx]) state.videos[idx].favorited = true;
          showFeedToast("已加入喜欢", 1400);
        }
      } catch (ex) {
        showFeedToast(ex.message || "喜欢操作失败", 2500);
      } finally {
        btn.disabled = false;
        delete state._favInflight[path];
      }
    }

    /** 独立「喜欢」按钮：按当前条目的 favorited 刷新图标与高亮 */
    export function syncDockFav() {
      const btn = $("#dock-fav-btn");
      if (!btn) return;
      const v = currentItem();
      const fav = !!(v && v.favorited);
      btn.classList.toggle("active", fav);
      btn.innerHTML = ICON.heart(fav);
      btn.title = fav ? "取消喜欢" : "喜欢";
      btn.setAttribute("aria-label", fav ? "取消喜欢" : "喜欢");
      btn.disabled = !v;
    }

    /* ---------- 右下角操作区：更多菜单 + 信息弹窗 ---------- */

    function closeDockMenu() {
      $("#dock-more-menu")?.classList.add("hidden");
      $("#dock-more-btn")?.setAttribute("aria-expanded", "false");
    }

    /** 每次打开按当前条目重建菜单：标记 / 信息 / 分享 */
    function openDockMenu() {
      const menu = $("#dock-more-menu");
      const v = currentItem();
      if (!menu) return;
      if (!v) return;
      menu.innerHTML = "";

      const mkItem = (tag, cls) => {
        const el = document.createElement(tag);
        if (cls) el.className = cls;
        return el;
      };

      const tagItem = mkItem("button", "side-more-item");
      tagItem.type = "button";
      tagItem.innerHTML = `${ICON.tag(false)}<span>标记</span>`;
      tagItem.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeDockMenu();
        openTagModal(currentItem());
      });
      menu.appendChild(tagItem);

      const infoItem = mkItem("button", "side-more-item");
      infoItem.type = "button";
      infoItem.innerHTML = `${ICON.info()}<span>信息</span>`;
      infoItem.addEventListener("click", (ev) => {
        ev.stopPropagation();
        closeDockMenu();
        openInfoModal(currentItem());
      });
      menu.appendChild(infoItem);

      if (isStaff()) {
        const shareItem = mkItem("button", "side-more-item");
        shareItem.type = "button";
        setShareMenuItem(shareItem, !!v.shared);
        shareItem.addEventListener("click", async (ev) => {
          ev.stopPropagation();
          closeDockMenu();
          const cur = currentItem();
          if (!cur) return;
          if (shareItem.dataset.shared === "1") await unshareVideo(cur);
          else await shareVideo(cur);
        });
        menu.appendChild(shareItem);
      }

      menu.classList.remove("hidden");
      $("#dock-more-btn").setAttribute("aria-expanded", "true");
    }

    $("#dock-more-btn")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      revealChrome();
      const menu = $("#dock-more-menu");
      if (menu.classList.contains("hidden")) openDockMenu();
      else closeDockMenu();
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#dock-more")) closeDockMenu();
    });

    /* 独立「喜欢」按钮 */
    $("#dock-fav-btn")?.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      revealChrome();
      const v = currentItem();
      if (!v) return;
      await toggleFavorite(state.videos[state.index], $("#dock-fav-btn"), state.index);
      syncDockFav();
    });

    /* 「退出搜索」按钮：清空 state.query 后回到普通列表 */
    const exitLabel = ICON.back() + '<span class="btn-label">退出搜索</span>';
    if ($("#exit-search-btn")) $("#exit-search-btn").innerHTML = exitLabel;
    if ($("#exit-search-btn-m")) $("#exit-search-btn-m").innerHTML = exitLabel;
    const exitSearchClick = () => {
      revealChrome();
      exitSearchFeed({ reload: true });
    };
    $("#exit-search-btn")?.addEventListener("click", exitSearchClick);
    $("#exit-search-btn-m")?.addEventListener("click", exitSearchClick);

    /** 根据是否处于搜索过滤状态，切换两个「退出搜索」按钮的显隐 */
    export function updateSearchExit() {
      const active = !!state.query;
      $("#exit-search-btn")?.classList.toggle("hidden", !active);
      $("#exit-search-btn-m")?.classList.toggle("hidden", !active);
    }

    /**
     * 彻底退出搜索：清空过滤条件并（可选）重载回普通列表。
     * reload=true 时重置分页并强制重载普通列表（resetAndLoad 内部也会调用
     * updateSearchExit()）；仅想在 feed 侧切按钮显隐时传 reload:false。
     */
    export function exitSearchFeed({ reload }) {
      state.query = "";
      const input = $("#search-input");
      if (input) input.value = "";
      if (reload) resetAndLoad(true);
      else updateSearchExit();
    }

    export function openInfoModal(v) {
      if (!v) return;
      const kind = itemKind(v);
      $("#info-modal-title").textContent = v.title || (v.path || "").split("/").pop() || "媒体信息";
      const rows = [
        ["类型", KIND_LABEL[kind] || "视频"],
        ["路径", v.path || "—"],
        ["存储库", v.library || "—"],
        ["修改时间", fmtDateTime(v.mtime_iso || v.mtime)],
      ];
      if (kind === "album") rows.push(["图片数", String(albumImageCount(v))]);
      if (isStaff()) rows.push(["分享", v.shared ? "已分享" : "未分享"]);
      $("#info-modal-rows").innerHTML = rows
        .map(([k, val]) => `<div class="info-row"><span>${k}</span><b>${escapeHtml(String(val))}</b></div>`)
        .join("");
      const dl = $("#info-modal-download");
      if (kind === "album") {
        dl.classList.add("hidden");
      } else {
        dl.classList.remove("hidden");
        dl.href = downloadUrl(v);
        dl.setAttribute("download", (v.path || "").split("/").pop() || "");
      }
      fillShareDetail(v);
      $("#info-modal").classList.add("open");
    }

    /** 已分享（staff）时在信息弹窗底部补充分享详情：链接 / 有效期 / 密码 / 访问数 */
    async function fillShareDetail(v) {
      const holder = $("#info-modal-share-detail");
      if (!holder) return;
      if (!isStaff() || !v.shared) {
        holder.innerHTML = "";
        return;
      }
      holder.innerHTML = '<div class="info-row"><span>链接</span><b>加载中…</b></div>';
      try {
        const qs = new URLSearchParams({ path: v.path || "", limit: "5" });
        const data = await api(`/api/admin/shares?${qs}`);
        const s = (data.items || []).find((it) => it.is_active) || (data.items || [])[0];
        if (!s) {
          holder.innerHTML = "";
          return;
        }
        const abs = location.origin + s.url;
        const bits = [`<a href="${escapeHtml(abs)}" target="_blank" rel="noopener" class="info-share-link">${escapeHtml(abs)}</a>`];
        bits.push(`访问 ${s.view_count} 次`);
        bits.push(s.expires_at ? `有效期至 ${escapeHtml(s.expires_at)}` : "永久有效");
        if (s.has_password) bits.push("需密码访问");
        if (!s.is_active) bits.push("已停用");
        holder.innerHTML = `<div class="info-row"><span>链接</span><b>${bits.join("<br>")}</b></div>`;
      } catch (_) {
        holder.innerHTML = "";
      }
    }
    $("#info-modal-close")?.addEventListener("click", () => {
      $("#info-modal").classList.remove("open");
    });
    $("#info-modal")?.addEventListener("click", (e) => {
      if (e.target === e.currentTarget) e.currentTarget.classList.remove("open");
    });


    export function buildStillSlide(slide, v, i, kind) {
      const pauseInd = document.createElement("div");
      pauseInd.className = "pause-indicator";
      pauseInd.innerHTML = `<div class="pause-badge">${PLAY_ICON}</div>`;

      if (kind === "album") {
        const albumTrack = document.createElement("div");
        albumTrack.className = "album-track";
        // 恢复上次停留的图片位置（slide 被窗口修剪重建后不丢失）
        slide.dataset.albumIdx = String(v._albumIdx > 0 ? Math.floor(v._albumIdx) : 0);
        slide.appendChild(albumTrack);
        const images = Array.isArray(v.images) ? v.images : null;
        if (images && images.length) {
          fillAlbumTrack(slide, v, images);
        } else if (images && !images.length) {
          fillAlbumTrack(slide, v, []);
        } else {
          // images 未随列表下发 → 按需拉取（仅当 slide 仍在 DOM 且仍指向同一条目时填充）
          const loadingCell = document.createElement("div");
          loadingCell.className = "album-slide";
          loadingCell.innerHTML = '<div class="spinner" style="margin:auto"></div>';
          albumTrack.appendChild(loadingCell);
          ensureAlbumImages(i).then((imgs) => {
            if (!slide.isConnected) return;
            if (state.videos[i] !== v) return;
            if (imgs === null) {
              // 拉取失败：显示可重试占位（下次重建 slide 会再拉），不缓存为「相册为空」
              albumTrack.innerHTML =
                '<div class="album-slide"><div class="error-msg" style="color:#fff;text-align:center">图片加载失败<br>稍后重试</div></div>';
              return;
            }
            fillAlbumTrack(slide, v, imgs || []);
          });
        }
      } else {
        const img = document.createElement("img");
        img.className = "feed-image";
        img.alt = v.title || "";
        img.draggable = false;
        img.loading = "lazy";
        img.src = streamUrl(v);
        slide.appendChild(img);
      }

      const meta = document.createElement("div");
      meta.className = "video-meta";
      meta.innerHTML = `<h2>${escapeHtml(v.title)}</h2>`;

      const zones = document.createElement("div");
      zones.className = "tap-zones";
      const left = document.createElement("div");
      left.className = "tap-zone left-zone";
      const mid = document.createElement("div");
      mid.className = "tap-zone mid-zone";
      const right = document.createElement("div");
      right.className = "tap-zone right-zone";
      zones.append(left, mid, right);

      const onStillTap = (sideName) => (e) => {
        e.stopPropagation();
        if (Date.now() < state.ignoreTapUntil) return;
        if (isMobileFeed()) {
          revealChrome();
          return;
        }
        if (kind === "album" && sideName === "left") {
          setAlbumIndex(Number(slide.dataset.index), Number(slide.dataset.albumIdx || 0) - 1);
          revealChrome();
          return;
        }
        if (kind === "album" && sideName === "right") {
          setAlbumIndex(Number(slide.dataset.index), Number(slide.dataset.albumIdx || 0) + 1);
          revealChrome();
          return;
        }
        revealChrome();
      };
      left.addEventListener("click", onStillTap("left"));
      mid.addEventListener("click", onStillTap("mid"));
      right.addEventListener("click", onStillTap("right"));

      slide.append(pauseInd, zones, meta);
    }

    // 视频 slide 的完整构建（含全部事件绑定）；供 renderSlides 与 ensureSlide 复用
    export function buildVideoSlide(slide, v, i) {
      const kind = "video";
      {

        const video = document.createElement("video");
        video.setAttribute("playsinline", "");
        video.setAttribute("webkit-playsinline", "");
        video.preload = "none";
        video.loop = false;
        video.muted = state.muted;
        video.volume = state.volume;
        video.playbackRate = state.playbackRate || 1;
        video.dataset.path = v.path;
        if (v.playable === false) {
          const badge = document.createElement("div");
          badge.className = "unplayable-badge";
          badge.textContent = "浏览器可能无法播放，请下载";
          slide.appendChild(badge);
        }
        // src 由 syncVideoWindow 按邻接窗口挂载/卸载

        video.addEventListener("ended", () => {
          if (String(state.index) !== slide.dataset.index) return;
          // 单个循环：播完当前重播
          if (state.playMode === "loop") {
            video.currentTime = 0;
            video.play().catch(() => {});
            return;
          }
          // 最后一条且没有更多：循环当前；否则切下一条
          if (!state.hasMore && state.index >= state.videos.length - 1) {
            video.currentTime = 0;
            video.play().catch(() => {});
            showFeedToast("已循环播放最后一条", 1600);
          } else {
            nextVideo();
          }
        });

        const pauseInd = document.createElement("div");
        pauseInd.className = "pause-indicator";
        pauseInd.innerHTML = `<div class="pause-badge">${PLAY_ICON}</div>`;

        const loader = document.createElement("div");
        loader.className = "loader-overlay hidden";
        loader.innerHTML = '<div class="spinner"></div><div class="loader-text">加载中…</div>';

        const netBadge = document.createElement("div");
        netBadge.className = "net-badge hidden";
        netBadge.innerHTML = `${ICON.signalWeak()}<span>网络不稳</span>`;

        const errBox = document.createElement("div");
        errBox.className = "error-overlay";
        errBox.innerHTML = `
          <div class="error-icon">${ERROR_ICON}</div>
          <div class="error-title">播放失败</div>
          <div class="error-msg">视频加载出错，请重试</div>
          <button type="button" class="btn btn-primary btn-sm retry-btn">重新加载</button>`;

        const showLoader = (text) => {
          netBadge.classList.add("hidden");
          loader.querySelector(".loader-text").textContent = text || "加载中…";
          loader.classList.remove("hidden");
          errBox.classList.remove("show");
        };
        const hideLoader = () => loader.classList.add("hidden");
        const showNetBadge = () => {
          // 还能继续播时只在角落提示，不挡画面
          if (!loader.classList.contains("hidden")) return;
          if (errBox.classList.contains("show")) return;
          netBadge.classList.remove("hidden");
        };
        const hideNetBadge = () => netBadge.classList.add("hidden");
        const showError = (msg) => {
          hideLoader();
          hideNetBadge();
          errBox.querySelector(".error-msg").textContent = msg || "视频加载出错，请重试";
          errBox.classList.add("show");
          updatePauseIndicator(slide, video);
        };
        const hideError = () => errBox.classList.remove("show");

        errBox.querySelector(".retry-btn").addEventListener("click", (ev) => {
          ev.stopPropagation();
          hideError();
          showLoader("重新加载…");
          const info = state.videos[Number(slide.dataset.index)];
          const src = info ? streamUrl(info) : video.getAttribute("src");
          if (!src) return;
          mountVideoSrc(video, src, { force: true });
          if (String(state.index) === slide.dataset.index) {
            const gen = state.playGen;
            video.play().catch(() => {
              if (gen !== state.playGen) return;
              showError("无法播放，请重试");
            });
          }
        });

        const canSoftWarn = () => {
          // 已有可播数据 / 正在播 / 有缓冲进度 → 不占满屏
          if (video.readyState >= 2) return true;
          if (!video.paused && video.currentTime > 0.2) return true;
          try {
            if (video.buffered && video.buffered.length > 0) {
              const end = video.buffered.end(video.buffered.length - 1);
              if (end > video.currentTime + 0.5) return true;
            }
          } catch (_) {}
          return false;
        };

        const eventGenOk = () => {
          // 卸载/重挂后忽略过期媒体事件
          return !!video.getAttribute("src");
        };

        video.addEventListener("loadstart", () => {
          if (!eventGenOk()) return;
          hideNetBadge();
          showLoader("加载中…");
          updatePauseIndicator(slide, video);
        });
        video.addEventListener("waiting", () => {
          if (!eventGenOk()) return;
          if (state.scrubbing) { hideNetBadge(); return; }
          if (canSoftWarn()) showNetBadge();
          else showLoader("缓冲中…");
          updatePauseIndicator(slide, video);
        });
        video.addEventListener("stalled", () => {
          if (!eventGenOk()) return;
          if (state.scrubbing) { hideNetBadge(); return; }
          if (canSoftWarn()) showNetBadge();
          else showLoader("网络不稳…");
        });
        video.addEventListener("canplay", () => {
          if (!eventGenOk()) return;
          hideLoader();
          hideNetBadge();
          hideError();
          updatePauseIndicator(slide, video);
        });
        video.addEventListener("playing", () => {
          if (!eventGenOk()) return;
          hideLoader();
          hideNetBadge();
          hideError();
          updatePauseIndicator(slide, video);
          if (video === state.currentVideo) { syncVideoWindow(); syncDesktopPlayerControls(); }
        });
        video.addEventListener("progress", () => {
          if (!eventGenOk()) return;
          if (!video.paused && video.readyState >= 3) hideNetBadge();
        });
        video.addEventListener("pause", () => updatePauseIndicator(slide, video));
        video.addEventListener("play", () => updatePauseIndicator(slide, video));
        video.addEventListener("error", () => {
          const gen = Number(video.dataset.loadGen || 0);
          // 等当前同步栈结束，避免卸载 abort / 紧接着重挂被误判
          queueMicrotask(async () => {
            if (Number(video.dataset.loadGen) !== gen) return;
            if (!video.getAttribute("src")) return;
            if (!video.error) return;
            if (video.readyState >= 2) return;
            const code = video.error.code;
            if (code === 1) {
              hideLoader();
              return;
            }
            const src = video.getAttribute("src");
            const authMsg = await diagnoseMediaError(src);
            if (Number(video.dataset.loadGen) !== gen) return;
            if (authMsg) {
              showError(authMsg);
              return;
            }
            const map = {
              2: "网络错误，无法加载",
              3: "视频解码失败",
              4: "格式不支持或地址无效",
            };
            showError(map[code] || "视频加载出错，请重试");
          });
        });

        const meta = document.createElement("div");
        meta.className = "video-meta";
        meta.innerHTML = `<h2>${escapeHtml(v.title)}</h2>`;

        const zones = document.createElement("div");
        zones.className = "tap-zones";
        const left = document.createElement("div");
        left.className = "tap-zone left-zone";
        left.dataset.flash = "−10s";
        const mid = document.createElement("div");
        mid.className = "tap-zone mid-zone";
        const right = document.createElement("div");
        right.className = "tap-zone right-zone";
        right.dataset.flash = "+10s";
        zones.append(left, mid, right);

        // 左/右：仅桌面单击快退/快进；移动端任意点按 = 播放/暂停
        // 中间：播放/暂停
        // 长按倍速结束后短暂忽略点击，避免误触暂停
        const onZonePointer = (sideName) => (e) => {
          e.stopPropagation();
          if (Date.now() < state.ignoreTapUntil) return;
          if (state.isFastForward) return;

          if (isMobileFeed()) {
            if ($("#page-feed").classList.contains("chrome-dimmed")) {
              revealChrome();
              return;
            }
            revealChrome();
            togglePlayPause(video, slide);
            return;
          }

          if (sideName === "left") {
            seekBy(video, -10, left);
            return;
          }
          if (sideName === "right") {
            seekBy(video, 10, right);
            return;
          }
          revealChrome();
          togglePlayPause(video, slide);
        };
        left.addEventListener("click", onZonePointer("left"));
        mid.addEventListener("click", onZonePointer("mid"));
        right.addEventListener("click", onZonePointer("right"));

        const startFF = (e) => {
          if (e.type === "mousedown" && e.button !== 0) return;
          const pt = e.touches && e.touches[0] ? e.touches[0] : e;
          state._ffStartX = pt.clientX;
          state._ffStartY = pt.clientY;
          clearTimeout(state.longPressTimer);
          state.longPressTimer = setTimeout(() => {
            state.isFastForward = true;
            video.playbackRate = 2;
            if (video.paused) video.play().catch(() => {});
            $("#speed-toast").textContent = "2x";
            $("#speed-toast").classList.add("show");
          }, 420);
        };
        const endFF = () => {
          clearTimeout(state.longPressTimer);
          if (state.isFastForward) {
            state.isFastForward = false;
            video.playbackRate = state.playbackRate || 1;
            $("#speed-toast").classList.remove("show");
            // 松手后的 click 会误触发暂停，短暂屏蔽
            state.ignoreTapUntil = Date.now() + 450;
          }
        };
        zones.addEventListener("touchstart", startFF, { passive: true });
        zones.addEventListener("touchend", endFF);
        zones.addEventListener("touchcancel", endFF);
        zones.addEventListener("mousedown", startFF);
        zones.addEventListener("mouseup", endFF);
        zones.addEventListener("mouseleave", endFF);
        zones.addEventListener("touchmove", (e) => {
          if (!state.longPressTimer && !state.isFastForward) return;
          const t = e.touches && e.touches[0];
          if (!t || state._ffStartX == null) return;
          const dx = t.clientX - state._ffStartX;
          const dy = t.clientY - state._ffStartY;
          if (dx * dx + dy * dy > 12 * 12) {
            clearTimeout(state.longPressTimer);
            if (state.isFastForward) endFF();
          }
        }, { passive: true });

        slide.append(video, pauseInd, loader, netBadge, errBox, zones, meta);
        updatePauseIndicator(slide, video);
      }
    }

    export function renderSlides(fromIdx) {
      const track = $("#feed-track");
      for (let i = fromIdx; i < state.videos.length; i++) {
        const v = state.videos[i];
        const kind = itemKind(v);
        const slide = document.createElement("div");
        slide.className = "video-slide";
        slide.dataset.index = String(i);
        slide.dataset.kind = kind;
        slide.style.transform = `translateY(${i * 100}%)`;
        if (kind === "image" || kind === "album") {
          buildStillSlide(slide, v, i, kind);
        } else {
          buildVideoSlide(slide, v, i);
        }
        track.appendChild(slide);
      }
      // 新建 DOM 后立即按窗口挂载邻接 src
      syncVideoWindow();
    }

    export function updatePauseIndicator(slide, video) {
      if (!slide || !video) return;
      const ind = slide.querySelector(".pause-indicator");
      if (!ind) return;
      const loading = slide.querySelector(".loader-overlay:not(.hidden)");
      const errored = slide.querySelector(".error-overlay.show");
      if (video.paused && !loading && !errored) ind.classList.add("show");
      else ind.classList.remove("show");
    }

    export function togglePlayPause(video, slide) {
      if (!video) return;
      if (isVideoDead(video)) {
        const info = state.videos[Number((slide || video.closest(".video-slide")).dataset.index)];
        if (info) {
          mountVideoSrc(video, streamUrl(info), { force: true });
          const gen = ++state.playGen;
          const idx = state.index;
          video.play().then(() => {
            if (gen !== state.playGen || idx !== state.index) return;
            updatePauseIndicator(slide || video.closest(".video-slide"), video);
          }).catch(() => {
            if (gen !== state.playGen || idx !== state.index) return;
            updatePauseIndicator(slide || video.closest(".video-slide"), video);
          });
          return;
        }
      }
      if (video.paused) {
        const gen = ++state.playGen;
        const idx = state.index;
        video.play().then(() => {
          if (gen !== state.playGen || idx !== state.index) return;
          bumpChrome();
          updatePauseIndicator(slide || video.closest(".video-slide"), video);
          syncDesktopPlayerControls();
        }).catch(() => {
          if (gen !== state.playGen || idx !== state.index) return;
          updatePauseIndicator(slide || video.closest(".video-slide"), video);
          syncDesktopPlayerControls();
        });
      } else {
        video.pause();
        $("#page-feed").classList.remove("chrome-dimmed");
        if (state._chromeTimer) {
          clearTimeout(state._chromeTimer);
          state._chromeTimer = 0;
        }
        updatePauseIndicator(slide || video.closest(".video-slide"), video);
        syncDesktopPlayerControls();
      }
    }

    export function syncDesktopPlayerControls() {
      const prevBtn = $("#pc-prev-btn");
      const ppBtn = $("#pc-playpause-btn");
      const nextBtn = $("#pc-next-btn");
      if (!prevBtn || !ppBtn || !nextBtn) return;
      const video = getCurrentVideo();
      const info = currentItem();
      const kind = info ? itemKind(info) : "video";
      const isVideo = kind === "video";
      const isImage = kind === "image" || kind === "album";

      // prev/next 图标在 init 处设定一次即可，这里只管理 disabled/播放图标
      // 上一条：首条时禁用；下一条：始终可用（handler 内部处理边界）
      prevBtn.disabled = state.index <= 0;
      nextBtn.disabled = !state.hasMore && state.index >= state.videos.length - 1;

      // 播放/暂停
      let ppIcon, ppTitle, ppPressed, ppDisabled;
      if (!video || !isVideo || isVideoDead(video)) {
        ppIcon = ICON.play();
        ppTitle = isImage ? "无法播放图片" : (isVideo ? "加载中…" : "无法播放");
        ppPressed = "false";
        ppDisabled = isImage || !isVideo;
      } else if (!video.paused) {
        ppIcon = ICON.pause();
        ppTitle = "暂停";
        ppPressed = "true";
        ppDisabled = false;
      } else {
        ppIcon = ICON.play();
        ppTitle = "播放";
        ppPressed = "false";
        ppDisabled = false;
      }
      ppBtn.innerHTML = ppIcon;
      ppBtn.title = ppTitle;
      ppBtn.setAttribute("aria-label", ppTitle);
      ppBtn.setAttribute("aria-pressed", ppPressed);
      ppBtn.disabled = ppDisabled;
    }

    export function seekBy(video, delta, zoneEl) {
      if (!video.duration || Number.isNaN(video.duration)) return;
      const wasPaused = video.paused;
      video.currentTime = Math.min(video.duration, Math.max(0, video.currentTime + delta));
      // 快进/快退不改变播放状态
      if (!wasPaused && video.paused) {
        video.play().catch(() => {});
      }
      const toast = $("#seek-toast");
      toast.textContent = delta > 0 ? `快进 ${delta}s` : `快退 ${Math.abs(delta)}s`;
      toast.classList.add("show");
      clearTimeout(toast._t);
      toast._t = setTimeout(() => toast.classList.remove("show"), 700);
      if (zoneEl) {
        zoneEl.classList.add("seek-flash");
        clearTimeout(zoneEl._flashT);
        zoneEl._flashT = setTimeout(() => zoneEl.classList.remove("seek-flash"), 350);
      }
      // 屏蔽紧随其后的多余 click
      state.ignoreTapUntil = Date.now() + 280;
      updatePauseIndicator(video.closest(".video-slide"), video);
    }

    export function shouldAutoPreloadNext() {
      const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      if (connection && (connection.saveData || connection.effectiveType === "slow-2g" || connection.effectiveType === "2g")) {
        return false;
      }
      const current = state.currentVideo;
      return !!current && !current.paused && current.readyState >= 2 && !isVideoDead(current);
    }

    export function ensureUpcomingPage() {
      const remaining = state.videos.length - state.index - 1;
      if (state.hasMore && remaining < NEXT_PAGE_PREFETCH_THRESHOLD) {
        loadVideos(false, false, true).catch(() => {});
      }
    }

    export function goTo(idx, animate) {
      if (idx < 0 || idx >= state.videos.length) return;
      state.index = idx;
      closeDockMenu();
      const track = $("#feed-track");
      if (!animate) track.style.transition = "none";
      track.style.transform = `translateY(${-idx * 100}%)`;
      if (!animate) {
        void track.offsetHeight;
        track.style.transition = "";
      }
      updateFeedCounter();
      updateNeighborPreload();
      ensureUpcomingPage();
      revealChrome();
    }

    // 距离当前过远的 slide 直接从 DOM 移除（state.videos 保留），
    // 回到附近时由 syncVideoWindow 按需重建，避免长 feed 无限堆积节点。
    export const SLIDE_WINDOW_BEHIND = 4;
    export const SLIDE_WINDOW_AHEAD = 6;

    export function removeSlide(slide) {
      const video = slide.querySelector("video");
      if (video) {
        try { video.pause(); } catch (_) {}
        unmountVideoSrc(video);
      }
      slide.remove();
    }

    export function pruneSlides() {
      $$("#feed-track .video-slide").forEach((slide) => {
        const i = Number(slide.dataset.index);
        if (i < state.index - SLIDE_WINDOW_BEHIND || i > state.index + SLIDE_WINDOW_AHEAD) {
          removeSlide(slide);
        }
      });
    }

    export function ensureSlide(i) {
      let slide = $(`#feed-track .video-slide[data-index="${i}"]`);
      if (slide) return slide;
      const v = state.videos[i];
      if (!v) return null;
      const track = $("#feed-track");
      const kind = itemKind(v);
      slide = document.createElement("div");
      slide.className = "video-slide";
      slide.dataset.index = String(i);
      slide.dataset.kind = kind;
      slide.style.transform = `translateY(${i * 100}%)`;
      if (kind === "image" || kind === "album") {
        buildStillSlide(slide, v, i, kind);
      } else {
        buildVideoSlide(slide, v, i);
      }
      // 按 index 顺序插入正确位置
      let ref = null;
      for (const child of track.children) {
        if (Number(child.dataset.index) > i) { ref = child; break; }
      }
      track.insertBefore(slide, ref);
      return slide;
    }

    export function syncVideoWindow() {
      pruneSlides();
      for (let k = Math.max(0, state.index - SLIDE_WINDOW_BEHIND);
           k <= Math.min(state.videos.length - 1, state.index + SLIDE_WINDOW_AHEAD); k++) {
        ensureSlide(k);
      }
      const autoPreloadNext = shouldAutoPreloadNext();
      $$("#feed-track .video-slide").forEach((slide) => {
        const i = Number(slide.dataset.index);
        const info = state.videos[i];
        if (!info || itemKind(info) !== "video") return;
        const video = slide.querySelector("video");
        if (!video) return;
        const ahead = i - state.index;
        const want = streamUrl(info);
        let preload = null;
        if (ahead === 0) preload = "auto";
        else if (ahead === 1) preload = autoPreloadNext ? "auto" : "metadata";
        else if ((ahead < 0 && ahead >= -VIDEO_KEEP_BEHIND) || (ahead >= 2 && ahead <= VIDEO_METADATA_AHEAD)) preload = "metadata";

        if (preload) {
          const force = isVideoDead(video) && !!video.getAttribute("src");
          mountVideoSrc(video, want, { force, preload });
        } else if (video.getAttribute("src") || video.error) {
          unmountVideoSrc(video);
          const loader = slide.querySelector(".loader-overlay");
          if (loader) loader.classList.add("hidden");
          const netBadge = slide.querySelector(".net-badge");
          if (netBadge) netBadge.classList.add("hidden");
          const errBox = slide.querySelector(".error-overlay");
          if (errBox) errBox.classList.remove("show");
          updatePauseIndicator(slide, video);
        }
      });
    }

    export function updateNeighborPreload() {
      syncVideoWindow();
    }

    export function pauseAll() {
      $$("#feed-track video").forEach((v) => {
        if (!v.getAttribute("src")) return;
        v.pause();
        v.playbackRate = 1;
        updatePauseIndicator(v.closest(".video-slide"), v);
      });
      state.currentVideo = null;
    }

    export function playCurrent() {
      syncVideoWindow();
      const slide = $(`#feed-track .video-slide[data-index="${state.index}"]`);
      const info = state.videos[state.index];
      if (!slide || !info) {
        bindGlobalProgress(null);
        state.currentVideo = null;
        updateDockForKind("video");
        setDockTitle("");
        return;
      }
      const kind = itemKind(info);
      updateDockForKind(kind);
      setDockTitle(info.title || "");
      syncDockFav();

      // 只暂停上一条视频
      if (state.currentVideo) {
        try {
          state.currentVideo.pause();
          state.currentVideo.playbackRate = 1;
          updatePauseIndicator(state.currentVideo.closest(".video-slide"), state.currentVideo);
        } catch (_) {}
      }
      state.currentVideo = null;

      if (kind !== "video") {
        bindGlobalProgress(null);
        const ind = slide.querySelector(".pause-indicator");
        if (ind) ind.classList.remove("show");
        if (kind === "image") {
          setProgressUI(100);
          syncMediaLabel(1, 1);
        } else {
          const idx = Number(slide.dataset.albumIdx || 0);
          updateAlbumProgress(slide, idx, albumImageCount(info));
        }
        updateFeedCounter();
        bumpChrome();
        syncDesktopPlayerControls();
        const path = info.path;
        if (path && !state.viewed.has(path)) {
          state.viewed.add(path);
          api("/api/videos/view", {
            method: "POST",
            body: JSON.stringify({ path }),
          }).catch(() => {});
        }
        return;
      }

      const video = slide.querySelector("video");
      if (!video) {
        bindGlobalProgress(null);
        return;
      }

      if (info) {
        // 出错或空挂载时强制重挂，避免「有播放键但点了没反应」
        mountVideoSrc(video, streamUrl(info), { force: isVideoDead(video) });
      }
      video.muted = state.muted;
      video.volume = state.volume;

      const errBox = slide.querySelector(".error-overlay");
      const loader = slide.querySelector(".loader-overlay");
      const playGen = ++state.playGen;
      const idx = state.index;

      const tryPlay = () => {
        if (playGen !== state.playGen || idx !== state.index) return;
        video.play().then(() => {
          if (playGen !== state.playGen || idx !== state.index) return;
          if (errBox) errBox.classList.remove("show");
          if (loader) loader.classList.add("hidden");
          updatePauseIndicator(slide, video);
        }).catch((err) => {
          if (playGen !== state.playGen || idx !== state.index) return;
          if (err && err.name === "AbortError") return;
          // 仍有媒体错误：露出重试，而不是假暂停键
          if (isVideoDead(video)) {
            if (loader) loader.classList.add("hidden");
            if (errBox) {
              errBox.querySelector(".error-msg").textContent = "播放失败，请重试";
              errBox.classList.add("show");
            }
          }
          updatePauseIndicator(slide, video);
        });
      };

      // 已有可播数据立刻播；否则等 canplay，并设超时兜底
      if (video.readyState >= 2 && !isVideoDead(video)) {
        tryPlay();
      } else {
        if (loader) {
          loader.querySelector(".loader-text").textContent = "加载中…";
          loader.classList.remove("hidden");
        }
        if (errBox) errBox.classList.remove("show");
        let canplayFired = false;
        const onReady = () => {
          canplayFired = true;
          video.removeEventListener("canplay", onReady);
          tryPlay();
        };
        video.addEventListener("canplay", onReady);
        if (state._playTimer) clearTimeout(state._playTimer);
        state._playTimer = setTimeout(() => {
          video.removeEventListener("canplay", onReady);
          if (playGen !== state.playGen || idx !== state.index) return;
          if (!canplayFired && video.paused) tryPlay();
        }, 8000);
      }

      state.currentVideo = video;
      try { video.playbackRate = state.playbackRate || 1; } catch (_) {}
      const attachSubs = () => {
        if (slide.querySelector("track[data-nastok]")) return;
        const track = document.createElement("track");
        track.kind = "subtitles";
        track.srclang = "zh";
        track.label = "字幕";
        track.default = true;
        track.dataset.nastok = "1";
        const rel = (info.path || "").split("/").filter(Boolean).map(encodeURIComponent).join("/");
        track.src = `/api/videos/subtitles/${rel}`;
        video.appendChild(track);
      };
      const onMeta = () => {
        resumeWatchPos(video, info.path);
        attachSubs();
      };
      if (video.readyState >= 1) onMeta();
      else video.addEventListener("loadedmetadata", onMeta, { once: true });
      if (!video.dataset.posBound) {
        video.dataset.posBound = "1";
        video.addEventListener("pause", () => {
          const p = video.dataset.path;
          saveWatchPos(p, video.currentTime, video.duration);
        });
      }
      bindGlobalProgress(video);
      bindMediaSession(info, video);
      updatePauseIndicator(slide, video);
      syncDesktopPlayerControls();
      updateFeedCounter();
      bumpChrome();
      const path = video.dataset.path;
      if (path && !state.viewed.has(path)) {
        state.viewed.add(path);
        api("/api/videos/view", {
          method: "POST",
          body: JSON.stringify({ path }),
        }).catch(() => {});
      }
    }

    export async function nextVideo() {
      if (state._swipeLock) return;
      if (state.index < state.videos.length - 1) {
        state._swipeLock = true;
        goTo(state.index + 1, true);
        playCurrent();
        setTimeout(() => { state._swipeLock = false; }, 180);
        return;
      }

      // 已到当前已加载列表末尾：先回弹，避免拖拽位移卡住
      goTo(state.index, true);

      if (!state.hasMore) {
        const total = state.total || state.videos.length;
        showFeedToast(
          total
            ? `已经是最后一个视频了（共 ${total} 个）\n可在播放顺序里选「随机」重新开始`
            : "没有更多视频了",
          2800
        );
        return;
      }

      showFeedToast("正在加载更多…", 1500);
      try {
        const before = state.videos.length;
        await loadVideos(false);
        if (state.index < state.videos.length - 1) {
          state._swipeLock = true;
          goTo(state.index + 1, true);
          playCurrent();
          setTimeout(() => { state._swipeLock = false; }, 180);
        } else if (state.videos.length === before || !state.hasMore) {
          const total = state.total || state.videos.length;
          state.hasMore = false;
          showFeedToast(
            total
              ? `已经是最后一个视频了（共 ${total} 个）`
              : "没有更多视频了",
            2800
          );
          updateFeedCounter();
        }
      } catch (_) {
        showFeedToast("加载失败，请稍后重试或点击刷新", 2500);
      }
    }

    export function prevVideo() {
      if (state._swipeLock) return;
      if (state.index > 0) {
        state._swipeLock = true;
        goTo(state.index - 1, true);
        playCurrent();
        setTimeout(() => { state._swipeLock = false; }, 180);
        return;
      }
      goTo(state.index, true);
      showFeedToast("已经是第一个视频了", 1800);
    }

    /* ---------- 滑动手势 / 滚轮 / 键盘 ---------- */
    export const viewport = $("#feed-viewport");
    export const trackEl = $("#feed-track");

    viewport.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        if ($("#help-overlay") && !$("#help-overlay").classList.contains("hidden")) return;
        if (document.querySelector(".modal-backdrop.open")) return;
        if (state._wheelLock) return;
        state._wheelLock = true;
        if (e.deltaY > 30) nextVideo();
        else if (e.deltaY < -30) prevVideo();
        setTimeout(() => { state._wheelLock = false; }, 450);
      },
      { passive: false }
    );

    // 统一 Pointer：手机滑动 + 桌面拖拽切换
    viewport.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      if (e.target.closest(".dock-actions, .bottom-dock, .top-bar, .help-overlay, .exit-immersive-btn, button, a, select, input, .progress-bar")) return;
      if (state._swipeLock) return;
      state.dragging = true;
      state._dragCommitted = false;
      state.startY = e.clientY;
      state.startX = e.clientX;
      state.dragY = 0;
      state._lastY = e.clientY;
      state._lastT = performance.now();
      state._velocity = 0;
      state._pointerId = e.pointerId;
      const cur = currentItem();
      // 相册 slide 上的点按落在 tap-zones 上，仍允许判定横向翻图
      state._onAlbum = itemKind(cur) === "album" && !!e.target.closest(".video-slide");
      state._albumAxis = null;
      state._albumFeedIdx = state._onAlbum ? state.index : -1;
      // 仅对鼠标做 pointer capture：触摸指针在 iOS Safari 等上调用 setPointerCapture
      // 会吞掉同一手势后续的 pointermove，导致拖动位移无法累积、松手回弹
      if (e.pointerType !== "touch") {
        try { viewport.setPointerCapture(e.pointerId); } catch (_) {}
      }
    });

    // 移动端长按禁止系统选中/呼出菜单
    $("#page-feed").addEventListener("contextmenu", (e) => {
      if (isMobileFeed()) e.preventDefault();
    });
    $("#page-feed").addEventListener("selectstart", (e) => {
      if (isMobileFeed()) e.preventDefault();
    });

    viewport.addEventListener("pointermove", (e) => {
      if (!state.dragging || e.pointerId !== state._pointerId) return;
      const dxTotal = e.clientX - state.startX;
      const dyTotal = e.clientY - state.startY;

      // 相册：先判定横/竖轴，横向翻图时不触发纵向 feed 滑动
      if (state._onAlbum && !state._albumAxis) {
        if (Math.abs(dxTotal) > 10 || Math.abs(dyTotal) > 10) {
          state._albumAxis = Math.abs(dxTotal) > Math.abs(dyTotal) * 1.15 ? "h" : "v";
          if (state._albumAxis === "h") {
            clearTimeout(state.longPressTimer);
            if (state.isFastForward) {
              state.isFastForward = false;
              const video = getCurrentVideo();
              if (video) video.playbackRate = state.playbackRate || 1;
              $("#speed-toast").classList.remove("show");
            }
          }
        }
      }

      if (state._albumAxis === "h") {
        const slide = $(`#feed-track .video-slide[data-index="${state._albumFeedIdx}"]`);
        const albumTrack = slide && slide.querySelector(".album-track");
        if (albumTrack) {
          const idx = Number(slide.dataset.albumIdx || 0);
          const w = viewport.clientWidth || 1;
          albumTrack.style.transition = "none";
          albumTrack.style.transform = `translateX(${-idx * 100 + (dxTotal / w) * 100}%)`;
        }
        return;
      }

      const now = performance.now();
      const dy = e.clientY - state._lastY;
      const dt = now - state._lastT;
      // 用短窗口速度，松手瞬间更准
      if (dt > 0) {
        const instant = dy / dt;
        state._velocity = Math.abs(dt) < 64 ? instant * 0.7 + state._velocity * 0.3 : instant;
      }
      state._lastY = e.clientY;
      state._lastT = now;
      state.dragY = dyTotal;
      if (!state._dragCommitted && Math.abs(state.dragY) > 10) {
        state._dragCommitted = true;
        clearTimeout(state.longPressTimer);
        if (state.isFastForward) {
          state.isFastForward = false;
          const video = getCurrentVideo();
          if (video) video.playbackRate = state.playbackRate || 1;
          $("#speed-toast").classList.remove("show");
        }
        trackEl.classList.add("dragging");
      }
      if (!state._dragCommitted) return;
      const h = viewport.clientHeight || 1;
      // 全程用百分比，与 goTo 单位一致，避免松手跳变
      const pct = -state.index * 100 + (state.dragY / h) * 100;
      trackEl.style.transform = `translateY(${pct}%)`;
    });

    async function doPullRefresh() {
      if (state._pullRefreshing) return;
      state._pullRefreshing = true;
      showFeedToast("正在刷新…", 1400);
      try {
        await api("/api/videos/refresh", { method: "POST" });
        state.seed = null;
        await resetAndLoad(true);
        showFeedToast(`已刷新，共 ${state.total ?? 0} 个媒体`, 2000);
      } catch (ex) {
        showFeedToast(ex.message || "刷新失败", 2400);
      } finally {
        state._pullRefreshing = false;
      }
    }

    export function endPointerDrag(e) {
      if (!state.dragging || (e && e.pointerId != null && e.pointerId !== state._pointerId)) return;
      state.dragging = false;
      trackEl.classList.remove("dragging");
      try {
        if (state._pointerId != null) viewport.releasePointerCapture(state._pointerId);
      } catch (_) {}

      // 指针捕获可能导致 zone 收不到 mouseup，在此统一结束倍速
      clearTimeout(state.longPressTimer);
      if (state.isFastForward) {
        state.isFastForward = false;
        const video = getCurrentVideo();
        if (video) video.playbackRate = state.playbackRate || 1;
        $("#speed-toast").classList.remove("show");
        state.ignoreTapUntil = Date.now() + 450;
      }

      if (state._albumAxis === "h") {
        const feedIdx = state._albumFeedIdx;
        const slide = $(`#feed-track .video-slide[data-index="${feedIdx}"]`);
        const item = state.videos[feedIdx];
        const dx = (e && e.clientX != null ? e.clientX : state.startX) - state.startX;
        const w = viewport.clientWidth || 400;
        const idx = Number((slide && slide.dataset.albumIdx) || 0);
        state.ignoreTapUntil = Date.now() + 350;
        if (item && slide) {
          if (dx < -w * 0.15) setAlbumIndex(feedIdx, idx + 1);
          else if (dx > w * 0.15) setAlbumIndex(feedIdx, idx - 1);
          else setAlbumIndex(feedIdx, idx);
        }
        state._albumAxis = null;
        state._onAlbum = false;
        state._albumFeedIdx = -1;
        state.dragY = 0;
        state._velocity = 0;
        state._dragCommitted = false;
        state._pointerId = null;
        return;
      }

      if (state._dragCommitted) {
        state.ignoreTapUntil = Date.now() + 350;
        const h = viewport.clientHeight || 400;
        const distThreshold = h * 0.12;
        const velThreshold = 0.4; // px/ms ≈ 400px/s
        // 停顿过久则忽略速度，避免“滑一点停住又误切”
        const idle = performance.now() - (state._lastT || 0);
        const vy = idle > 120 ? 0 : (state._velocity || 0);
        if (state.dragY < -distThreshold || vy < -velThreshold) {
          nextVideo();
        } else if (state.dragY > distThreshold || vy > velThreshold) {
          prevVideo();
        } else {
          goTo(state.index, true);
          playCurrent();
        }
      }
      state.dragY = 0;
      state._velocity = 0;
      state._dragCommitted = false;
      state._pointerId = null;
      state._albumAxis = null;
      state._onAlbum = false;
      state._albumFeedIdx = -1;
    }
    viewport.addEventListener("pointerup", endPointerDrag);
    viewport.addEventListener("pointercancel", endPointerDrag);
    viewport.addEventListener("lostpointercapture", endPointerDrag);

    export function toggleHelp() {
      const ov = $("#help-overlay");
      ov.classList.toggle("hidden");
    }
    export function closeHelp() {
      $("#help-overlay").classList.add("hidden");
    }
    $("#help-btn").addEventListener("click", toggleHelp);
    $("#help-close").addEventListener("click", closeHelp);
    $("#help-overlay").addEventListener("click", (e) => {
      if (e.target === $("#help-overlay")) closeHelp();
    });

    window.addEventListener("keydown", (e) => {
      if (!$("#page-feed").classList.contains("active")) return;
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target.isContentEditable) return;
      if (document.querySelector(".modal-backdrop.open")) return;

      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
        e.preventDefault();
        toggleHelp();
        return;
      }
      if (e.key === "Escape") {
        if (!$("#help-overlay").classList.contains("hidden")) {
          e.preventDefault();
          closeHelp();
          return;
        }
        if (document.querySelector(".top-more.open")) {
          e.preventDefault();
          closeTopMore();
          return;
        }
        if (!$("#dock-more-menu").classList.contains("hidden")) {
          e.preventDefault();
          closeDockMenu();
          return;
        }
        if (state.immersive) {
          e.preventDefault();
          exitImmersive();
        }
        return;
      }
      if (!$("#help-overlay").classList.contains("hidden")) return;

      if (e.key === "ArrowLeft") {
        e.preventDefault();
        const item = currentItem();
        if (itemKind(item) === "album") {
          const slide = $(`#feed-track .video-slide[data-index="${state.index}"]`);
          setAlbumIndex(state.index, Number((slide && slide.dataset.albumIdx) || 0) - 1);
          return;
        }
        if (itemKind(item) !== "video") return;
        const video = getCurrentVideo();
        if (video) seekBy(video, e.shiftKey ? -30 : -5);
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        const item = currentItem();
        if (itemKind(item) === "album") {
          const slide = $(`#feed-track .video-slide[data-index="${state.index}"]`);
          setAlbumIndex(state.index, Number((slide && slide.dataset.albumIdx) || 0) + 1);
          return;
        }
        if (itemKind(item) !== "video") return;
        const video = getCurrentVideo();
        if (video) seekBy(video, e.shiftKey ? 30 : 5);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        prevVideo();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        nextVideo();
        return;
      }
      if (e.key === "j" || e.key === "PageDown") {
        e.preventDefault();
        nextVideo();
        return;
      }
      if (e.key === "k" || e.key === "PageUp") {
        e.preventDefault();
        prevVideo();
        return;
      }
      if (e.key === " ") {
        e.preventDefault();
        if (itemKind(currentItem()) !== "video") return;
        const video = getCurrentVideo();
        if (video) togglePlayPause(video);
        return;
      }
      if (e.key === "m" || e.key === "M") {
        e.preventDefault();
        if (itemKind(currentItem()) !== "video") return;
        $("#mute-btn").click();
        return;
      }
      if (e.key === "<" || e.key === ",") {
        e.preventDefault();
        cycleRate(-1);
        return;
      }
      if (e.key === ">" || e.key === ".") {
        e.preventDefault();
        cycleRate(1);
      }
    });

    const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];
    function cycleRate(dir) {
      const cur = Number(state.playbackRate) || 1;
      let i = RATES.findIndex((r) => Math.abs(r - cur) < 0.01);
      if (i < 0) i = 2;
      i = Math.max(0, Math.min(RATES.length - 1, i + dir));
      setPlaybackRate(RATES[i]);
    }
