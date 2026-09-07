/* main.js — 启动路由(分享/登录/Feed)与全局装配(由 split_frontend.py 机械切割,勿手改顺序) */
import { openAdmin } from './admin.js';
import { doLogout, isStaff, isSysadmin } from './auth.js';
import { playCurrent, resetAndLoad, showFeedToast } from './feed.js';
import { ICON } from './icons.js';
import { setSort } from './player.js';
import { openPublicShare } from './share-page.js';
import { state } from './state.js';
import { refreshTagCache } from './tags.js';
import { $, api, lsGet, lsSet, showPage } from './util.js';

    export async function bootstrap() {
      const shareMatch = location.pathname.match(/^\/s\/([A-Za-z0-9_-]+)$/);
      if (shareMatch) {
        await openPublicShare(shareMatch[1]);
        return;
      }
      try {
        const user = await api("/api/auth/me");
        state.user = user;
        if (user.must_change_password) {
          showPage("page-change-pwd");
        } else {
          enterFeed();
        }
      } catch {
        showPage("page-login");
      }
    }

    export function enterFeed() {
      $("#admin-link").classList.toggle("hidden", !isStaff());
      $("#admin-link-m")?.classList.toggle("hidden", !isStaff());
      $("#backup-download-btn")?.classList.toggle("hidden", !isSysadmin());
      $("#backup-restore-label")?.classList.toggle("hidden", !isSysadmin());
      showPage("page-feed");
      setSort(state.sort, false);
      state.tagId = null;
      state.tagged = false;
      loadLibraryFilter();
      resetAndLoad();
      refreshTagCache().catch(() => {});
      if (!lsGet("nastok_help_hint")) {
        lsSet("nastok_help_hint", "1");
        setTimeout(() => showFeedToast("按 ? 或点击右下角「?」查看快捷键", 3200), 800);
      }
    }

    async function loadLibraryFilter() {
      const sel = $("#library-filter-select");
      if (!sel) return;
      try {
        const data = await api("/api/libraries");
        const items = data.items || [];
        sel.innerHTML = '<option value="">全部库</option>';
        items.forEach((lib) => {
          const opt = document.createElement("option");
          opt.value = String(lib.id);
          opt.textContent = lib.name;
          sel.appendChild(opt);
        });
        sel.classList.toggle("hidden", items.length < 2);
        if (state.libraryId) sel.value = String(state.libraryId);
      } catch (_) {
        sel.classList.add("hidden");
      }
    }
    $("#library-filter-select")?.addEventListener("change", () => {
      const v = $("#library-filter-select").value;
      state.libraryId = v ? Number(v) : null;
      state.seed = null;
      resetAndLoad();
    });
    let _searchTimer = 0;
    $("#feed-search")?.addEventListener("input", () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => {
        state.query = ($("#feed-search").value || "").trim();
        state.seed = null;
        resetAndLoad();
      }, 280);
    });

    $("#admin-link").addEventListener("click", (e) => {
      e.preventDefault();
      openAdmin();
    });
    $("#admin-link-m")?.addEventListener("click", (e) => {
      e.preventDefault();
      openAdmin();
    });
    $("#logout-btn-m")?.addEventListener("click", doLogout);
    $("#back-feed-btn").addEventListener("click", () => {
      showPage("page-feed");
      playCurrent();
    });

    $("#refresh-btn").addEventListener("click", async () => {
      const btn = $("#refresh-btn");
      const menuBtn = $("#menu-refresh-btn");
      btn.disabled = true;
      btn.classList.add("is-busy");
      if (menuBtn) { menuBtn.disabled = true; menuBtn.textContent = "刷新中…"; }
      try {
        const data = await api("/api/videos/refresh", { method: "POST" });
        state.seed = null;
        await resetAndLoad(true);
        const total = state.total ?? data.total ?? 0;
        showFeedToast(`已刷新扫描，共 ${total} 个视频`, 2200);
      } catch (ex) {
        showFeedToast(ex.message || "刷新失败", 2500);
      } finally {
        btn.disabled = false;
        btn.classList.remove("is-busy");
        btn.innerHTML = ICON.refresh();
        if (menuBtn) { menuBtn.disabled = false; menuBtn.textContent = "刷新"; }
      }
    });

    bootstrap();

    // PWA:仅安全上下文(https/localhost)注册;http://局域网IP 不生效属预期
    if ("serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker
        .register("/static/sw.js", { scope: "/" })
        .catch((err) => console.warn("ServiceWorker 注册失败:", err));
    }
