"""
video_handler.py — 视频扫描（带缓存）、稳定随机分页、Range 流式传输、编辑删除
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import random
import shutil
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from typing import Optional, Set

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Library, User, UserLibrary

MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media/videos")).resolve()
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
BROWSER_VIDEO_EXTENSIONS = {".mp4", ".webm", ".m4v", ".mov"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
SCAN_CACHE_TTL = int(os.getenv("SCAN_CACHE_TTL", "300"))
_FP_CACHE_TTL = 8.0
_ORDERED_LRU_MAX = 8


class VideoEditRequest(BaseModel):
    old_path: str = Field(..., description="相对 MEDIA_ROOT 的路径")
    new_name: Optional[str] = Field(None, description="新文件名（不含路径）")
    new_date: Optional[str] = Field(None, description="新修改时间 ISO8601")


class VideoDeleteRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024, description="相对 MEDIA_ROOT 的路径")


class AlbumRenameRequest(BaseModel):
    old_path: str = Field(..., description="合集目录相对路径")
    new_name: str = Field(..., min_length=1, max_length=255, description="新目录名")


class AlbumImagesEditRequest(BaseModel):
    album_path: str = Field(..., description="合集目录相对路径")
    renames: list[dict] = Field(default_factory=list, description="[{old_name, new_name}]")


class ImageInfo(BaseModel):
    title: str
    path: str
    encoded_path: str
    mtime: float
    mtime_iso: str
    size: int


class VideoInfo(BaseModel):
    """Feed 条目：video / image / album"""

    kind: str = "video"
    title: str
    path: str
    encoded_path: str
    mtime: float
    mtime_iso: str
    size: int
    library: str
    library_id: Optional[int] = None
    count: Optional[int] = None
    images: Optional[list[ImageInfo]] = None


@dataclass
class _ScanCache:
    fingerprint: str = ""
    videos: list[VideoInfo] = field(default_factory=list)
    built_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _OrderedCache:
    """LRU：缓存已排序/已 shuffle 的列表，分页只切片，避免每次全量重排"""
    lock: threading.Lock = field(default_factory=threading.Lock)
    slots: OrderedDict = field(default_factory=OrderedDict)


_cache = _ScanCache()
_ordered_cache = _OrderedCache()
_fp_cache: tuple[float, str] = (0.0, "")


def normalize_library_path(raw: str) -> str:
    """规范化相对路径：禁止绝对路径、禁止穿越，返回相对 MEDIA_ROOT 的 posix 路径"""
    text = (raw or "").strip().replace("\\", "/")
    # 用户误填容器绝对路径时，尝试截取 MEDIA_ROOT 后缀
    media_posix = MEDIA_ROOT.as_posix().rstrip("/")
    if text.startswith(media_posix + "/"):
        text = text[len(media_posix) + 1 :]
    elif text == media_posix:
        text = ""
    text = text.lstrip("/")
    if text.startswith("media/videos/"):
        text = text[len("media/videos/") :]
    parts = [p for p in text.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="非法路径：不允许包含 ..")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        raise HTTPException(
            status_code=400,
            detail="请填写相对路径（相对 /media/videos），例如 movies；不要填宿主机绝对路径",
        )
    return "/".join(parts)


def _ensure_under_media(rel_path: str) -> Path:
    decoded = urllib.parse.unquote(rel_path).lstrip("/\\")
    full = (MEDIA_ROOT / decoded).resolve()
    try:
        full.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法路径") from exc
    return full


def invalidate_scan_cache() -> None:
    global _fp_cache
    with _cache.lock:
        _cache.fingerprint = ""
        _cache.videos = []
        _cache.built_at = 0.0
        _fp_cache = (0.0, "")
    with _ordered_cache.lock:
        _ordered_cache.slots.clear()
    invalidate_media_access_cache()


def discover_media_dirs() -> list[dict]:
    """列出 MEDIA_ROOT 下一层可挂载为库的子目录"""
    result = []
    if not MEDIA_ROOT.is_dir():
        return result
    for entry in sorted(os.listdir(MEDIA_ROOT)):
        full = MEDIA_ROOT / entry
        if full.is_dir() and not entry.startswith("."):
            result.append(
                {
                    "name": entry,
                    "path": entry,
                    "absolute_path": str(full),
                }
            )
    return result


def browse_media_dirs(rel_path: str = "") -> dict:
    """
    浏览 MEDIA_ROOT 下的子目录（单层）。
    rel_path 为空表示根；返回当前路径、父路径、子目录列表。
    """
    path = normalize_library_path(rel_path)
    current = (MEDIA_ROOT / path).resolve() if path else MEDIA_ROOT.resolve()
    try:
        current.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法路径") from exc
    if not current.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")

    parent = ""
    if path:
        parent = str(Path(path).parent.as_posix())
        if parent == ".":
            parent = ""

    children = []
    try:
        entries = sorted(os.listdir(current))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法读取目录: {exc}") from exc

    for entry in entries:
        if entry.startswith("."):
            continue
        full = current / entry
        if not full.is_dir():
            continue
        child_rel = f"{path}/{entry}" if path else entry
        # 粗略统计该层及以下视频数（浅层：仅直接文件数，避免过慢；另给 has_subdir）
        file_count = 0
        has_subdir = False
        try:
            for name in os.listdir(full):
                p = full / name
                if p.is_dir() and not name.startswith("."):
                    has_subdir = True
                elif p.is_file() and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    file_count += 1
        except OSError:
            pass
        children.append(
            {
                "name": entry,
                "path": child_rel,
                "absolute_path": str(full),
                "direct_video_count": file_count,
                "has_subdir": has_subdir,
            }
        )

    return {
        "media_root": str(MEDIA_ROOT),
        "path": path,
        "parent": parent,
        "absolute_path": str(current),
        "children": children,
    }


def count_videos_in_dir(root: Path, *, recursive: bool) -> int:
    if not root.is_dir():
        return 0
    n = 0
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                    n += 1
    else:
        for fname in os.listdir(root):
            p = root / fname
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                n += 1
    return n


def _dir_mtime_sample(root: Path, *, max_depth: int = 2) -> str:
    """
    采样目录及浅层子目录 mtime，使 movies/2024/ 下新增文件能更快让指纹失效。
    不做全树 walk，控制开销。
    """
    if not root.is_dir():
        return "0"
    stamps: list[str] = []
    try:
        stamps.append(f".:{root.stat().st_mtime_ns}")
    except OSError:
        return "0"

    def _walk(cur: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            with os.scandir(cur) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            rel = f"{prefix}/{entry.name}" if prefix else entry.name
            stamps.append(f"{rel}:{st.st_mtime_ns}")
            _walk(Path(entry.path), rel, depth + 1)

    _walk(root, "", 1)
    return ",".join(stamps)


def get_user_library_ids(db: Session, user_id: int) -> Set[int]:
    """用户已分配的存储库 id 集合。"""
    return set(
        db.scalars(select(UserLibrary.library_id).where(UserLibrary.user_id == user_id)).all()
    )


# 拉流 Range 请求极多：短缓存用户库分配与 path 鉴权结果
_MEDIA_ACCESS_TTL = float(os.getenv("MEDIA_ACCESS_CACHE_TTL", "45"))
_access_lock = threading.Lock()
_user_libs_cache: dict[int, tuple[float, frozenset[int]]] = {}
_libs_snapshot_cache: Optional[tuple[float, tuple[tuple[int, str, bool], ...]]] = None
_path_access_cache: dict[tuple[int, str], tuple[float, bool]] = {}
_user_by_name_cache: dict[str, tuple[float, Optional[tuple[int, bool, int]]]] = {}


def invalidate_media_access_cache(*, user_id: Optional[int] = None) -> None:
    """库分配/启停变更后清理拉流 ACL 缓存。"""
    global _libs_snapshot_cache
    with _access_lock:
        if user_id is None:
            _user_libs_cache.clear()
            _path_access_cache.clear()
            _user_by_name_cache.clear()
            _libs_snapshot_cache = None
            return
        _user_libs_cache.pop(user_id, None)
        for key in [k for k in _path_access_cache if k[0] == user_id]:
            _path_access_cache.pop(key, None)
        # 用户名缓存按值反查清理
        drop_names = [
            name
            for name, packed in _user_by_name_cache.items()
            if packed[1] and packed[1][0] == user_id
        ]
        for name in drop_names:
            _user_by_name_cache.pop(name, None)


def _cached_user_library_ids(db: Session, user_id: int) -> frozenset[int]:
    now = time.time()
    with _access_lock:
        hit = _user_libs_cache.get(user_id)
        if hit and (now - hit[0]) < _MEDIA_ACCESS_TTL:
            return hit[1]
    ids = frozenset(get_user_library_ids(db, user_id))
    with _access_lock:
        _user_libs_cache[user_id] = (now, ids)
    return ids


def _library_snapshot(db: Session) -> tuple[tuple[int, str, bool], ...]:
    """(id, path, enabled) 按 path 长度降序，供路径归属解析。"""
    global _libs_snapshot_cache
    now = time.time()
    with _access_lock:
        if _libs_snapshot_cache and (now - _libs_snapshot_cache[0]) < _MEDIA_ACCESS_TTL:
            return _libs_snapshot_cache[1]
    libs = list(db.scalars(select(Library)).all())
    libs.sort(key=lambda lib: len(lib.path or ""), reverse=True)
    snap = tuple((int(lib.id), lib.path or "", bool(lib.enabled)) for lib in libs)
    with _access_lock:
        _libs_snapshot_cache = (now, snap)
    return snap


def resolve_library_for_path(db: Session, rel: str) -> Optional[Library]:
    """按相对路径匹配所属存储库（最长 path 优先；默认库仅匹配根文件）。"""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return None
    for lib_id, path, _enabled in _library_snapshot(db):
        if not path:
            if "/" not in rel:
                return db.get(Library, lib_id)
            continue
        if rel == path or rel.startswith(path + "/"):
            return db.get(Library, lib_id)
    return None


def _resolve_library_meta(db: Session, rel: str) -> Optional[tuple[int, bool]]:
    """返回 (library_id, enabled)，避免每次鉴权再 get(Library)。"""
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return None
    for lib_id, path, enabled in _library_snapshot(db):
        if not path:
            if "/" not in rel:
                return lib_id, enabled
            continue
        if rel == path or rel.startswith(path + "/"):
            return lib_id, enabled
    return None


def user_can_access_media_path(db: Session, user_id: int, rel: str) -> bool:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return False
    now = time.time()
    cache_key = (user_id, rel)
    with _access_lock:
        hit = _path_access_cache.get(cache_key)
        if hit and (now - hit[0]) < _MEDIA_ACCESS_TTL:
            return hit[1]

    allowed = _cached_user_library_ids(db, user_id)
    ok = False
    if allowed:
        meta = _resolve_library_meta(db, rel)
        ok = bool(meta and meta[0] in allowed and meta[1])

    with _access_lock:
        _path_access_cache[cache_key] = (now, ok)
        # 简单限长，防止 path 枚举撑爆内存
        if len(_path_access_cache) > 4096:
            oldest = sorted(_path_access_cache.items(), key=lambda kv: kv[1][0])[:1024]
            for k, _ in oldest:
                _path_access_cache.pop(k, None)
    return ok


def lookup_stream_user(db: Session, username: str) -> Optional[tuple[int, bool, int]]:
    """拉流用：缓存 username → (user_id, is_active, credentials_version)。"""
    name = (username or "").strip()
    if not name:
        return None
    now = time.time()
    with _access_lock:
        hit = _user_by_name_cache.get(name)
        if hit and (now - hit[0]) < _MEDIA_ACCESS_TTL:
            return hit[1]

    user = db.scalar(select(User).where(User.username == name))
    packed: Optional[tuple[int, bool, int]] = None
    if user:
        packed = (int(user.id), bool(user.is_active), int(user.credentials_version or 0))
    with _access_lock:
        _user_by_name_cache[name] = (now, packed)
    return packed


def grant_library_to_users(db: Session, library_id: int, user_ids: list[int]) -> None:
    """给指定用户分配某个存储库（已有则跳过）。"""
    for uid in user_ids:
        exists = db.scalar(
            select(UserLibrary.id).where(
                UserLibrary.user_id == uid,
                UserLibrary.library_id == library_id,
            ).limit(1)
        )
        if not exists:
            db.add(UserLibrary(user_id=uid, library_id=library_id))


def _library_fingerprint(db: Session) -> str:
    global _fp_cache
    now = time.time()
    cached_at, cached_fp = _fp_cache
    if cached_fp and (now - cached_at) < _FP_CACHE_TTL:
        return cached_fp
    libs = db.scalars(select(Library).order_by(Library.id.asc())).all()
    parts: list[str] = []
    for lib in libs:
        parts.append(f"{lib.id}:{lib.path}:{int(lib.enabled)}")
        root = (MEDIA_ROOT / lib.path) if lib.path else MEDIA_ROOT
        # 默认库只扫根文件：根目录 mtime 足够；子目录库采样两层
        depth = 0 if not lib.path else 2
        parts.append(_dir_mtime_sample(root, max_depth=depth))
    raw = "|".join(parts) + f"|root={MEDIA_ROOT}"
    fp = hashlib.sha1(raw.encode()).hexdigest()
    _fp_cache = (now, fp)
    return fp


def _rel_under_media(full: Path) -> Optional[str]:
    """安全生成相对 MEDIA_ROOT 的路径；失败返回 None"""
    try:
        return full.relative_to(MEDIA_ROOT).as_posix()
    except ValueError:
        try:
            return full.resolve().relative_to(MEDIA_ROOT).as_posix()
        except ValueError:
            return None


def ensure_library_dir(path: str) -> Path:
    """
    校验库目录：必须存在、必须是目录，且 resolve 后仍在 MEDIA_ROOT 内
    （拒绝指向卷外的符号链接）。
    """
    root = (MEDIA_ROOT / path) if path else MEDIA_ROOT
    if not root.exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"目录不存在: {root}。"
                f"请先在 docker-compose 把宿主机目录挂载到 /media/videos/{path or '...'}，"
                f"当前 MEDIA_ROOT={MEDIA_ROOT}"
            ),
        )
    try:
        resolved = root.resolve()
        resolved.relative_to(MEDIA_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="目录解析后超出媒体根目录（可能是指向外部的符号链接），不允许添加",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法访问目录: {exc}") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="路径不是目录")
    return resolved


def find_nested_library_conflict(
    db: Session, path: str, *, exclude_id: Optional[int] = None
) -> Optional[Library]:
    """若 path 与已有库存在父子嵌套，返回冲突的库。空路径（默认库）不与子目录冲突。"""
    if not path:
        return None
    libs = db.scalars(select(Library)).all()
    for other in libs:
        if exclude_id is not None and other.id == exclude_id:
            continue
        op = other.path or ""
        if not op:
            continue
        if path == op:
            continue
        if path.startswith(op + "/") or op.startswith(path + "/"):
            return other
    return None


def _file_kind(name: str) -> Optional[str]:
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def _stat_fields(st: os.stat_result) -> tuple[float, str, int]:
    mtime = st.st_mtime
    return (
        mtime,
        datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
        int(st.st_size),
    )


def _append_media(
    items: list[VideoInfo],
    seen: set[str],
    abs_path: Path,
    lib_name: str,
    lib_id: int,
    *,
    kind: str,
    rel: Optional[str] = None,
    st: Optional[os.stat_result] = None,
) -> None:
    if rel is None:
        rel = _rel_under_media(abs_path)
        if not rel:
            return
    if rel in seen:
        return
    seen.add(rel)
    try:
        stat = st if st is not None else abs_path.stat()
    except OSError:
        return
    mtime, mtime_iso, size = _stat_fields(stat)
    items.append(
        VideoInfo(
            kind=kind,
            title=abs_path.stem,
            path=rel,
            encoded_path=urllib.parse.quote(rel, safe=""),
            mtime=mtime,
            mtime_iso=mtime_iso,
            size=size,
            library=lib_name,
            library_id=lib_id,
        )
    )


def _make_image_info(abs_path: Path, rel: str, st: os.stat_result) -> ImageInfo:
    mtime, mtime_iso, size = _stat_fields(st)
    return ImageInfo(
        title=abs_path.stem,
        path=rel,
        encoded_path=urllib.parse.quote(rel, safe=""),
        mtime=mtime,
        mtime_iso=mtime_iso,
        size=size,
    )


def _scan_root_files(
    items: list[VideoInfo],
    seen: set[str],
    root: Path,
    lib_name: str,
    lib_id: int,
) -> None:
    """扫描目录下的直接文件（视频/单图），不进入子目录。"""
    try:
        with os.scandir(root) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                kind = _file_kind(entry.name)
                if not kind:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel = _rel_under_media(Path(entry.path))
                if not rel:
                    try:
                        rel = Path(entry.path).resolve().relative_to(MEDIA_ROOT).as_posix()
                    except ValueError:
                        continue
                _append_media(
                    items, seen, Path(entry.path), lib_name, lib_id,
                    kind=kind, rel=rel, st=st,
                )
    except OSError:
        return


def _scan_album_dir(
    items: list[VideoInfo],
    seen: set[str],
    album_dir: Path,
    lib_name: str,
    lib_id: int,
) -> None:
    """一级子目录：图片（含更深）合并为合集；视频各自独立（1A+2A）。"""
    album_rel = _rel_under_media(album_dir)
    if not album_rel:
        return

    image_count = 0
    max_mtime = 0.0
    total_size = 0
    try:
        for dirpath, _dns, filenames in os.walk(album_dir, followlinks=False):
            for fname in filenames:
                kind = _file_kind(fname)
                if not kind:
                    continue
                full = Path(dirpath) / fname
                rel = _rel_under_media(full)
                if not rel:
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                if kind == "video":
                    _append_media(
                        items, seen, full, lib_name, lib_id,
                        kind="video", rel=rel, st=st,
                    )
                else:
                    image_count += 1
                    total_size += int(st.st_size)
                    if st.st_mtime > max_mtime:
                        max_mtime = st.st_mtime
    except OSError:
        return

    if image_count <= 0:
        return
    if album_rel in seen:
        return
    seen.add(album_rel)
    items.append(
        VideoInfo(
            kind="album",
            title=album_dir.name,
            path=album_rel,
            encoded_path=urllib.parse.quote(album_rel, safe=""),
            mtime=max_mtime,
            mtime_iso=datetime.fromtimestamp(max_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            size=total_size,
            library=lib_name,
            library_id=lib_id,
            count=image_count,
            images=None,
        )
    )


def _scan_filesystem(db: Session) -> list[VideoInfo]:
    """
    扫描规则：
    - path="" 默认库：只扫根目录直接文件（视频+单图）
    - path=子目录库：根下直接文件为单视频/单图；一级子目录有图则成合集（更深图并入）；
      同目录视频仍单独进流（1A + 2A）
    - 已配置存储库但全部停用：不扫任何内容（禁止回退扫整个 MEDIA_ROOT）
    """
    all_libs = list(db.scalars(select(Library)).all())
    libs = [lib for lib in all_libs if lib.enabled]
    libs.sort(key=lambda lib: (0 if not lib.path else lib.path.count("/") + 1, lib.path or ""), reverse=True)
    items: list[VideoInfo] = []
    seen: set[str] = set()

    if not libs:
        # 库已登记但全部停用 → 空列表；仅在完全未配置任何库时才兼容回退
        if all_libs:
            return []
        if MEDIA_ROOT.is_dir():
            _scan_root_files(items, seen, MEDIA_ROOT, "默认库", 0)
            try:
                for entry in sorted(os.listdir(MEDIA_ROOT)):
                    sub = MEDIA_ROOT / entry
                    if sub.is_dir() and not entry.startswith("."):
                        _scan_album_dir(items, seen, sub, entry, 0)
            except OSError:
                pass
        return items

    for lib in libs:
        try:
            root = ensure_library_dir(lib.path)
        except HTTPException:
            continue
        _scan_root_files(items, seen, root, lib.name, lib.id)
        if not lib.path:
            continue
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("."):
                        continue
                    _scan_album_dir(items, seen, Path(entry.path), lib.name, lib.id)
        except OSError:
            continue

    return items


def load_album_images(album_rel: str) -> list[ImageInfo]:
    """按需读取合集图片，不进入全局扫描缓存。"""
    rel = (album_rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        return []
    try:
        album = _ensure_under_media(rel)
    except HTTPException:
        return []
    if not album.is_dir():
        return []
    images: list[ImageInfo] = []
    try:
        for dirpath, _dns, filenames in os.walk(album, followlinks=False):
            for fname in filenames:
                if _file_kind(fname) != "image":
                    continue
                full = Path(dirpath) / fname
                item_rel = _rel_under_media(full)
                if not item_rel:
                    continue
                try:
                    st = full.stat()
                except OSError:
                    continue
                images.append(_make_image_info(full, item_rel, st))
    except OSError:
        return []
    images.sort(key=lambda im: (im.mtime, im.path))
    return images


def get_all_videos(db: Session, *, force: bool = False) -> list[VideoInfo]:
    fp = _library_fingerprint(db)
    now = time.time()
    with _cache.lock:
        fresh = (
            not force
            and _cache.videos is not None
            and _cache.fingerprint == fp
            and (now - _cache.built_at) < SCAN_CACHE_TTL
        )
        if fresh and _cache.built_at > 0:
            return _cache.videos  # 只读引用；调用方勿原地修改

    videos = _scan_filesystem(db)
    with _cache.lock:
        _cache.fingerprint = fp
        _cache.videos = videos
        _cache.built_at = time.time()
    with _ordered_cache.lock:
        _ordered_cache.slots.clear()
    return videos


def library_video_counts(db: Session, *, force: bool = False) -> dict[int, int]:
    """按 library_id 聚合视频数，复用扫描缓存，避免后台反复 os.walk"""
    counts: dict[int, int] = {}
    for v in get_all_videos(db, force=force):
        lid = int(v.library_id or 0)
        counts[lid] = counts.get(lid, 0) + 1
    return counts


def library_byte_sizes(db: Session, *, force: bool = False) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for v in get_all_videos(db, force=force):
        lid = int(v.library_id or 0)
        sizes[lid] = sizes.get(lid, 0) + int(v.size or 0)
    return sizes


def _get_ordered_videos(
    db: Session,
    *,
    sort: str,
    seed: Optional[str],
    force_refresh: bool,
    media_mode: str = "video",
) -> tuple[list[VideoInfo], Optional[str]]:
    videos = get_all_videos(db, force=force_refresh)
    fp = _cache.fingerprint or _library_fingerprint(db)
    mode = media_mode if media_mode in ("video", "mixed", "images") else "video"

    if mode == "video":
        videos = [v for v in videos if v.kind == "video"]
    elif mode == "images":
        videos = [v for v in videos if v.kind in ("image", "album")]
    # mixed: all

    if sort == "newest":
        used_seed = None
        key = f"{fp}|{mode}|newest"
    else:
        used_seed = seed or hashlib.sha1(f"{time.time()}-{os.getpid()}".encode()).hexdigest()[:16]
        key = f"{fp}|{mode}|random|{used_seed}"

    with _ordered_cache.lock:
        hit = _ordered_cache.slots.get(key)
        if hit:
            _ordered_cache.slots.move_to_end(key)
            return hit, used_seed

    ordered = list(videos)
    if sort == "newest":
        ordered.sort(key=lambda v: v.mtime, reverse=True)
    else:
        random.Random(used_seed).shuffle(ordered)

    with _ordered_cache.lock:
        _ordered_cache.slots[key] = ordered
        _ordered_cache.slots.move_to_end(key)
        while len(_ordered_cache.slots) > _ORDERED_LRU_MAX:
            _ordered_cache.slots.popitem(last=False)
        return ordered, used_seed


def scan_videos(
    db: Session,
    *,
    sort: str = "random",
    page: int = 1,
    limit: int = 20,
    seed: Optional[str] = None,
    force_refresh: bool = False,
    favorites_only: bool = False,
    favorite_paths: Optional[Set[str]] = None,
    tagged_paths: Optional[Set[str]] = None,
    media_mode: str = "video",
    allowed_library_ids: Optional[Set[int]] = None,
    q: Optional[str] = None,
    library_id: Optional[int] = None,
    recent_paths: Optional[list] = None,
) -> dict:
    ordered, used_seed = _get_ordered_videos(
        db,
        sort=sort,
        seed=seed,
        force_refresh=force_refresh,
        media_mode=media_mode,
    )
    if allowed_library_ids is not None:
        ordered = [v for v in ordered if int(v.library_id or 0) in allowed_library_ids]
    if library_id is not None:
        ordered = [v for v in ordered if int(v.library_id or 0) == int(library_id)]

    needle = (q or "").strip().casefold()
    if needle:
        ordered = [
            v for v in ordered
            if needle in (v.title or "").casefold() or needle in (v.path or "").casefold()
        ]

    fav_set = favorite_paths or set()

    # tagged_paths is not None 表示启用标记过滤（空集合 = 无匹配，不得回退到全库）
    if tagged_paths is not None and favorites_only:
        source = [v for v in ordered if v.path in tagged_paths and v.path in fav_set]
    elif tagged_paths is not None:
        source = [v for v in ordered if v.path in tagged_paths]
    elif favorites_only:
        source = [v for v in ordered if v.path in fav_set]
    else:
        source = ordered

    if recent_paths is not None:
        rank = {p: i for i, p in enumerate(recent_paths)}
        source = [v for v in source if v.path in rank]
        source.sort(key=lambda v: rank.get(v.path, 10**9))

    total = len(source)
    page = max(1, page)
    limit = max(1, min(limit, 100))
    start = (page - 1) * limit
    end = start + limit
    items = []
    for v in source[start:end]:
        d = v.model_dump()
        if v.kind == "album":
            # 合集图片可能数百张，全量内联会拖垮列表响应；
            # 前端改用 GET /api/albums/images 按需拉取（count 字段保留）
            d.pop("images", None)
        d["favorited"] = (v.path in fav_set) if fav_set else False
        ext = Path(v.path or "").suffix.lower()
        if v.kind == "video":
            d["playable"] = ext in BROWSER_VIDEO_EXTENSIONS
        else:
            d["playable"] = True
        items.append(d)

    result: dict = {
        "total": total,
        "page": page,
        "limit": limit,
        "sort": sort,
        "seed": used_seed,
        "cached_ttl": SCAN_CACHE_TTL,
        "favorites_only": favorites_only,
        "media_mode": media_mode if media_mode in ("video", "mixed", "images") else "video",
        "items": items,
    }
    if tagged_paths is not None:
        result["tagged_only"] = True
    return result


_STREAM_CHUNK = 512 * 1024  # Range 流式响应的分块大小


def _parse_range_header(header: str, size: int) -> Optional[tuple[int, int]]:
    """
    解析单段 Range(bytes=start-end / start- / -suffix),返回 (start, end) 闭区间。
    语法不支持时返回 None(按 RFC 忽略,回 200);start 越界时抛 416。
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].strip()
    if "," in spec:  # 多段不支持,整体忽略
        return None
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    if not start_s and not end_s:
        return None
    try:
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            if start < 0 or end < start:
                return None
        else:
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(size - suffix, 0)
            end = size - 1
    except ValueError:
        return None
    if start >= size:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        )
    return start, min(end, size - 1)


