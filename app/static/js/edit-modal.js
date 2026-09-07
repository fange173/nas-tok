/* edit-modal.js — 编辑模态框(视频/图片/相册)(由 split_frontend.py 机械切割,勿手改顺序) */
import { clearFeedTrack, downloadUrl, goTo, invalidatePendingLoad, isVideoDead, mountVideoSrc, pauseAll, playCurrent, renderSlides, resetAndLoad, showFeedToast, streamUrl, updateFeedCounter } from './feed.js';
import { bindGlobalProgress, ensureAlbumImages, getCurrentVideo } from './player.js';
import { setShareMenuItem } from './share-page.js';
import { albumImageCount, itemKind, state } from './state.js';
import { $, $$, api, escapeHtml, fmtDateTime, toDatetimeLocalValue } from './util.js';

    /* ---------- 编辑模态框 ---------- */
    export function openEditModal(videoInfo, idx) {
      const cur = getCurrentVideo();
      state._editWasPlaying = cur && !cur.paused;
      pauseAll();
      state.editTarget = { ...videoInfo, idx, kind: itemKind(videoInfo) };
      const kind = state.editTarget.kind;
      const titleEl = $("#edit-modal-title");
      const fileFields = $("#edit-file-fields");
      const albumFields = $("#edit-album-fields");
      const delBtn = $("#edit-delete");
      $("#edit-error").textContent = "";

      if (kind === "album") {
        if (titleEl) titleEl.textContent = "编辑相册";
        if (fileFields) fileFields.classList.add("hidden");
        if (albumFields) albumFields.classList.remove("hidden");
        if (delBtn) delBtn.textContent = "删除相册";
        $("#edit-album-name").value = videoInfo.title || "";
        const box = $("#edit-album-images");
        box.innerHTML = "";
        const renderAlbumRows = (images) => {
          box.innerHTML = "";
          (images || []).forEach((im, n) => {
          const albumPrefix = (videoInfo.path || "").replace(/\/$/, "");
          let rel = im.path || "";
          if (albumPrefix && (rel === albumPrefix || rel.startsWith(albumPrefix + "/"))) {
            rel = rel.slice(albumPrefix.length).replace(/^\//, "");
          } else {
            rel = (im.path || "").split("/").filter(Boolean).pop() || im.title || "";
          }
          const baseName = rel.split("/").pop() || rel;
          const row = document.createElement("div");
          row.className = "edit-album-row";
          const label = document.createElement("label");
          label.textContent = rel.includes("/") ? rel : `图片 ${n + 1}`;
          const input = document.createElement("input");
          input.dataset.oldName = rel;
          input.value = baseName;
          row.append(label, input);
          box.appendChild(row);
          });
        };
        if (Array.isArray(videoInfo.images) && videoInfo.images.length) {
          renderAlbumRows(videoInfo.images);
        } else {
          box.innerHTML = '<div class="spinner" style="margin:12px auto"></div>';
          ensureAlbumImages(idx).then((imgs) => {
            // 模态框可能已被关闭/切换目标；feed 重载后同 idx 可能指向不同相册，必须比对路径
            if (!state.editTarget || state.editTarget.idx !== idx || state.editTarget.path !== videoInfo.path) return;
            renderAlbumRows(imgs || []);
          });
        }
      } else {
        if (titleEl) titleEl.textContent = kind === "image" ? "编辑图片" : "编辑视频";
        if (fileFields) fileFields.classList.remove("hidden");
        if (albumFields) albumFields.classList.add("hidden");
        if (delBtn) delBtn.textContent = kind === "image" ? "删除图片" : "删除视频";
        $("#edit-name").value = videoInfo.title;
        $("#edit-date").value = toDatetimeLocalValue(videoInfo.mtime_iso || videoInfo.mtime);
      }
      $("#edit-modal").classList.add("open");
    }

    export function closeEditModal() {
      $("#edit-modal").classList.remove("open");
      state.editTarget = null;
      if (state._editWasPlaying) {
        state._editWasPlaying = false;
        const video = getCurrentVideo();
        if (video && !isVideoDead(video)) {
          video.play().catch(() => {});
        }
      }
    }

    export function patchSlide(i, info) {
      state.videos[i] = { ...state.videos[i], ...info };
      const slide = $(`#feed-track .video-slide[data-index="${i}"]`);
      if (!slide) return;
      const kind = itemKind(state.videos[i]);
      const video = slide.querySelector("video");
      const meta = slide.querySelector(".video-meta");
      if (video && kind === "video") {
        video.dataset.path = info.path;
        const nextSrc = streamUrl(state.videos[i]);
        if (video.getAttribute("src") !== nextSrc) {
          const t = video.currentTime;
          const wasPaused = video.paused;
          const gen = mountVideoSrc(video, nextSrc, { force: true, preload: "auto" });
          video.addEventListener("loadedmetadata", () => {
            if (Number(video.dataset.loadGen) !== gen) return;
            try { video.currentTime = t; } catch (_) {}
            if (!wasPaused) video.play().catch(() => {});
          }, { once: true });
        }
      }
      if (meta) {
        const item = state.videos[i];
        const sub = kind === "album"
          ? `${escapeHtml(item.library)} · ${albumImageCount(item)} 张 · ${escapeHtml(fmtDateTime(info.mtime_iso || info.mtime || item.mtime))}`
          : `${escapeHtml(item.library)} · ${escapeHtml(fmtDateTime(info.mtime_iso || info.mtime))}`;
        meta.innerHTML = `<h2>${escapeHtml(info.title || item.title)}</h2><p>${sub}</p>`;
      }
      // 下载链接与分享按钮的 href/data-share-path 是构建时静态写死的，重命名后需同步为新路径
      // （syncFeedShareState 按 data-share-path 匹配按钮，旧值会导致分享状态文案不更新）
      const dl = slide.querySelector("a.side-more-item");
      if (dl && kind !== "album") {
        dl.href = downloadUrl(state.videos[i]);
        dl.download = (state.videos[i].path || "").split("/").pop() || "";
      }
      const shareBtn = slide.querySelector(".side-more-item[data-share-path]");
      if (shareBtn && kind !== "album") {
        shareBtn.dataset.sharePath = state.videos[i].path || "";
        setShareMenuItem(shareBtn, !!state.videos[i].shared);
      }
    }

    export function rebuildSlidesKeepIndex(keepIdx) {
      clearFeedTrack();
      renderSlides(0);
      const idx = Math.min(Math.max(0, keepIdx), Math.max(0, state.videos.length - 1));
      if (state.videos.length) {
        goTo(idx, false);
        playCurrent();
      } else {
        bindGlobalProgress(null);
        updateFeedCounter();
      }
    }

    $("#edit-cancel").addEventListener("click", closeEditModal);
    $("#edit-modal").addEventListener("click", (e) => {
      if (e.target === $("#edit-modal")) closeEditModal();
    });

    $("#edit-save").addEventListener("click", async () => {
      if (!state.editTarget) return;
      const err = $("#edit-error");
      err.textContent = "";
      const kind = state.editTarget.kind || itemKind(state.editTarget);
      // 相册目录改名成功但后续图片改名失败时，用于在 catch 中同步 slide 状态
      let albumDirRenamed = false;
      let albumIdx = -1;
      try {
        if (kind === "album") {
          const i = state.editTarget.idx;
          const oldPath = state.editTarget.path;
          let albumPath = oldPath;
          const newAlbumName = $("#edit-album-name").value.trim();
          if (newAlbumName && newAlbumName !== state.editTarget.title) {
            const renamed = await api("/api/albums/rename", {
              method: "PUT",
              body: JSON.stringify({ old_path: oldPath, new_name: newAlbumName }),
            });
            albumPath = renamed.path || albumPath;
            albumDirRenamed = true;
            albumIdx = i;
            state.videos[i] = {
              ...state.videos[i],
              path: renamed.path,
              title: renamed.title,
              encoded_path: renamed.encoded_path,
            };
            // 路径前缀已变，清空图片缓存让 feed 重新按需拉取
            state.videos[i].images = null;
          }
          const renames = [];
          $$("#edit-album-images .edit-album-row input").forEach((input) => {
            const oldName = input.dataset.oldName || "";
            const newName = input.value.trim();
            if (oldName && newName && newName !== oldName) {
              renames.push({ old_name: oldName, new_name: newName });
            }
          });
          if (renames.length) {
            await api("/api/albums/images", {
              method: "PUT",
              body: JSON.stringify({ album_path: albumPath, renames }),
            });
            // 文件名已变，清空缓存让 feed 重新按需拉取
            state.videos[i].images = null;
          }
          closeEditModal();
          rebuildSlidesKeepIndex(i);
          showFeedToast("相册已保存", 1600);
          return;
        }

        const newName = $("#edit-name").value.trim();
        const newDate = $("#edit-date").value;
        const body = { old_path: state.editTarget.path };
        if (newName) body.new_name = newName;
        if (newDate) body.new_date = newDate + ":00";
        const result = await api("/api/videos/edit", {
          method: "PUT",
          body: JSON.stringify(body),
        });
        const i = state.editTarget.idx;
        const merged = { ...state.videos[i], ...result };
        closeEditModal();
        if (kind === "image") {
          state.videos[i] = merged;
          rebuildSlidesKeepIndex(i);
        } else {
          patchSlide(i, merged);
        }
      } catch (ex) {
        if (albumDirRenamed) {
          // 目录已改名成功：关闭弹窗并重建 slide（state.videos 已指向新路径），
          // 避免界面停留在旧标题、后续操作却走新路径的漂移
          closeEditModal();
          rebuildSlidesKeepIndex(albumIdx);
          showFeedToast(`相册目录已改名，但图片重命名失败：${ex.message || "请重试"}`, 3600);
          return;
        }
        err.textContent = ex.message || "保存失败";
      }
    });

    $("#edit-delete").addEventListener("click", async () => {
      if (!state.editTarget) return;
      const kind = state.editTarget.kind || itemKind(state.editTarget);
      const label = kind === "album" ? "相册" : kind === "image" ? "图片" : "视频";
      if (!confirm(`确认物理删除「${state.editTarget.title}」？此操作不可恢复。`)) return;
      try {
        const i = state.editTarget.idx;
        await api("/api/videos/delete", {
          method: "DELETE",
          body: JSON.stringify({ path: state.editTarget.path }),
        });
        closeEditModal();
        // 作废在途分页，防止旧响应返回时覆盖下面重算的 state.page
        invalidatePendingLoad();
        state.videos.splice(i, 1);
        state.total = Math.max(0, state.total - 1);
        clearFeedTrack();
        bindGlobalProgress(null);
        if (!state.videos.length) {
          // resetAndLoad 自带 loadGen 递增与状态复位：
          // 直接 loadVideos(true) 会与在途分页预取请求同代并发，旧响应会污染新列表
          await resetAndLoad();
          return;
        }
        const keep = Math.min(i, state.videos.length - 1);
        // 服务端按 page*limit 偏移分页：已加载列表变短后必须重算下一页，否则会漏一条
        state.page = Math.floor(state.videos.length / state.limit) + 1;
        renderSlides(0);
        goTo(keep, false);
        playCurrent();
        showFeedToast(`已删除${label}`, 1600);
      } catch (ex) {
        $("#edit-error").textContent = ex.message || "删除失败";
      }
    });
