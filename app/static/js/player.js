/* player.js — 音量/画面适配/沉浸模式/全局进度条 + 事件绑定(由 split_frontend.py 机械切割,勿手改顺序) */
import { closeTopMore, nextVideo, prevVideo, resetAndLoad, showFeedToast, streamUrl, syncDesktopPlayerControls, togglePlayPause } from './feed.js';
import { ICON, MEDIA_MODE_ICON, MEDIA_MODE_LABEL } from './icons.js';
import { MEDIA_MODES, VIDEO_KEEP_BEHIND, VIDEO_METADATA_AHEAD, albumImageCount, currentItem, itemKind, persistSetting, state } from './state.js';
import { $, $$, api, fmtTime, isMobileFeed, lsGet, lsSet } from './util.js';

    /* ---------- 音量 / 画面模式 ---------- */
    export function persistAudio() {
      lsSet("nastok_volume", String(state.volume));
      lsSet("nastok_muted", String(state.muted));
    }

    export function updateMuteBtn() {
      const icon = state.muted ? ICON.mute() : ICON.volume();
      const title = state.muted ? "取消静音" : "静音";
      const btn = $("#mute-btn");
      if (btn) {
        btn.innerHTML = icon;
        btn.title = title;
        btn.setAttribute("aria-label", title);
      }
      const dockMute = $("#dock-mute-btn");
      if (dockMute) {
        dockMute.innerHTML = icon;
        dockMute.title = title;
        dockMute.setAttribute("aria-label", title);
        dockMute.classList.toggle("is-muted", !!state.muted);
      }
    }

    export function syncScrubTime(cur, dur) {
      const text = `${fmtTime(cur || 0)} / ${fmtTime(dur || 0)}`;
      const a = $("#global-time");
      const b = $("#scrub-time");
      if (a) a.textContent = text;
      if (b) b.textContent = text;
    }

    export function syncMediaLabel(cur, total) {
      const text = `${cur} / ${total}`;
      const a = $("#global-time");
      const b = $("#scrub-time");
      if (a) a.textContent = text;
      if (b) b.textContent = text;
    }

    export function updateDockForKind(kind) {
      // 图片也保留音量控件（视觉常驻、布局稳定）；音量操作仅对视频生效(handler 按 kind 守卫)
      const dockMute = $("#dock-mute-btn");
      if (dockMute) {
        dockMute.disabled = kind !== "video";
        dockMute.title = kind === "video" ? (state.muted ? "取消静音" : "静音") : "图片无声音";
      }
    }

    // 底栏左下角标题（PC 端显示，替代 slide 内的 video-meta）
    export function setDockTitle(title) {
      const el = $("#dock-title");
      if (el) el.textContent = title || "";
    }

    // 合集图片按需加载：列表接口不再内联 images，首次用到时拉取并缓存回 state.videos
    export function ensureAlbumImages(feedIdx) {
      const item = state.videos[feedIdx];
      if (!item || itemKind(item) !== "album") return Promise.resolve(null);
      if (Array.isArray(item.images) && item.images.length) return Promise.resolve(item.images);
      if (item._imagesPromise) return item._imagesPromise;
      item._imagesPromise = api(`/api/albums/images?path=${encodeURIComponent(item.path)}`)
        .then((data) => {
          item.images = data.images || [];
          return item.images;
        })
        .catch(() => {
          // 失败不缓存为空列表（否则瞬时错误被永久显示为「相册为空」），返回 null 让调用方提示可重试
          return null;
        })
        .finally(() => { item._imagesPromise = null; });
      return item._imagesPromise;
    }

    export function fillAlbumTrack(slide, item, images) {
      const albumTrack = slide.querySelector(".album-track");
      if (!albumTrack) return;
      albumTrack.innerHTML = "";
      if (!images.length) {
        const cell = document.createElement("div");
        cell.className = "album-slide";
        cell.innerHTML = '<div class="error-msg" style="color:#fff;text-align:center">相册为空</div>';
        albumTrack.appendChild(cell);
        return;
      }
      images.forEach((im) => {
        const cell = document.createElement("div");
        cell.className = "album-slide";
        const img = document.createElement("img");
        img.className = "feed-image";
        img.alt = im.title || "";
        img.draggable = false;
        img.loading = "lazy";
        img.src = streamUrl(im);
        cell.appendChild(img);
        albumTrack.appendChild(cell);
      });
      // 维持当前停留的图片位置
      const idx = Number(slide.dataset.albumIdx || 0);
      updateAlbumProgress(slide, idx, images.length);
    }

    export function updateAlbumProgress(slide, idx, n) {
      const count = Math.max(1, n || 1);
      const i = Math.min(count - 1, Math.max(0, idx));
      if (slide) {
        slide.dataset.albumIdx = String(i);
        const albumTrack = slide.querySelector(".album-track");
        if (albumTrack) {
          albumTrack.style.transition = "";
          albumTrack.style.transform = `translateX(${-i * 100}%)`;
        }
      }
      // 只在相册是当前条目时写全局进度/页码；构建非当前相册 slide 时不得覆盖当前条目显示
      if (!slide || String(slide.dataset.index) !== String(state.index)) return;
      setProgressUI(count ? ((i + 1) / count) * 100 : 0);
      syncMediaLabel(i + 1, count);
    }

    export function setAlbumIndex(feedIdx, imgIdx, animate) {
      const slide = $(`#feed-track .video-slide[data-index="${feedIdx}"]`);
      const item = state.videos[feedIdx];
      if (!slide || !item || itemKind(item) !== "album") return;
      const n = albumImageCount(item);
      const i = Math.min(n - 1, Math.max(0, imgIdx));
      // 回写到条目：slide 被窗口修剪重建后可恢复停留位置
      item._albumIdx = i;
      const albumTrack = slide.querySelector(".album-track");
      if (albumTrack && animate === false) albumTrack.style.transition = "none";
      updateAlbumProgress(slide, i, n);
      if (albumTrack && animate === false) {
        void albumTrack.offsetHeight;
        albumTrack.style.transition = "";
      }
    }

    export function bumpChrome(opts) {
      const force = opts && opts.force;
      const page = $("#page-feed");
      page.classList.remove("chrome-dimmed");
      if (state._chromeTimer) {
        clearTimeout(state._chromeTimer);
        state._chromeTimer = 0;
      }
      if (!isMobileFeed() || state.immersive || !state.immersiveMobile) return;
      const video = getCurrentVideo();
      const playing = video && !video.paused;
      if (!force && !playing) return;
      state._chromeTimer = setTimeout(() => {
        state._chromeTimer = 0;
        if (state.immersive || state.scrubbing) return;
        if (document.querySelector(".top-more.open")) return;
        const v = getCurrentVideo();
        if (v && !v.paused) page.classList.add("chrome-dimmed");
      }, 2600);
    }

    export function revealChrome() {
      bumpChrome({ force: true });
    }

    export function applyAudioToVideos() {
      // 只同步窗口内视频，避免扫全表
      $$("#feed-track .video-slide").forEach((slide) => {
        const i = Number(slide.dataset.index);
        if (i < state.index - VIDEO_KEEP_BEHIND || i > state.index + VIDEO_METADATA_AHEAD) return;
        const v = slide.querySelector("video");
        if (!v || !v.getAttribute("src")) return;
        v.volume = state.volume;
        v.muted = state.muted;
      });
      // 静音时滑块仍显示记忆音量，避免误解为音量被清零
      $("#volume-slider").value = String(state.volume);
      updateMuteBtn();
    }

    const POS_KEY = "nastok_pos";
    export function readWatchPos() {
      try { return JSON.parse(lsGet(POS_KEY) || "{}") || {}; } catch (_) { return {}; }
    }
    export function saveWatchPos(path, t, d) {
      if (!state.resumePlayback) return;
      if (!path || !d || !Number.isFinite(t)) return;
      if (t < 3 || t > d - 5) {
        const all = readWatchPos();
        if (all[path]) { delete all[path]; lsSet(POS_KEY, JSON.stringify(all)); }
        return;
      }
      const all = readWatchPos();
      all[path] = { t, d, at: Date.now() };
      const keys = Object.keys(all);
      if (keys.length > 200) {
        keys.sort((a, b) => (all[a].at || 0) - (all[b].at || 0));
        keys.slice(0, keys.length - 200).forEach((k) => delete all[k]);
      }
      lsSet(POS_KEY, JSON.stringify(all));
    }
    export function resumeWatchPos(video, path) {
      if (!state.resumePlayback) return;
      const rec = readWatchPos()[path];
      if (!rec || !video || !video.duration) return;
      if (rec.t > 3 && rec.t < video.duration - 5) {
        try { video.currentTime = rec.t; } catch (_) {}
        if (video.dataset.resumeToast !== path) {
          video.dataset.resumeToast = path;
          showFeedToast(`续播 ${fmtTime(rec.t)}`, 1200, true);
        }
      }
    }
    export function syncRateButtons() {
      const n = Number(state.playbackRate) || 1;
      const sel = $("#rate-select");
      if (sel) sel.value = String(n);
    }
    export function setPlaybackRate(rate) {
      persistSetting("rate", rate);
      const n = Number(state.playbackRate) || 1;
      syncRateButtons();
      $$("#feed-track video").forEach((v) => { try { v.playbackRate = n; } catch (_) {} });
      const toast = $("#speed-toast");
      if (toast && n !== 1) {
        toast.textContent = n + "x";
        toast.classList.add("show");
        clearTimeout(toast._t);
        toast._t = setTimeout(() => toast.classList.remove("show"), 900);
      }
    }
    export function applyFitMode() {
      const vp = $("#feed-viewport");
      vp.classList.remove("fit-contain", "fit-cover", "fit-fill", "fit-width", "fit-height");
      vp.classList.add("fit-" + state.fitMode);
      $("#fit-mode").value = state.fitMode;
    }

    const PLAY_MODE_META = {
      order: { label: "顺序", icon: () => ICON.listOrder(), sort: "newest" },
      random: { label: "随机", icon: () => ICON.shuffle(), sort: "random" },
      loop: { label: "单个循环", icon: () => ICON.repeatOne(), sort: "newest" },
    };

    export function setPlayMode(mode, reload, persist) {
      if (!PLAY_MODE_META[mode]) mode = "order";
      state.playMode = mode;
      if (persist) persistSetting("play_mode", mode);
      const newSort = PLAY_MODE_META[mode].sort;
      const sortChanged = newSort !== state.sort;
      state.sort = newSort;
      const btn = $("#dock-sort-btn");
      if (btn) {
        const meta = PLAY_MODE_META[mode];
        btn.innerHTML = meta.icon();
        btn.title = `播放顺序：${meta.label}（点按切换）`;
        btn.setAttribute("aria-label", `切换播放顺序，当前${meta.label}`);
      }
      // 顺序↔单个循环 列表都是按时间排，无需重载；切进/切出随机才重排列表
      if (reload && sortChanged) {
        state.seed = null;
        resetAndLoad();
      }
    }

    export function setMediaMode(mode, reload, persist) {
      state.mediaMode = MEDIA_MODES.includes(mode) ? mode : "video";
      if (persist) persistSetting("media_mode", state.mediaMode);
      const btn = $("#media-mode-btn");
      if (btn) {
        const label = MEDIA_MODE_LABEL[state.mediaMode] || "仅视频";
        const iconFn = MEDIA_MODE_ICON[state.mediaMode] || MEDIA_MODE_ICON.video;
        btn.innerHTML = iconFn() + '<span class="btn-label">' + label + '</span>';
        btn.title = `当前：${label}（点按切换）`;
        btn.setAttribute("aria-label", `切换媒体类型，当前${label}`);
        btn.dataset.mode = state.mediaMode;
      }
      if (reload) {
        state.seed = null;
        resetAndLoad();
      }
    }

    $("#func-menu-btn").innerHTML = ICON.user() + '<span class="btn-label">账户</span>';
    $("#top-more-btn").innerHTML = ICON.user() + '<span class="btn-label">账户</span>';
    $("#search-btn").innerHTML = ICON.grid() + '<span class="btn-label">网格</span>';
    $("#settings-btn").innerHTML = ICON.gear() + '<span class="btn-label">设置</span>';
    $("#dock-more-btn").innerHTML = ICON.more();
    updateMuteBtn();
    setPlayMode(state.playMode, false);
    setMediaMode(state.mediaMode, false);

    // 桌面端：右下角 dock-actions(播放顺序/更多)并入底部控制条、排在全屏按钮左侧；
    // 移动端保持右下角悬浮。跨断点来回搬同一个节点,事件绑定与菜单结构不受影响。
    {
      const dockActions = $("#dock-actions");
      const fsBtn = $("#fullscreen-btn");
      if (dockActions && fsBtn) {
        const home = { parent: dockActions.parentElement, next: dockActions.nextSibling };
        const mq = window.matchMedia("(min-width: 641px)");
        const place = () => {
          if (mq.matches) {
            fsBtn.parentElement.insertBefore(dockActions, fsBtn);
          } else if (dockActions.parentElement !== home.parent) {
            home.parent.insertBefore(dockActions, home.next);
          }
        };
        place();
        mq.addEventListener("change", place);
      }
    }

    $("#volume-slider").addEventListener("input", (e) => {
      if (itemKind(currentItem()) !== "video") return;
      const val = parseFloat(e.target.value);
      state.volume = val;
      state.muted = val === 0;
      persistAudio();
      applyAudioToVideos();
    });

    $("#mute-btn").addEventListener("click", () => {
      if (itemKind(currentItem()) !== "video") return;
      if (state.muted) {
        state.muted = false;
        if (state.volume === 0) state.volume = 0.5;
      } else {
        state.muted = true;
      }
      persistAudio();
      applyAudioToVideos();
    });

    $("#fit-mode").addEventListener("change", (e) => {
      persistSetting("fit_mode", e.target.value);
      applyFitMode();
    });

    /* 设置面板开关：续播(默认关) / 沉浸播放(默认开,仅移动端生效) */
    function syncSettingsSwitches() {
      const r = $("#resume-playback-switch");
      if (r) r.checked = state.resumePlayback;
      const i = $("#immersive-playback-switch");
      if (i) i.checked = state.immersiveMobile;
    }
    export { syncSettingsSwitches };
    /** 登录拉取 DB 设置后,把各设置值刷新到界面(纯渲染,不触发持久化)。 */
    export function syncSettingsUi() {
      syncSettingsSwitches();
      syncRateButtons();
      applyFitMode();
      setPlayMode(state.playMode, false);
      setMediaMode(state.mediaMode, false);
    }
    $("#resume-playback-switch")?.addEventListener("change", (e) => {
      persistSetting("resume_playback", !!e.target.checked);
    });
    $("#immersive-playback-switch")?.addEventListener("change", (e) => {
      persistSetting("immersive_mobile", !!e.target.checked);
      if (state.immersiveMobile) showFeedToast("沉浸播放仅移动端生效", 2000);
      else revealChrome();
    });
    syncSettingsSwitches();

    // 桌面播放控制栏按钮
    $("#pc-prev-btn").innerHTML = ICON.prev();
    $("#pc-next-btn").innerHTML = ICON.next();
    $("#pc-prev-btn").addEventListener("click", () => {
      if (state._swipeLock) return;
      prevVideo();
    });
    $("#pc-next-btn").addEventListener("click", () => {
      if (state._swipeLock) return;
      nextVideo();
    });
    $("#pc-playpause-btn").addEventListener("click", () => {
      const video = getCurrentVideo();
      const slide = video ? video.closest(".video-slide") : null;
      togglePlayPause(video, slide);
      syncDesktopPlayerControls();
    });

    export function syncImmersiveUi() {
      const btn = $("#fullscreen-btn");
      const exitBtn = $("#exit-immersive-btn");
      const on = !!state.immersive && !isMobileFeed();
      const page = $("#page-feed");
      page.classList.toggle("is-immersive", on);
      if (on) page.classList.remove("chrome-dimmed");
      if (exitBtn) {
        exitBtn.classList.toggle("hidden", !on);
        if (on) exitBtn.innerHTML = ICON.fullscreenExit() + "<span>退出全屏</span>";
      }
      if (!btn) return;
      const fsIcon = on ? ICON.fullscreenExit() : ICON.fullscreen();
      const fsTitle = on ? "退出全屏" : "全屏";
      btn.innerHTML = fsIcon;
      btn.setAttribute("title", fsTitle);
      btn.setAttribute("aria-label", fsTitle);
    }

    export function enterImmersive() {
      // 移动端不做全屏（系统 API 不可靠），仅桌面
      if (isMobileFeed()) return;
      if (state.immersive) return;
      state.immersive = true;
      closeTopMore();
      syncImmersiveUi();
      const el = $("#page-feed");
      if (!document.fullscreenElement) {
        const req = el.requestFullscreen || el.webkitRequestFullscreen;
        if (req) {
          try {
            const p = req.call(el);
            if (p && typeof p.catch === "function") p.catch(() => {});
          } catch (_) {}
        }
      }
    }

    export function exitImmersive() {
      if (!state.immersive) return;
      state.immersive = false;
      syncImmersiveUi();
      if (document.fullscreenElement || document.webkitFullscreenElement) {
        const exit = document.exitFullscreen || document.webkitExitFullscreen;
        if (exit) {
          try {
            const p = exit.call(document);
            if (p && typeof p.catch === "function") p.catch(() => {});
          } catch (_) {}
        }
      }
      bumpChrome();
    }

    export function toggleImmersive() {
      if (isMobileFeed()) return;
      if (state.immersive) exitImmersive();
      else enterImmersive();
    }

    $("#fullscreen-btn").innerHTML = ICON.fullscreen();
    $("#fullscreen-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleImmersive();
    });
    export const exitImmersiveBtn = $("#exit-immersive-btn");
    if (exitImmersiveBtn) {
      exitImmersiveBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        exitImmersive();
      });
    }
    export const onFsChange = () => {
      if (isMobileFeed()) {
        state.immersive = false;
        syncImmersiveUi();
        return;
      }
      const fs = !!(document.fullscreenElement || document.webkitFullscreenElement);
      if (!fs && state.immersive) {
        state.immersive = false;
        syncImmersiveUi();
      } else if (fs && !state.immersive) {
        state.immersive = true;
        syncImmersiveUi();
      } else {
        syncImmersiveUi();
      }
    };
    document.addEventListener("fullscreenchange", onFsChange);
    document.addEventListener("webkitfullscreenchange", onFsChange);

    updateMuteBtn();
    $("#volume-slider").value = String(state.volume || 0);
    applyFitMode();
    setPlayMode(state.playMode, false);
    if ($("#rate-select")) {
      $("#rate-select").addEventListener("change", () => setPlaybackRate($("#rate-select").value));
    }
    syncRateButtons();


    /* ---------- 全局进度条 ---------- */
    export function getCurrentVideo() {
      const slide = $(`#feed-track .video-slide[data-index="${state.index}"]`);
      return slide ? slide.querySelector("video") : null;
    }

    export function bindGlobalProgress(video) {
      if (state.progressVideo && state.onTimeUpdate) {
        state.progressVideo.removeEventListener("timeupdate", state.onTimeUpdate);
      }
      if (state._progressRaf) {
        cancelAnimationFrame(state._progressRaf);
        state._progressRaf = 0;
      }
      state.progressVideo = video;
      state.onTimeUpdate = () => {
        if (state.scrubbing) return;
        if (!video || !video.duration) return;
        if (state._progressRaf) return;
        state._progressRaf = requestAnimationFrame(() => {
          state._progressRaf = 0;
          if (!video || !video.duration || state.scrubbing) return;
          const pct = (video.currentTime / video.duration) * 100;
          // 变化极小时跳过 DOM 写入
          if (state._lastProgressPct != null && Math.abs(pct - state._lastProgressPct) < 0.35) return;
          state._lastProgressPct = pct;
          setProgressUI(pct);
          syncScrubTime(video.currentTime, video.duration);
          const bar = $("#global-progress");
          if (bar) bar.setAttribute("aria-valuenow", String(Math.round(pct)));
          const path = video.dataset.path;
          if (path && Math.floor(video.currentTime) % 8 === 0) {
            saveWatchPos(path, video.currentTime, video.duration);
          }
        });
      };
      if (video) {
        video.addEventListener("timeupdate", state.onTimeUpdate);
        state._lastProgressPct = null;
        state.onTimeUpdate();
      } else {
        setProgressUI(0);
        syncScrubTime(0, 0);
      }
    }

    export function setProgressUI(pct) {
      const p = Math.min(100, Math.max(0, pct));
      $("#global-progress-fill").style.width = p + "%";
      const thumb = $("#global-progress-thumb");
      if (thumb) thumb.style.left = p + "%";
    }

    export function seekFromClientX(clientX) {
      const bar = $("#global-progress");
      if (!bar) return;
      const rect = bar.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      const item = currentItem();
      const kind = itemKind(item);
      if (kind === "album") {
        const n = albumImageCount(item);
        const idx = Math.min(n - 1, Math.max(0, Math.floor(ratio * n - 1e-9)));
        setAlbumIndex(state.index, idx);
        return;
      }
      if (kind === "image") {
        setProgressUI(100);
        syncMediaLabel(1, 1);
        return;
      }
      const video = getCurrentVideo();
      if (!video || !video.duration) return;
      video.currentTime = ratio * video.duration;
      setProgressUI(ratio * 100);
      syncScrubTime(video.currentTime, video.duration);
    }

    (function bindProgressScrub() {
      const bar = $("#global-progress");
      if (!bar) return;

      const onStart = (clientX) => {
        state.scrubbing = true;
        bar.classList.add("is-dragging");
        $("#bottom-dock").classList.add("is-scrubbing");
        revealChrome();
        seekFromClientX(clientX);
      };
      const onMove = (clientX) => {
        if (!state.scrubbing) return;
        seekFromClientX(clientX);
      };
      const onEnd = () => {
        if (!state.scrubbing) return;
        state.scrubbing = false;
        bar.classList.remove("is-dragging");
        $("#bottom-dock").classList.remove("is-scrubbing");
        bumpChrome();
      };

      bar.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        bar.setPointerCapture(ev.pointerId);
        onStart(ev.clientX);
      });
      bar.addEventListener("pointermove", (ev) => {
        if (!state.scrubbing) return;
        onMove(ev.clientX);
      });
      bar.addEventListener("pointerup", onEnd);
      bar.addEventListener("pointercancel", onEnd);
      bar.addEventListener("lostpointercapture", onEnd);
      bar.addEventListener("keydown", (ev) => {
        const video = getCurrentVideo();
        if (!video || !video.duration) return;
        if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
          ev.preventDefault();
          ev.stopPropagation();
          video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + (ev.key === "ArrowRight" ? 5 : -5)));
        }
      });
    })();

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        const v = getCurrentVideo();
        if (v && !v.paused) {
          v.pause();
          state._resumeOnVisible = true;
        }
      } else if (state._resumeOnVisible) {
        state._resumeOnVisible = false;
        const v = getCurrentVideo();
        if (v) v.play().catch(() => {});
      }
    });

    export function bindMediaSession(info, video) {
      if (!navigator.mediaSession || !info) return;
      try {
        navigator.mediaSession.metadata = new MediaMetadata({
          title: info.title || "NasTok",
          artist: "NasTok",
          album: info.library || "",
        });
        navigator.mediaSession.setActionHandler("play", () => video && video.play().catch(() => {}));
        navigator.mediaSession.setActionHandler("pause", () => video && video.pause());
        navigator.mediaSession.setActionHandler("previoustrack", () => prevVideo());
        navigator.mediaSession.setActionHandler("nexttrack", () => nextVideo());
        navigator.mediaSession.setActionHandler("seekbackward", () => { if (video) video.currentTime = Math.max(0, video.currentTime - 10); });
        navigator.mediaSession.setActionHandler("seekforward", () => { if (video) video.currentTime = Math.min(video.duration || 0, video.currentTime + 10); });
      } catch (_) {}
    }