def _file_slice_iter(path: Path, start: int, length: int):
    """同步分块迭代器:StreamingResponse 会放线程池消费。"""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            data = f.read(min(_STREAM_CHUNK, remaining))
            if not data:
                break
            yield data
            remaining -= len(data)


def stream_video_file(
    file_path_encoded: str,
    download: bool = False,
    range_header: Optional[str] = None,
    if_range: Optional[str] = None,
):
    rel = urllib.parse.unquote(file_path_encoded)
    path = _ensure_under_media(rel)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    if download:
        # 下载走 FileResponse:starlette 自行处理合法 Range(断点续传),行为正确
        return FileResponse(
            path,
            media_type=content_type,
            content_disposition_type="attachment",
            filename=path.name,
            headers=headers,
        )
    st = path.stat()
    size = st.st_size
    mtime_http = formatdate(st.st_mtime, usegmt=True)
    headers["Last-Modified"] = mtime_http
    rng = None
    if size > 0 and range_header:
        # If-Range 与当前 mtime 不一致 → 文件已变,按规范回 200 全量
        if if_range and if_range.strip() != mtime_http:
            rng = None
        else:
            rng = _parse_range_header(range_header, size)
    if rng is None:
        if range_header:
            # 客户端带了 Range 但被本模块判定忽略:必须走开,不能回落 FileResponse
            # —— starlette 会从 scope 二次解析同一 Range 头,产生 400/416/多段 bug。
            # 200 全量由统一的 _file_slice_iter 提供,行为只由这一个解析器决定。
            return StreamingResponse(
                _file_slice_iter(path, 0, size),
                status_code=status.HTTP_200_OK,
                media_type=content_type,
                headers={**headers, "Content-Length": str(size)},
            )
        return FileResponse(path, media_type=content_type, filename=None, headers=headers)
    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        _file_slice_iter(path, start, length),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


