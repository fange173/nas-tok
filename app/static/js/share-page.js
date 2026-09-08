/* share-page.js — 公开分享页 + 分享/取消分享数据流(由 split_frontend.py 机械切割,勿手改顺序) */
import { showFeedToast } from './feed.js';
import { ICON } from './icons.js';
import { state } from './state.js';
import { $, $$, api, copyText, showPage } from './util.js';

    let _shareTarget = null;
    export async function shareVideo(videoInfo) {
      if (!videoInfo || !videoInfo.path) return false;
      _shareTarget = videoInfo;
      $("#share-modal-sub").textContent = videoInfo.title || videoInfo.path;
      // 默认 7 天有效期(不默认永久);用户仍可手动选回永久
      $("#share-expire").value = "168";
      $("#share-pass").value = "";
      $("#share-max-views").value = "";
      $("#share-modal").classList.add("open");
      return true;
    }
    async function confirmShare() {
      const videoInfo = _shareTarget;
      if (!videoInfo || !videoInfo.path) return false;
      const body = { path: videoInfo.path };
      const hours = $("#share-expire").value;
      if (hours) body.expires_in_hours = Number(hours);
      const pass = ($("#share-pass").value || "").trim();
      if (pass) body.password = pass;
      const maxv = ($("#share-max-views").value || "").trim();
      if (maxv) body.max_views = Number(maxv);
      try {
        const data = await api("/api/shares", {
          method: "POST",
          body: JSON.stringify(body),
        });
        syncFeedShareState(videoInfo.path, true);
        const url = data.url || `${location.origin}/s/${data.token}`;
        const ok = await copyText(url);
        const tip = data.rotated ? "已重新分享（新链接）并复制" : (data.restored ? "已恢复分享并复制链接" : "分享链接已复制");
        showFeedToast(ok ? tip : `分享链接：${url}`, ok ? 2200 : 5000);
        $("#share-modal").classList.remove("open");
        _shareTarget = null;
        return true;
      } catch (ex) {
        showFeedToast(ex.message || "分享失败", 2500);
        return false;
      }
    }
    $("#share-modal-ok")?.addEventListener("click", confirmShare);
    $("#share-modal-cancel")?.addEventListener("click", () => {
      $("#share-modal").classList.remove("open");
      _shareTarget = null;
    });
    $("#share-modal")?.addEventListener("click", (e) => {
      if (e.target === $("#share-modal")) {
        $("#share-modal").classList.remove("open");
        _shareTarget = null;
      }
    });

    export async function unshareVideo(videoInfo) {
      if (!videoInfo || !videoInfo.path) return false;
      try {
        await api("/api/shares", {
          method: "DELETE",
          body: JSON.stringify({ path: videoInfo.path }),
        });
        syncFeedShareState(videoInfo.path, false);
        showFeedToast("已取消分享", 1800);
        return true;
      } catch (ex) {
        showFeedToast(ex.message || "取消分享失败", 2500);
        return false;
      }
    }

    export function setShareMenuItem(shareItem, shared) {
      if (!shareItem) return;
      const on = !!shared;
      shareItem.dataset.shared = on ? "1" : "0";
      shareItem.innerHTML = `${ICON.share()}<span>${on ? "取消分享" : "分享"}</span>`;
    }

    export function syncFeedShareState(path, shared) {
      if (!path) return;
      const on = !!shared;
      state.videos.forEach((v) => {
        if (v && v.path === path) v.shared = on;
      });
      $$(".side-more-item[data-share-path]").forEach((btn) => {
        if (btn.dataset.sharePath === path) setShareMenuItem(btn, on);
      });
    }

    function shareAlbumCount() {
      const album = $("#share-album");
      return album ? album.querySelectorAll(".album-slide").length : 0;
    }

    function setShareAlbumIndex(i, animate) {
      const album = $("#share-album");
      if (!album || album.classList.contains("hidden")) return;
      const n = shareAlbumCount();
      const idx = Math.max(0, Math.min(Math.max(0, n - 1), i));
      album.dataset.idx = String(idx);
      album.style.transition = animate === false ? "none" : "";
      album.style.transform = `translateX(${-idx * 100}%)`;
      const wrap = album.parentElement;
      let indicator = wrap && wrap.querySelector(".album-page-indicator");
      if (n > 1) {
        if (!indicator && wrap) {
          indicator = document.createElement("div");
          indicator.className = "album-page-indicator";
          wrap.appendChild(indicator);
        }
        if (indicator) {
          indicator.classList.remove("hidden");
          indicator.textContent = `${idx + 1} / ${n}`;
        }
      } else if (indicator) {
        indicator.classList.add("hidden");
      }
    }

    function bindShareAlbumSwipe() {
      const album = $("#share-album");
      const wrap = album && album.parentElement;
      if (!wrap || wrap.dataset.albumSwipe === "1") return;
      wrap.dataset.albumSwipe = "1";
      let dragging = false;
      let axis = null;
      let pid = 0;
      let x0 = 0;
      let y0 = 0;
      let startIdx = 0;
      wrap.addEventListener("pointerdown", (e) => {
        if (!album || album.classList.contains("hidden")) return;
        if (e.button && e.button !== 0) return;
        dragging = true;
        axis = null;
        pid = e.pointerId;
        x0 = e.clientX;
        y0 = e.clientY;
        startIdx = Number(album.dataset.idx || 0);
        try { wrap.setPointerCapture(pid); } catch (_) {}
      });
      wrap.addEventListener("pointermove", (e) => {
        if (!dragging || e.pointerId !== pid) return;
        const dx = e.clientX - x0;
        const dy = e.clientY - y0;
        if (axis == null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
          axis = Math.abs(dx) >= Math.abs(dy) ? "h" : "v";
        }
        if (axis !== "h") return;
        const w = wrap.clientWidth || 1;
        album.style.transition = "none";
        album.style.transform = `translateX(${-startIdx * 100 + (dx / w) * 100}%)`;
      });
      const end = (e) => {
        if (!dragging || (e.pointerId && e.pointerId !== pid)) return;
        dragging = false;
        const dx = e.clientX - x0;
        const w = wrap.clientWidth || 1;
        if (axis === "h" && Math.abs(dx) > w * 0.18) {
          setShareAlbumIndex(startIdx + (dx < 0 ? 1 : -1), true);
        } else {
          setShareAlbumIndex(startIdx, true);
        }
        axis = null;
      };
      wrap.addEventListener("pointerup", end);
      wrap.addEventListener("pointercancel", end);
      // 桌面键盘翻页（与 feed 页方向键约定一致）；仅在分享页且相册可见时生效
      document.addEventListener("keydown", (e) => {
        if (!$("#page-share").classList.contains("active")) return;
        if (!album || album.classList.contains("hidden")) return;
        const tag = (e.target && e.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
        if (e.key === "ArrowLeft") {
          e.preventDefault();
          setShareAlbumIndex(Number(album.dataset.idx || 0) - 1, true);
        } else if (e.key === "ArrowRight") {
          e.preventDefault();
          setShareAlbumIndex(Number(album.dataset.idx || 0) + 1, true);
        }
      });
    }

    function mountShareMedia(data, token) {
      const video = $("#share-video");
      const img = $("#share-image");
      const album = $("#share-album");
      const loader = $("#share-loader");
      // 图片/相册没有可播放的音视频，播放与静音按钮点了无效，直接隐藏
      const isVideoMedia = data.kind !== "album" && data.kind !== "image";
      $("#share-play-btn")?.classList.toggle("hidden", !isVideoMedia);
      $("#share-mute-btn")?.classList.toggle("hidden", !isVideoMedia);
      video.classList.add("hidden");
      img.classList.add("hidden");
      album.classList.add("hidden");
      album.innerHTML = "";
      album.style.transform = "";
      const wrap = album.parentElement;
      const oldInd = wrap && wrap.querySelector(".album-page-indicator");
      if (oldInd) oldInd.remove();
      if (data.kind === "album" && Array.isArray(data.images)) {
        album.classList.remove("hidden");
        data.images.forEach((im) => {
          const cell = document.createElement("div");
          cell.className = "album-slide";
          const el = document.createElement("img");
          el.src = im.stream_url;
          el.alt = im.title || "";
          el.className = "feed-image";
          el.loading = "lazy";
          cell.appendChild(el);
          album.appendChild(cell);
        });
        bindShareAlbumSwipe();
        setShareAlbumIndex(0, false);
        loader.classList.add("hidden");
        return;
      }
      if (data.kind === "image") {
        img.classList.remove("hidden");
        img.src = data.stream_url;
        img.onload = () => loader.classList.add("hidden");
        img.onerror = () => {
          loader.classList.add("hidden");
          $("#share-error-msg").textContent = "图片加载失败";
          $("#share-error").classList.add("show");
        };
        return;
      }
      video.classList.remove("hidden");
      video.src = data.stream_url || `/api/share/${encodeURIComponent(token)}/stream`;
      video.muted = true;
      video.playsInline = true;
      video.controls = true;
      const play = () => video.play().catch(() => {});
      video.addEventListener("canplay", () => {
        loader.classList.add("hidden");
        play();
      }, { once: true });
      video.addEventListener("error", () => {
        loader.classList.add("hidden");
        $("#share-error-msg").textContent = "视频加载失败";
        $("#share-error").classList.add("show");
      }, { once: true });
      video.addEventListener("click", () => {
        if (video.paused) video.play().catch(() => {});
        else video.pause();
      });
      video.load();
      updateShareMuteBtn();
    }

    export async function openPublicShare(token) {
      showPage("page-share");
      const loader = $("#share-loader");
      const errBox = $("#share-error");
      const errMsg = $("#share-error-msg");
      const pwCard = $("#share-password-card");
      loader.classList.remove("hidden");
      errBox.classList.remove("show");
      pwCard.classList.add("hidden");
      $("#share-title").textContent = "加载中…";
      $("#share-sub").textContent = "NasTok 公开分享";
      try {
        const data = await api(`/api/share/${encodeURIComponent(token)}`);
        $("#share-title").textContent = data.title || "分享";
        $("#share-sub").textContent = data.view_count != null ? `已打开 ${data.view_count} 次` : "NasTok 公开分享";
        state._shareToken = token;
        state._shareUrl = `${location.origin}/s/${token}`;
        if (data.needs_password) {
          loader.classList.add("hidden");
          pwCard.classList.remove("hidden");
          return;
        }
        mountShareMedia(data, token);
      } catch (ex) {
        loader.classList.add("hidden");
        $("#share-title").textContent = "无法打开分享";
        errMsg.textContent = ex.message || "分享无效或视频已删除";
        errBox.classList.add("show");
      }
    }

    export function updateShareMuteBtn() {
      const video = $("#share-video");
      const btn = $("#share-mute-btn");
      if (!video || !btn) return;
      btn.textContent = video.muted ? "取消静音" : "静音";
    }

    $("#share-play-btn")?.addEventListener("click", () => {
      const video = $("#share-video");
      if (!video || video.classList.contains("hidden")) return;
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    });
    $("#share-mute-btn").addEventListener("click", () => {
      const video = $("#share-video");
      video.muted = !video.muted;
      if (!video.muted && video.volume === 0) video.volume = 0.8;
      updateShareMuteBtn();
      video.play().catch(() => {});
    });
    $("#share-password-btn")?.addEventListener("click", async () => {
      const token = state._shareToken;
      if (!token) return;
      try {
        const data = await api(`/api/share/${encodeURIComponent(token)}/auth`, {
          method: "POST",
          body: JSON.stringify({ password: $("#share-password-input").value }),
        });
        $("#share-password-card").classList.add("hidden");
        mountShareMedia(data, token);
      } catch (ex) {
        showFeedToast(ex.message || "密码错误", 2200);
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) return;
      const video = $("#share-video");
      if (video && !video.paused) video.pause();
    });
    $("#share-copy-btn").addEventListener("click", async () => {
      const url = state._shareUrl || location.href;
      const ok = await copyText(url);
      $("#share-sub").textContent = ok ? "链接已复制" : url;
    });

    document.addEventListener("click", (e) => {
      if (e.target.closest(".side-more")) return;
      $$(".side-more-menu").forEach((m) => m.classList.add("hidden"));
    });
