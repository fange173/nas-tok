/* state.js — 全局可变状态与常量(预载窗口、媒体模式)(由 split_frontend.py 机械切割,勿手改顺序) */
import { api, lsGet, lsSet } from './util.js';

    export const savedVol = lsGet("nastok_volume");
    export const savedMuted = lsGet("nastok_muted");
    export const savedMediaMode = lsGet("nastok_media_mode");
    export const savedRate = parseFloat(lsGet("nastok_rate") || "1");
    export const MEDIA_MODES = ["video", "mixed", "images"];
    export const PLAY_MODES = ["order", "random", "loop"];
    // 续播默认关；沉浸播放默认开（仅移动端生效）
    export const savedResumePlayback = lsGet("nastok_resume_playback") === "1";
    export const savedImmersiveMobile = lsGet("nastok_immersive_mobile") !== "0";

    // 旧版只存列表排序(nastok_sort)，迁移：random → 播放顺序「随机」，否则默认「顺序」
    const savedPlayMode = lsGet("nastok_play_mode")
      || (lsGet("nastok_sort") === "random" ? "random" : "")
      || "order";

    export const state = {
      user: null,
      tab: "all",
      playMode: PLAY_MODES.includes(savedPlayMode) ? savedPlayMode : "order",
      // 列表排序由播放顺序派生：随机=乱序列表，顺序/单个循环=按时间
      sort: savedPlayMode === "random" ? "random" : "newest",
      mediaMode: MEDIA_MODES.includes(savedMediaMode) ? savedMediaMode : "video",
      resumePlayback: savedResumePlayback,
      immersiveMobile: savedImmersiveMobile,
      seed: null,
      videos: [],
      index: 0,
      page: 1,
      limit: 8,
      total: 0,
      loading: false,
      hasMore: true,
      loadPromise: null,
      loadGen: 0,
      viewed: new Set(),
      editTarget: null,
      logsPage: 1,
      logsTotal: 0,
      dragY: 0,
      dragging: false,
      startY: 0,
      startX: 0,
      longPressTimer: null,
      isFastForward: false,
      ignoreTapUntil: 0,
      volume: savedVol !== null ? parseFloat(savedVol) : 0,
      muted: savedMuted !== null ? savedMuted === "true" : true,
      fitMode: lsGet("nastok_fit") || "contain",
      progressVideo: null,
      currentVideo: null,
      onTimeUpdate: null,
      scrubbing: false,
      _progressRaf: 0,
      _lastProgressPct: null,
      browserPath: "",
      browserParent: "",
      browserRegistered: false,
      immersive: false,
      sharesPage: 1,
      sharesTotal: 0,
      lastLibraryIds: null,
      _velocity: 0,
      _lastY: 0,
      _lastT: 0,
      _dragCommitted: false,
      _pointerId: null,
      _swipeLock: false,
      _chromeTimer: 0,
      playGen: 0,
      _onAlbum: false,
      _albumAxis: null,
      _albumFeedIdx: -1,
      tagId: null,
      tagged: false,
      tags: null,
      libraryId: Number(lsGet("nastok_library")) || null,
      query: "",
      playbackRate: Number.isFinite(savedRate) && savedRate > 0 ? savedRate : 1,
      _favInflight: Object.create(null),
      _playTimer: 0,
      _userLibsSeq: 0,
    };

    export function itemKind(v) {
      return (v && v.kind) || "video";
    }

    export function currentItem() {
      return state.videos[state.index] || null;
    }

    export function albumImageCount(item) {
      if (!item) return 1;
      if (item.images && item.images.length) return item.images.length;
      return Math.max(1, Number(item.count) || 1);
    }

    // 当前视频优先缓冲；下一条在当前视频稳定播放后提升为可播放预载。
    export const VIDEO_KEEP_BEHIND = 1;
    export const VIDEO_METADATA_AHEAD = 3;
    export const NEXT_PAGE_PREFETCH_THRESHOLD = 3;

    // ---- 逐用户设置:DB 持久化,localStorage 仅作离线兜底 ----
    // DB 键 → state 字段/localStorage 键/类型。真值来源以登录后拉取的 DB 为准。
    // 只收「用户偏好」:排除音量/静音(设备级)、存储库(会话上下文)、帮助提示(一次性)。
    const SETTINGS_MAP = {
      resume_playback: { field: "resumePlayback", ls: "nastok_resume_playback", type: "bool" },
      immersive_mobile: { field: "immersiveMobile", ls: "nastok_immersive_mobile", type: "bool" },
      media_mode: { field: "mediaMode", ls: "nastok_media_mode", type: "enum", values: MEDIA_MODES },
      play_mode: { field: "playMode", ls: "nastok_play_mode", type: "enum", values: PLAY_MODES },
      rate: { field: "playbackRate", ls: "nastok_rate", type: "number" },
      fit_mode: { field: "fitMode", ls: "nastok_fit", type: "enum", values: ["contain", "cover", "fill", "width", "height"] },
    };

    // 归一化服务端值;非法值返回 undefined 表示「忽略该项」
    function _normSetting(c, value) {
      if (c.type === "bool") return !!value;
      if (c.type === "number") {
        const n = Number(value);
        return Number.isFinite(n) && n > 0 ? n : undefined;
      }
      if (c.type === "enum") return c.values.includes(value) ? value : undefined;
      return value == null ? undefined : String(value);
    }

    function _lsEncode(c, value) {
      return c.type === "bool" ? (value ? "1" : "0") : String(value);
    }

    function applyServerSettings(server) {
      server = server || {};
      for (const key in SETTINGS_MAP) {
        if (!(key in server)) continue;
        const c = SETTINGS_MAP[key];
        const v = _normSetting(c, server[key]);
        if (v === undefined) continue;
        state[c.field] = v;
        lsSet(c.ls, _lsEncode(c, v));
      }
    }

    /** 写一项设置:更新 state、镜像 localStorage,并在登录态同步到 DB(单键 patch)。 */
    export function persistSetting(key, value) {
      const c = SETTINGS_MAP[key];
      if (!c) return;
      const v = _normSetting(c, value);
      if (v === undefined) return;
      state[c.field] = v;
      lsSet(c.ls, _lsEncode(c, v));
      if (state.user) {
        api("/api/auth/settings", { method: "PUT", body: JSON.stringify({ settings: { [key]: v } }) }).catch(() => {});
      }
    }

    /** 登录后从服务端拉取逐用户设置并合并到 state;读失败静默(沿用本地兜底)。 */
    export async function loadServerSettings() {
      if (!state.user) return;
      try {
        const s = await api("/api/auth/settings");
        applyServerSettings(s.settings || {});
      } catch (_) { /* 网络/接口异常不影响进入 Feed */ }
    }
