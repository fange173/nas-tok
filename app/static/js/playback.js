/* playback.js — 判断当前设备能否直接播放某个视频;不能时请服务端转换并轮询进度 */
import { api } from './util.js';

    const tester = typeof document !== "undefined" && document.createElement ? document.createElement("video") : null;
    const canPlay = (type) => {
      try { return !!(tester && tester.canPlayType && tester.canPlayType(type)); } catch (_) { return false; }
    };

    /** 浏览器原生支持的容器(按扩展名);其他容器(mkv/avi/wmv…)一律转封装 */
    export function nativeContainer(path) {
      const ext = (String(path || "").split(".").pop() || "").toLowerCase();
      if (["mp4", "m4v", "mov"].includes(ext)) return "mp4";
      if (ext === "webm" && canPlay("video/webm")) return "webm";
      return null;
    }

    // ffprobe 编码 → canPlayType 用的 codecs 字符串
    function videoType(v) {
      const hi = (v.bit_depth || 8) > 8;
      switch (v.codec) {
        case "h264": return `video/mp4; codecs="${hi ? "avc1.6E0028" : "avc1.640028"}"`;
        case "hevc": return `video/mp4; codecs="${hi ? "hvc1.2.4.L120.90" : "hvc1.1.6.L120.90"}"`;
        case "vp9": return 'video/webm; codecs="vp9"';
        case "av1": return 'video/mp4; codecs="av01.0.05M.08"';
        default: return null;
      }
    }

    function audioOk(a, container) {
      if (!a) return true;
      switch (a.codec) {
        case "aac": case "mp3": return true;
        case "opus": case "vorbis": return container === "webm";
        case "ac3": return canPlay('audio/mp4; codecs="ac-3"');
        case "eac3": return canPlay('audio/mp4; codecs="ec-3"');
        default: return false;
      }
    }

    /** 由编码信息得出播放方式:direct 直接播 / remux 转封装 / transcode 完整转码 */
    export function decidePlan(path, info) {
      if (!info || !info.ffmpeg || !info.probe || !info.probe.video) return "direct";
      const v = info.probe.video;
      const container = nativeContainer(path);
      const vt = videoType(v);
      const videoOk = !!vt && canPlay(vt);
      // Safari 只认 hvc1 标记的 HEVC;转封装时会统一改成 hvc1
      const tagOk = v.codec !== "hevc" || v.tag === "hvc1";
      if (container && videoOk && tagOk && audioOk(info.probe.audio, container)) return "direct";
      return videoOk ? "remux" : "transcode";
    }

    /** 获取并缓存某条视频的播放方式(结果写在 item._plan / item._info 上) */
    export function ensurePlan(item) {
      if (item._plan) return Promise.resolve(item._plan);
      if (item._planPromise) return item._planPromise;
      item._planPromise = api(`/api/videos/playinfo?path=${encodeURIComponent(item.path)}`)
        .then((info) => {
          item._info = info;
          item._plan = decidePlan(item.path, info);
          const st = info && info[item._plan];
          if (st && st.status === "done") item._ready = true;
          return item._plan;
        })
        .catch(() => {
          // 拿不到编码信息时按原文件尝试,失败后由播放器的错误提示兜底
          item._plan = "direct";
          return item._plan;
        })
        .finally(() => { item._planPromise = null; });
      return item._planPromise;
    }

    export function convertedUrl(item, mode) {
      const rel = (item.path || "").split("/").filter(Boolean).map(encodeURIComponent).join("/");
      return `/api/videos/converted/${rel}?mode=${mode}&v=${encodeURIComponent(item.mtime ?? "")}`;
    }

    export function startConversion(item, mode) {
      return api("/api/videos/prepare", { method: "POST", body: JSON.stringify({ path: item.path, mode }) });
    }

    export function conversionStatus(item, mode) {
      return api(`/api/videos/prepare?path=${encodeURIComponent(item.path)}&mode=${mode}`);
    }