def edit_video(req: VideoEditRequest) -> dict:
    path = _ensure_under_media(req.old_path)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    new_path = path
    if req.new_name:
        new_name = req.new_name.strip()
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="非法文件名")
        if not Path(new_name).suffix:
            new_name = new_name + path.suffix
        candidate = path.with_name(new_name)
        if candidate.resolve().parent != path.resolve().parent:
            raise HTTPException(status_code=400, detail="非法文件名")
        if candidate.exists() and candidate != path:
            raise HTTPException(status_code=409, detail="目标文件名已存在")
        path.rename(candidate)
        new_path = candidate

    if req.new_date:
        try:
            dt = datetime.fromisoformat(req.new_date.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="日期格式无效") from exc
        atime = new_path.stat().st_atime
        os.utime(new_path, (atime, ts))

    invalidate_scan_cache()
    rel = new_path.resolve().relative_to(MEDIA_ROOT).as_posix()
    stat = new_path.stat()
    kind = _file_kind(new_path.name) or "video"
    return {
        "kind": kind,
        "path": rel,
        "encoded_path": urllib.parse.quote(rel, safe=""),
        "title": new_path.stem,
        "mtime": stat.st_mtime,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _normalize_rel(rel_path: str) -> str:
    text = urllib.parse.unquote(rel_path or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in text.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="非法路径")
    return "/".join(parts)


def _assert_safe_nested_media_dir(rel_path: str, path: Path) -> str:
    """
    仅允许操作「媒体根下至少两级」的目录（典型：库/合集），
    禁止 MEDIA_ROOT 本身及一级库根目录。
    """
    rel = _normalize_rel(rel_path)
    if not rel:
        raise HTTPException(status_code=400, detail="禁止操作媒体根目录")
    resolved = path.resolve()
    root = MEDIA_ROOT.resolve()
    if resolved == root:
        raise HTTPException(status_code=400, detail="禁止操作媒体根目录")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法路径") from exc
    if resolved.parent == root:
        raise HTTPException(status_code=400, detail="禁止操作存储库根目录")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="合集目录不存在")
    return rel


