/* search.js — 全屏搜索页：关键词搜索 + 结果卡片 + 点击定位播放 */
import { ICON, MEDIA_MODE_ICON } from './icons.js';
import { goTo, loadVideos, playCurrent, resetAndLoad, exitSearchFeed, streamUrl, syncFeedTabSelect } from './feed.js';
import { setMediaMode, setPlayMode } from './player.js';
import { itemKind, state } from './state.js';
import { $, api, escapeHtml, showPage } from './util.js';

    const KIND_LABEL = { video: "视频", image: "图片", album: "相册" };
    const PAGE_LIMIT = 50;
    let _timer = 0;
    let _gen = 0;
    let _q = null; // 当前网格对应的查询词；"" = 全部媒体；null = 尚未加载过
    let _page = 1;
    let _hasMore = false;
    let _loadingMore = false;

    export function openSearch() {
      const input = $("#search-input");
      input.value = state.query || "";
      syncClearBtn();
      showPage("page-search");
      // 搜索页独立于播放流，进入时暂停 feed 声音
      try {
        const v = $("#feed-track video");
        if (v) v.pause();
      } catch (_) {}
      // 进入时按当前关键词加载网格：首次(_q===null)或过滤词已变(如退出搜索
      // 清空 state.query)时需重新加载,否则仍停留在旧的筛选结果且无清除入口
      const q = (input.value || "").trim();
      if (_q === null || _q !== q) runSearch(input.value);
      setTimeout(() => input.focus(), 60);
    }

    function syncClearBtn() {
      $("#search-clear-btn").classList.toggle("hidden", !$("#search-input").value);
    }

    function gridQs(q, page) {
      const qs = new URLSearchParams({
        sort: "newest",
        media_mode: "mixed",
        page: String(page),
        limit: String(PAGE_LIMIT),
      });
      if (q) qs.set("q", q);
      return qs;
    }

    /** 加载网格：有关键词=搜索，无关键词=全部媒体；page=1 重置 */
    async function runSearch(q) {
      q = (q || "").trim();
      const myGen = ++_gen;
      _q = q;
      _page = 1;
      _hasMore = false;
      _loadingMore = false;
      const grid = $("#search-grid");
      const empty = $("#search-empty");
      const spinner = $("#search-spinner");
      spinner.classList.remove("hidden");
      grid.classList.add("hidden");
      empty.classList.add("hidden");
      try {
        const data = await api(`/api/videos/list?${gridQs(q, 1)}`);
        if (myGen !== _gen) return;
        renderResults(data.items || [], q);
        _hasMore = (data.page || 1) * (data.limit || PAGE_LIMIT) < (data.total || 0);
        // 补齐首屏不足一屏的情况;失败静默(翻页错误不应影响已渲染结果)
        ensureGridFill().catch(() => {});
      } catch (ex) {
        if (myGen !== _gen) return;
        grid.classList.add("hidden");
        empty.textContent = ex.message || "加载失败，请重试";
        empty.classList.remove("hidden");
      } finally {
        if (myGen === _gen) spinner.classList.add("hidden");
      }
    }

    /** 滚动到底自动加载下一页 */
    async function loadMore() {
      if (!_hasMore || _loadingMore || _q === null) return;
      _loadingMore = true;
      const spinner = $("#search-spinner");
      spinner.classList.remove("hidden");
      try {
        const data = await api(`/api/videos/list?${gridQs(_q, _page + 1)}`);
        if (data.items && data.items.length) {
          _page = data.page || _page + 1;
          $("#search-grid").insertAdjacentHTML("beforeend", data.items.map(resultCardHtml).join(""));
          _hasMore = (data.page || 1) * (data.limit || PAGE_LIMIT) < (data.total || 0);
        } else {
          _hasMore = false;
        }
      } catch (_) {
        // 翻页失败静默：保留已加载内容，下次滚动到底重试
      } finally {
        _loadingMore = false;
        spinner.classList.add("hidden");
      }
    }

    /** 首屏网格不足一屏时滚动条不出现,无限滚动 loadMore 永不触发。
     * 大屏桌面端需自动翻页补齐,直到出现滚动条或无更多。 */
    async function ensureGridFill() {
      const el = $("#search-body");
      if (!el || !_hasMore || _loadingMore) return;
      let guard = 0;
      while (_hasMore && !_loadingMore && el.scrollHeight <= el.clientHeight && guard++ < 50) {
        await loadMore();
      }
    }

    function resultCardHtml(v) {
      const kind = itemKind(v);
      const title = v.title || (v.path || "").split("/").pop() || "未命名";
      let cover;
      if (kind === "image") {
        cover = `<img src="${streamUrl(v)}" loading="lazy" alt="" />`;
      } else {
        const iconFn = MEDIA_MODE_ICON[kind === "album" ? "images" : kind] || MEDIA_MODE_ICON.video;
        cover = iconFn();
      }
      return `<div class="search-card" data-path="${escapeHtml(v.path)}" role="button" tabindex="0" aria-label="播放 ${escapeHtml(title)}">
        <div class="search-card-cover">${cover}<span class="search-kind-badge">${KIND_LABEL[kind] || "视频"}</span></div>
        <div class="search-card-info">
          <div class="search-card-title">${escapeHtml(title)}</div>
          <div class="search-card-path">${escapeHtml(v.path || "")}</div>
        </div>
      </div>`;
    }

    function renderResults(items, q) {
      const grid = $("#search-grid");
      const empty = $("#search-empty");
      if (!items.length) {
        grid.classList.add("hidden");
        empty.textContent = q ? `没有找到「${q}」` : "暂无媒体";
        empty.classList.remove("hidden");
        return;
      }
      empty.classList.add("hidden");
      grid.innerHTML = items.map(resultCardHtml).join("");
      grid.classList.remove("hidden");
    }

    /** 点击结果：feed 以同一关键词 + 最新排序加载，定位到该条播放 */
    async function chooseResult(path) {
      const q = ($("#search-input").value || "").trim();
      state.query = q;
      state.seed = null;
      state.tab = "all";
      state.tagId = null;
      state.tagged = false;
      // 与 runSearch 完全一致的查询参数（mixed 全类型 + 最新排序），
      // 保证结果顺序相同、目标条目必在列表中
      setMediaMode("mixed", false);
      setPlayMode("order", false);
      syncFeedTabSelect();
      showPage("page-feed");
      try {
        await resetAndLoad();
        let idx = state.videos.findIndex((it) => it && it.path === path);
        let guard = 0;
        while (idx < 0 && state.hasMore && guard++ < 20) {
          await loadVideos(false, false, true);
          idx = state.videos.findIndex((it) => it && it.path === path);
        }
        if (idx > 0) goTo(idx, false);
      } catch (ex) {
        console.error(ex);
      }
      playCurrent();
    }

    export function bindSearchPage() {
      $("#search-back-btn").innerHTML = ICON.back();
      $("#search-back-btn").addEventListener("click", () => {
        showPage("page-feed");
        playCurrent();
      });
      $("#search-input").addEventListener("input", () => {
        syncClearBtn();
        clearTimeout(_timer);
        _timer = setTimeout(() => runSearch($("#search-input").value), 350);
      });
      $("#search-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          clearTimeout(_timer);
          runSearch($("#search-input").value);
        }
      });
      $("#search-clear-btn").addEventListener("click", () => {
        const input = $("#search-input");
        input.value = "";
        syncClearBtn();
        clearTimeout(_timer);
        // 若 feed 正处于搜索过滤态,重置标志外还要真正重载回普通列表,
        // 否则返回播放页仍是搜索过滤的列表(仅清标志不够)
        const reload = state.query !== "";
        exitSearchFeed({ reload });
        runSearch("");
        input.focus();
      });
      $("#search-grid").addEventListener("click", (e) => {
        const card = e.target.closest(".search-card");
        if (card) chooseResult(card.dataset.path);
      });
      $("#search-grid").addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const card = e.target.closest(".search-card");
        if (card) {
          e.preventDefault();
          chooseResult(card.dataset.path);
        }
      });
      // 无限滚动：接近底部 300px 时加载下一页
      $("#search-body").addEventListener("scroll", () => {
        const el = $("#search-body");
        if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) loadMore();
      });
    }
