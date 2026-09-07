/* state.js — 全局可变状态与常量(预载窗口、媒体模式)(由 split_frontend.py 机械切割,勿手改顺序) */
import { lsGet } from './util.js';

    export const savedVol = lsGet("nastok_volume");
    export const savedMuted = lsGet("nastok_muted");
    export const savedMediaMode = lsGet("nastok_media_mode");
    export const savedRate = parseFloat(lsGet("nastok_rate") || "1");
    export const MEDIA_MODES = ["video", "mixed", "images"];

    export const state = {
      user: null,
      tab: "all",
      sort: lsGet("nastok_sort") === "newest" ? "newest" : "random",
      mediaMode: MEDIA_MODES.includes(savedMediaMode) ? savedMediaMode : "video",
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
      query: "",
      libraryId: null,
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