def _dir_has_image(path: Path) -> bool:
    try:
        for dirpath, _dns, filenames in os.walk(path, followlinks=False):
            for fname in filenames:
                if Path(fname).suffix.lower() in IMAGE_EXTENSIONS:
                    return True
    except OSError:
        return False
    return False


def delete_video(rel_path: str) -> dict:
    rel = _normalize_rel(rel_path)
    if not rel:
        raise HTTPException(status_code=400, detail="路径不能为空")
    path = _ensure_under_media(rel)
    if path.is_dir():
        safe_rel = _assert_safe_nested_media_dir(rel, path)
        if not _dir_has_image(path):
            raise HTTPException(status_code=400, detail="只能删除图片合集目录")
        shutil.rmtree(path)
        invalidate_scan_cache()
        return {"ok": True, "deleted": safe_rel, "kind": "album"}
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只能删除视频或图片文件")
    path.unlink()
    invalidate_scan_cache()
    return {"ok": True, "deleted": rel, "kind": "file"}


def rename_album(req: AlbumRenameRequest) -> dict:
    old_rel = _normalize_rel(req.old_path)
    if not old_rel:
        raise HTTPException(status_code=400, detail="合集路径不能为空")
    old = _ensure_under_media(old_rel)
    _assert_safe_nested_media_dir(old_rel, old)
    if not _dir_has_image(old):
        raise HTTPException(status_code=400, detail="只能重命名图片合集目录")
    new_name = req.new_name.strip()
    if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法目录名")
    candidate = old.with_name(new_name)
    if candidate.resolve().parent != old.resolve().parent:
        raise HTTPException(status_code=400, detail="非法目录名")
    if candidate.exists():
        raise HTTPException(status_code=409, detail="目标目录已存在")
    old.rename(candidate)
    invalidate_scan_cache()
    rel = candidate.resolve().relative_to(MEDIA_ROOT).as_posix()
    return {
        "ok": True,
        "old_path": old_rel,
        "path": rel,
        "title": candidate.name,
        "encoded_path": urllib.parse.quote(rel, safe=""),
    }


