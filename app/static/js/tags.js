/* tags.js — 标记模态框与标记筛选(由 split_frontend.py 机械切割,勿手改顺序) */
import { isVideoDead, pauseAll, resetAndLoad, showFeedToast } from './feed.js';
import { getCurrentVideo } from './player.js';
import { state } from './state.js';
import { $, $$, api, escapeHtml } from './util.js';

    /* ---------- 标记 ---------- */

    export let _tagModalPath = null;
    export let _tagModalWasPlaying = false;

    export function openTagModal(v) {
      const cur = getCurrentVideo();
      _tagModalWasPlaying = cur && !cur.paused;
      pauseAll();
      _tagModalPath = v.path;
      $("#tag-modal-path").textContent = v.title || v.path;
      $("#tag-modal").classList.add("open");
      loadTagModalItems();
    }

    export function closeTagModal() {
      $("#tag-modal").classList.remove("open");
      _tagModalPath = null;
      if (_tagModalWasPlaying) {
        _tagModalWasPlaying = false;
        const video = getCurrentVideo();
        if (video && !isVideoDead(video)) {
          video.play().catch(() => {});
        }
      }
    }

    export async function loadTagModalItems() {
      if (!_tagModalPath) return;
      const path = _tagModalPath;
      const container = $("#tag-list");
      try {
        const data = await api(`/api/videos/tags?path=${encodeURIComponent(path)}`);
        if (_tagModalPath !== path) return;
        container.innerHTML = "";
        data.items.forEach((t) => {
          const row = document.createElement("label");
          row.className = "tag-item";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = t.id;
          cb.checked = t.assigned;
          const nameSpan = document.createElement("span");
          nameSpan.className = "tag-name";
          nameSpan.textContent = t.name;
          const renameBtn = document.createElement("button");
          renameBtn.type = "button";
          renameBtn.className = "btn btn-ghost btn-sm tag-rename-btn";
          renameBtn.textContent = "✎";
          renameBtn.title = "重命名";
          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-ghost btn-sm tag-del-btn";
          delBtn.textContent = "✕";
          delBtn.title = "删除";
          renameBtn.addEventListener("click", (ev) => {
            ev.preventDefault();
            renameTag(t.id, t.name);
          });
          delBtn.addEventListener("click", (ev) => {
            ev.preventDefault();
            deleteTagPrompt(t.id, t.name);
          });
          row.append(cb, nameSpan, renameBtn, delBtn);
          container.appendChild(row);
        });
      } catch (ex) {
        showFeedToast(ex.message || "加载标记失败", 2200);
      }
    }

    export async function renameTag(tagId, oldName) {
      const newName = prompt("重命名标记", oldName);
      if (!newName || newName.trim() === oldName || !newName.trim()) return;
      try {
        await api(`/api/tags/${tagId}`, {
          method: "PUT",
          body: JSON.stringify({ name: newName.trim() }),
        });
        loadTagModalItems();
        refreshTagCache();
      } catch (ex) {
        showFeedToast(ex.message || "重命名失败", 2200);
      }
    }

    export async function deleteTagPrompt(tagId, tagName) {
      if (!confirm(`确定删除标记「${tagName}」吗？所有媒体上的此标记将被移除。`)) return;
      try {
        await api(`/api/tags/${tagId}`, { method: "DELETE" });
        loadTagModalItems();
        refreshTagCache();
      } catch (ex) {
        showFeedToast(ex.message || "删除失败", 2200);
      }
    }

    $("#tag-new-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        $("#tag-create-btn").click();
      }
    });
    $("#tag-create-btn").addEventListener("click", async () => {
      const input = $("#tag-new-input");
      const name = (input.value || "").trim();
      if (!name) return;
      try {
        await api("/api/tags", {
          method: "POST",
          body: JSON.stringify({ name }),
        });
        input.value = "";
        loadTagModalItems();
        refreshTagCache();
      } catch (ex) {
        showFeedToast(ex.message || "创建失败", 2200);
      }
    });

    $("#tag-save-btn").addEventListener("click", async () => {
      if (!_tagModalPath) return;
      const cbs = $$("#tag-list input[type='checkbox']");
      const ids = cbs.filter((c) => c.checked).map((c) => Number(c.value));
      try {
        await api("/api/videos/tags", {
          method: "PUT",
          body: JSON.stringify({ path: _tagModalPath, tag_ids: ids }),
        });
        refreshTagCache();
        closeTagModal();
        showFeedToast("标记已保存", 1400);
      } catch (ex) {
        showFeedToast(ex.message || "保存失败", 2200);
      }
    });

    $("#tag-close-btn").addEventListener("click", closeTagModal);
    $("#tag-modal").addEventListener("click", (e) => {
      if (e.target === $("#tag-modal")) closeTagModal();
    });

    export async function refreshTagCache() {
      try {
        const data = await api("/api/tags");
        state.tags = data.items || [];
        // 当前筛选的标记若已被删除，回到默认
        if ((state.tagId && !state.tags.some((t) => t.id === state.tagId)) ||
            (state.tagged && !state.tags.length)) {
          state.tagId = null;
          state.tagged = false;
          state.seed = null;
          resetAndLoad();
        }
        syncTagFilter();
      } catch (_) {}
    }

    export function syncTagFilter() {
      const sel = $("#tag-filter-select");
      if (!sel) return;
      const tags = state.tags || [];
      if (tags.length === 0) {
        sel.classList.add("hidden");
        return;
      }
      sel.classList.remove("hidden");
      let html = '<option value="">默认</option><option value="__all__">全部标记</option>';
      for (const t of tags) {
        html += `<option value="${t.id}">${escapeHtml(t.name)}</option>`;
      }
      sel.innerHTML = html;
      if (state.tagId) sel.value = String(state.tagId);
      else if (state.tagged) sel.value = "__all__";
      else sel.value = "";
    }

    $("#tag-filter-select").addEventListener("change", () => {
      const val = $("#tag-filter-select").value;
      if (val === "__all__") {
        state.tagId = null;
        state.tagged = true;
      } else if (val) {
        state.tagId = Number(val);
        state.tagged = false;
      } else {
        // "默认"：回到无标记筛选，等同推荐
        state.tagId = null;
        state.tagged = false;
        state.seed = null;
        state.tab = "all";
        $$(".feed-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === "all"));
        resetAndLoad();
        return;
      }
      $$(".feed-tabs button").forEach((b) => b.classList.remove("active"));
      state.tab = null;
      state.seed = null;
      resetAndLoad();
    });

    // feed-tabs 点击时回到默认（无标记筛选）
    $$(".feed-tabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.tagId = null;
        state.tagged = false;
        $("#tag-filter-select").value = "";
      });
    });
