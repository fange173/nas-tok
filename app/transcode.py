"""
transcode.py — 浏览器解不了的视频按需转换为 MP4 缓存

两种模式:
- remux:画面流原样复制(HEVC 统一改为 hvc1 标记),音频非 AAC/MP3 时转 AAC,速度接近拷贝文件;
- transcode:画面转为 H.264 8bit(不超过 1080p),优先 Intel VAAPI 硬件编码,失败自动回落 x264。
输出写入缓存目录,按源文件路径 + mtime + 大小命名,超过容量上限时按最近访问时间淘汰。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.slog import slog

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
def _default_cache_dir() -> Path:
    """默认放在数据库同目录的 transcode/ 下(Docker 中即 /app/data/transcode)。"""
    from app.database import sqlite_db_file

    db_file = sqlite_db_file()
    return (Path(db_file).parent if db_file else Path("data")) / "transcode"


CACHE_DIR = Path(os.getenv("TRANSCODE_CACHE_DIR") or _default_cache_dir())
CACHE_MAX_BYTES = int(float(os.getenv("TRANSCODE_CACHE_MAX_GB", "20")) * 1024**3)
# auto:有 /dev/dri 渲染节点且探测可用时用 VAAPI;vaapi:强制尝试;none:只用软件编码
HWACCEL = os.getenv("TRANSCODE_HWACCEL", "auto").strip().lower()
VAAPI_DEVICE = os.getenv("TRANSCODE_VAAPI_DEVICE", "/dev/dri/renderD128")
MODES = ("remux", "transcode")
# 同时运行的任务数:转封装很轻,完整转码占满 CPU/GPU 只跑一个
_SLOTS = {"remux": threading.Semaphore(2), "transcode": threading.Semaphore(1)}
# 排队+运行中的任务上限,防止批量提交把 NAS 拖垮;已结束的任务记录保留一小时
MAX_PENDING = int(os.getenv("TRANSCODE_MAX_PENDING", "8"))
_JOB_KEEP_SECONDS = 3600
_PROBE_TIMEOUT = 20
_PROBE_CACHE_MAX = 4000


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


# ---------- 探测 ----------

_probe_lock = threading.Lock()
_probe_cache: dict[tuple, dict] = {}


def _bit_depth(pix_fmt: str) -> int:
    for depth in (16, 14, 12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def probe(path: Path) -> Optional[dict]:
    """ffprobe 摘要:容器、首条视频流与音频流的编码信息、时长。结果按 mtime/大小缓存。"""
    if not available():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _probe_lock:
        hit = _probe_cache.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=True,
        ).stdout
        data = json.loads(out or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    summary = {
        "container": (fmt.get("format_name") or "").split(",")[0],
        "duration": float(fmt.get("duration") or 0) or None,
        "video": None if not v else {
            "codec": v.get("codec_name") or "",
            "profile": v.get("profile") or "",
            "tag": v.get("codec_tag_string") or "",
            "pix_fmt": v.get("pix_fmt") or "",
            "bit_depth": _bit_depth(v.get("pix_fmt") or ""),
            "level": v.get("level"),
            "width": v.get("width"),
            "height": v.get("height"),
        },
        "audio": None if not a else {"codec": a.get("codec_name") or "", "channels": a.get("channels")},
    }
    with _probe_lock:
        if len(_probe_cache) >= _PROBE_CACHE_MAX:
            _probe_cache.clear()
        _probe_cache[key] = summary
    return summary


# ---------- 缓存与任务 ----------

@dataclass
class Job:
    key: str
    mode: str
    out: Path
    status: str = "queued"  # queued / running / done / error
    progress: float = 0.0
    error: str = ""
    encoder: str = ""
    started: float = field(default_factory=time.time)


_jobs_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def _cache_key(path: Path, mode: str) -> str:
    st = path.stat()
    raw = f"{path}|{st.st_mtime_ns}|{st.st_size}|{mode}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cached_output(path: Path, mode: str) -> Optional[Path]:
    """已完成的转换文件;命中时刷新访问时间供淘汰排序。"""
    if mode not in MODES:
        return None
    try:
        out = CACHE_DIR / f"{_cache_key(path, mode)}.mp4"
    except OSError:
        return None
    if out.is_file():
        try:
            os.utime(out, None)
        except OSError:
            pass
        return out
    return None


def status(path: Path, mode: str) -> dict:
    out = cached_output(path, mode)
    if out:
        return {"mode": mode, "status": "done", "progress": 1.0}
    key = _cache_key(path, mode)
    with _jobs_lock:
        job = _jobs.get(key)
    if not job:
        return {"mode": mode, "status": "none", "progress": 0.0}
    return {"mode": mode, "status": job.status, "progress": round(job.progress, 3), "error": job.error}


class QueueFull(Exception):
    """排队中的转换任务过多。"""


def start(path: Path, mode: str) -> dict:
    """确保转换任务存在(已完成或进行中则直接返回状态);排队过多时抛 QueueFull。"""
    if mode not in MODES:
        raise ValueError("unknown mode")
    if cached_output(path, mode):
        return status(path, mode)
    key = _cache_key(path, mode)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job.status in ("queued", "running"):
            return status(path, mode)
        now = time.time()
        for k in [k for k, j in _jobs.items() if j.status in ("done", "error") and now - j.started > _JOB_KEEP_SECONDS]:
            _jobs.pop(k, None)
        if sum(1 for j in _jobs.values() if j.status in ("queued", "running")) >= MAX_PENDING:
            raise QueueFull()
        job = Job(key=key, mode=mode, out=CACHE_DIR / f"{key}.mp4")
        _jobs[key] = job
    threading.Thread(target=_run_job, args=(job, path), daemon=True, name=f"transcode-{key[:8]}").start()
    return status(path, mode)


# ---------- ffmpeg 命令 ----------

_vaapi_ok: Optional[bool] = None


def _vaapi_usable() -> bool:
    """探测一次 VAAPI 设备能否初始化;结果缓存到进程结束。"""
    global _vaapi_ok
    if _vaapi_ok is not None:
        return _vaapi_ok
    if HWACCEL == "none" or not FFMPEG or not os.path.exists(VAAPI_DEVICE):
        _vaapi_ok = False
        return False
    try:
        subprocess.run(
            [FFMPEG, "-v", "error", "-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=15, check=True,
        )
        _vaapi_ok = True
    except (subprocess.SubprocessError, OSError):
        _vaapi_ok = False
    return _vaapi_ok


def _audio_args(info: dict) -> list[str]:
    audio = info.get("audio")
    if not audio:
        return []
    if audio.get("codec") in ("aac", "mp3"):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]


def _commands(mode: str, src: Path, dst: Path, info: dict) -> list[tuple[str, list[str]]]:
    """按优先级返回候选命令 [(编码器名, argv)],前一个失败时尝试下一个。"""
    base = [FFMPEG, "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
    tail = ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(dst)]
    audio = _audio_args(info)
    if mode == "remux":
        video = ["-c:v", "copy"]
        if (info.get("video") or {}).get("codec") == "hevc":
            video += ["-tag:v", "hvc1"]
        return [("copy", base + ["-i", str(src)] + video + audio + tail)]
    scale = "scale=-2:'min(ih,1080)'"
    software = (
        "libx264",
        base + ["-i", str(src), "-vf", f"{scale},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-profile:v", "high", "-level", "4.1"] + audio + tail,
    )
    cmds = []
    if HWACCEL in ("auto", "vaapi") and _vaapi_usable():
        hw = base + ["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}", "-hwaccel", "vaapi", "-hwaccel_device", "va",
                     "-filter_hw_device", "va", "-i", str(src),
                     "-vf", f"{scale},format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
        # 部分核显(如 N 系列)只有低功耗编码入口,两种都试
        cmds.append(("h264_vaapi", hw + audio + tail))
        cmds.append(("h264_vaapi_lp", hw + ["-low_power", "1"] + audio + tail))
    cmds.append(software)
    return cmds


def _run_job(job: Job, src: Path) -> None:
    with _SLOTS[job.mode]:
        job.status = "running"
        info = probe(src) or {}
        duration = info.get("duration") or 0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = job.out.with_suffix(".part.mp4")
        last_err = ""
        for name, argv in _commands(job.mode, src, tmp, info):
            job.encoder = name
            job.progress = 0.0
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for line in proc.stdout:
                    if line.startswith("out_time_us=") and duration:
                        try:
                            job.progress = min(0.99, int(line.split("=", 1)[1]) / 1e6 / duration)
                        except ValueError:
                            pass
                err = proc.stderr.read()
                code = proc.wait()
            except OSError as exc:
                code, err = -1, str(exc)
            if code == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, job.out)
                job.progress = 1.0
                job.status = "done"
                slog("transcode_done", mode=job.mode, encoder=name, secs=round(time.time() - job.started, 1))
                _evict(keep=job.out)
                return
            last_err = (err or "").strip().splitlines()[-1:] or [f"exit {code}"]
            last_err = last_err[0][:200]
            try:
                tmp.unlink()
            except OSError:
                pass
        job.status = "error"
        job.error = last_err or "转换失败"
        slog("transcode_failed", mode=job.mode, error=job.error)


def _evict(keep: Optional[Path] = None) -> None:
    """缓存超过上限时删除最久未访问的文件(跳过进行中的临时文件与刚生成的 keep)。"""
    now = time.time()
    for part in CACHE_DIR.glob("*.part.mp4"):
        try:
            if now - part.stat().st_mtime > 86400:
                part.unlink()
        except OSError:
            pass
    try:
        files = [f for f in CACHE_DIR.glob("*.mp4") if not f.name.endswith(".part.mp4") and f != keep]
        stats = [(f, f.stat()) for f in files]
    except OSError:
        return
    total = sum(st.st_size for _, st in stats)
    if total <= CACHE_MAX_BYTES:
        return
    for f, st in sorted(stats, key=lambda x: x[1].st_atime if x[1].st_atime else x[1].st_mtime):
        try:
            f.unlink()
            total -= st.st_size
        except OSError:
            continue
        if total <= CACHE_MAX_BYTES:
            break