def edit_album_images(req: AlbumImagesEditRequest) -> dict:
    album_rel = _normalize_rel(req.album_path)
    if not album_rel:
        raise HTTPException(status_code=400, detail="合集路径不能为空")
    album = _ensure_under_media(album_rel)
    _assert_safe_nested_media_dir(album_rel, album)
    changed = []
    for item in req.renames or []:
        # old_name: 相对合集的路径，如 nested/a.jpg 或 a.jpg
        # new_name: 仅新文件名（不含目录），保留原相对目录
        old_rel = (item.get("old_name") or "").strip().replace("\\", "/")
        new_name = (item.get("new_name") or "").strip().replace("\\", "/")
        if not old_rel or not new_name:
            continue
        old_parts = [p for p in old_rel.split("/") if p and p != "."]
        if any(p == ".." for p in old_parts) or not old_parts:
            raise HTTPException(status_code=400, detail="非法原文件路径")
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="新文件名不能包含路径")
        src = (album.joinpath(*old_parts)).resolve()
        try:
            src.relative_to(album.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法原文件路径") from exc
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"找不到图片: {old_rel}")
        if not Path(new_name).suffix:
            new_name = new_name + src.suffix
        dst = src.with_name(new_name)
        if dst.resolve().parent != src.resolve().parent:
            raise HTTPException(status_code=400, detail="非法文件名")
        if dst.exists() and dst != src:
            raise HTTPException(status_code=409, detail=f"目标已存在: {new_name}")
        old_full = src.resolve().relative_to(MEDIA_ROOT).as_posix()
        src.rename(dst)
        new_full = dst.resolve().relative_to(MEDIA_ROOT).as_posix()
        changed.append({"old_path": old_full, "path": new_full, "title": dst.stem})
    invalidate_scan_cache()
    return {"ok": True, "album_path": album_rel, "changed": changed}


def find_sidecar_subtitle(rel_path: str) -> Optional[Path]:
    """同目录同主文件名的 .vtt / .srt。"""
    try:
        media = _ensure_under_media(rel_path)
    except HTTPException:
        return None
    if not media.is_file():
        return None
    stem = media.with_suffix("")
    for ext in (".vtt", ".srt"):
        candidate = Path(str(stem) + ext)
        if candidate.is_file():
            return candidate
    return None


def srt_to_vtt(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = ["WEBVTT", ""]
    for line in lines:
        if "-->" in line:
            out.append(line.replace(",", "."))
        else:
            out.append(line)
    return "\n".join(out).strip() + "\n"
