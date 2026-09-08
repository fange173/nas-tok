"""
main.py — NasTok FastAPI 入口
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import glob
import os
import re
import tempfile
import threading
import time
import urllib.parse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, text as sa_text
from sqlalchemy.orm import Session

from app.admin import router as admin_router
from app.share import router as share_router
from app.auth import (
    COOKIE_NAME,
    ChangePasswordRequest,
    LoginRequest,
    authenticate_user,
    check_login_allowed,
    clear_auth_cookie,
    clear_login_failures,
    create_access_token,
    get_client_ip,
    get_current_user,
    is_staff,
    record_login_failure,
    require_password_ok,
    require_staff,
    require_stream_access,
    set_auth_cookie,
    token_is_remembered,
    user_to_dict,
    warn_if_weak_secret,
)
from app.database import (
    AuditLog,
    Favorite,
    Library,
    ShareLink,
    ShareView,
    Tag,
    TagAssignRequest,
    TagCreateRequest,
    TagUpdateRequest,
    User,
    VideoTagAssignment,
    bump_credentials,
    cleanup_old_logs,
    get_db,
    init_db,
    utcnow,
    write_audit_log,
    write_view_log,
)
from app.slog import slog
from app.video_handler import (
    MEDIA_ROOT,
    AlbumImagesEditRequest,
    AlbumRenameRequest,
    VideoDeleteRequest,
    VideoEditRequest,
    delete_video,
    edit_album_images,
    edit_video,
    find_sidecar_subtitle,
    get_user_library_ids,
    invalidate_media_access_cache,
    invalidate_scan_cache,
    load_album_images,
    lookup_stream_user,
    rename_album,
    scan_videos,
    srt_to_vtt,
    stream_video_file,
    user_can_access_media_path,
)

STATIC_DIR = Path(__file__).parent / "static"

_spa_cache: dict[str, str] = {}


def _spa_html() -> str:
    """读取 SPA HTML,把 style.css / js/main.js 的 ?v= 替换为文件 mtime —— 改静态资源自动破缓存。"""
    html_path = STATIC_DIR / "index.html"
    css_path = STATIC_DIR / "style.css"
    js_dir = STATIC_DIR / "js"
    try:
        html_mtime = html_path.stat().st_mtime_ns
        css_mtime = css_path.stat().st_mtime_ns
        # 子模块按 './x.js' 相对导入无版本号;/static/js 挂载点已强制 no-cache+304 协商,
        # 保证部署后子模块与入口同批更新;入口 main.js 注入 js 目录最大 mtime,任何模块改动都会使入口 URL 变化
        js_mtime = max(
            (f.stat().st_mtime_ns for f in js_dir.glob("*.js")),
            default=0,
        )
    except OSError:
        return html_path.read_text(encoding="utf-8")
    key = f"{html_mtime}:{css_mtime}:{js_mtime}"
    if _spa_cache.get("key") == key and "html" in _spa_cache:
        return _spa_cache["html"]
    text = html_path.read_text(encoding="utf-8")
    text = re.sub(r'style\.css\?v=[^"\']*', f"style.css?v={css_mtime}", text, count=1)
    text = re.sub(r'/static/js/main\.js\?v=[^"\']*', f"/static/js/main.js?v={js_mtime}", text, count=1)
    _spa_cache["key"] = key
    _spa_cache["html"] = text
    return text


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 escape='\\' 使用。"""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _path_exact_or_under(column, prefix: str):
    """匹配 exact 或 prefix/…，避免 path 中的 _/% 被当成通配符。"""
    p = (prefix or "").rstrip("/")
    esc = _escape_like(p)
    return or_(column == p, column.like(esc + "/%", escape="\\"))


def _assert_media_access(db: Session, user: User, path: str) -> None:
    if not user_can_access_media_path(db, user.id, path):
        raise HTTPException(status_code=403, detail="无权访问该媒体(未分配对应存储库)")


def _repoint_favorites(db: Session, old: str, new: str) -> None:
    for fav in list(db.scalars(select(Favorite).where(Favorite.video_path == old)).all()):
        clash = db.scalar(
            select(Favorite).where(Favorite.user_id == fav.user_id, Favorite.video_path == new)
        )
        if clash:
            db.delete(fav)
        else:
            fav.video_path = new


def _repoint_shares(db: Session, old: str, new: str, title: Optional[str] = None) -> None:
    for share in list(db.scalars(select(ShareLink).where(ShareLink.video_path == old)).all()):
        clash = db.scalar(select(ShareLink).where(ShareLink.video_path == new))
        if clash and clash.id != share.id:
            # 访问明细先批量删除（未定义 relationship，同 flush 不保证先子后父）
            db.execute(delete(ShareView).where(ShareView.share_id == share.id))
            db.delete(share)
        else:
            share.video_path = new
            if title:
                share.title = title


def _repoint_tags(db: Session, old: str, new: str) -> None:
    for va in list(db.scalars(select(VideoTagAssignment).where(VideoTagAssignment.video_path == old)).all()):
        clash = db.scalar(
            select(VideoTagAssignment).where(
                VideoTagAssignment.user_id == va.user_id,
                VideoTagAssignment.tag_id == va.tag_id,
                VideoTagAssignment.video_path == new,
            )
        )
        if clash:
            db.delete(va)
        else:
            va.video_path = new


LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))


class VideoViewRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024, description="相对 MEDIA_ROOT 的路径")


def _sweep_backup_tmp() -> int:
    """清理临时目录下 >1 小时的 nastok-backup-*.db 残留(客户端断连时 BackgroundTask 不执行,靠启动清扫兜底)。"""
    removed = 0
    now = time.time()
    for f in glob.glob(os.path.join(tempfile.gettempdir(), "nastok-backup-*.db")):
        try:
            if now - os.path.getmtime(f) > 3600:
                os.unlink(f)
                removed += 1
        except OSError:
            pass
    return removed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    warn_if_weak_secret()
    init_db()
    deleted = cleanup_old_logs(LOG_RETENTION_DAYS)
    if deleted:
        print(f"[NasTok] 已清理 {deleted} 条超过 {LOG_RETENTION_DAYS} 天的审计日志")
    swept = _sweep_backup_tmp()
    if swept:
        print(f"[NasTok] 已清理 {swept} 个过期备份临时文件")
    yield


# 接口文档默认关闭（生产暴露面最小化），ENABLE_DOCS=true 时开启
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").lower() in ("1", "true", "yes")

app = FastAPI(
    title="NasTok",
    description="NAS 轻量级短视频浏览器",
    version="1.2.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


# ---------- 安全响应头 ----------


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """所有响应注入安全基线头;不覆盖路由已设置的同名头。
    CSP:全部 JS 已外置为 ES modules,可收 script-src 'self';
    style 保留 'unsafe-inline'(JS 大量用 style 属性与内联样式)。"""
    resp = await call_next(request)
    base = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; worker-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        ),
    }
    for k, v in base.items():
        resp.headers.setdefault(k, v)
    return resp


# ---------- 健康检查 ----------


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db_ok = False
    try:
        db.execute(sa_text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    media_ok = MEDIA_ROOT.is_dir()
    ok = db_ok and media_ok
    return JSONResponse(
        {"ok": ok, "service": "nastok", "db": db_ok, "media": media_ok},
        status_code=200 if ok else 503,
    )


# ---------- 鉴权 ----------


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = get_client_ip(request)
    check_login_allowed(ip)
    try:
        user = authenticate_user(db, body.username, body.password)
    except HTTPException as exc:
        if exc.status_code == 401:
            record_login_failure(ip)
        raise
    clear_login_failures(ip)
    token, max_age = create_access_token(user, remember=body.remember)
    set_auth_cookie(response, token, max_age)
    write_audit_log(db, user=user, action="login", detail="用户登录")
    slog("login", user=user.username, ip=ip, remember=bool(body.remember))
    return {"ok": True, "user": user_to_dict(user)}


@app.post("/api/auth/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_to_dict(user)


@app.post("/api/auth/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not user.verify_password(body.old_password):
        return JSONResponse(status_code=400, content={"detail": "原密码错误"})
    if body.old_password == body.new_password:
        return JSONResponse(status_code=400, content={"detail": "新密码不能与原密码相同"})

    user.password_hash = User.hash_password(body.new_password)
    user.must_change_password = False
    bump_credentials(user)
    db.commit()
    db.refresh(user)
    invalidate_media_access_cache(user_id=user.id)
    # 改密后刷新 Cookie，同步 mcp=false，避免流接口仍被挡；
    # 会话时长跟随原 token（普通 24h 会话不被意外延长成 remember 长期会话）
    current_token = request.cookies.get(COOKIE_NAME)
    remember = bool(current_token and token_is_remembered(current_token))
    token, max_age = create_access_token(user, remember=remember)
    set_auth_cookie(response, token, max_age)
    write_audit_log(db, user=user, action="admin", detail="修改密码")
    return {"ok": True, "user": user_to_dict(user)}


# ---------- 视频 ----------


_VIEW_PREFIX = "观看视频: "


def _recent_view_paths(db: Session, user_id: int, limit: int = 400) -> list[str]:
    rows = db.execute(
        select(AuditLog.detail, func.max(AuditLog.created_at))
        .where(AuditLog.user_id == user_id, AuditLog.action == "view")
        .group_by(AuditLog.detail)
        .order_by(func.max(AuditLog.created_at).desc())
        .limit(limit)
    ).all()
    paths: list[str] = []
    for detail, _ts in rows:
        if detail and str(detail).startswith(_VIEW_PREFIX):
            paths.append(str(detail)[len(_VIEW_PREFIX) :])
    return paths


@app.get("/api/videos/list")
def list_videos(
    sort: str = Query("random", pattern="^(random|newest)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    seed: Optional[str] = Query(None, min_length=4, max_length=64),
    refresh: bool = Query(False),
    favorites: bool = Query(False, description="仅喜欢"),
    media_mode: str = Query("video", pattern="^(video|mixed|images)$"),
    tag_id: Optional[int] = Query(None, ge=1, description="仅该标记的媒体"),
    tagged: bool = Query(False, description="仅已打标媒体（任意标记）"),
    q: Optional[str] = Query(None, max_length=128, description="按标题/路径搜索"),
    library_id: Optional[int] = Query(None, ge=1, description="仅该存储库"),
    recent: bool = Query(False, description="最近观看"),
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    allowed = get_user_library_ids(db, user.id)
    if library_id is not None and library_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该存储库")
    if refresh and not is_staff(user):
        refresh = False
    tag_paths: Optional[set] = None
    tag_name: Optional[str] = None
    if tag_id is not None:
        tag = db.scalar(select(Tag).where(Tag.id == tag_id, Tag.user_id == user.id))
        if not tag:
            raise HTTPException(status_code=404, detail="标记不存在")
        tag_name = tag.name
        tag_paths = {
            va.video_path
            for va in db.scalars(
                select(VideoTagAssignment).where(
                    VideoTagAssignment.user_id == user.id,
                    VideoTagAssignment.tag_id == tag.id,
                )
            ).all()
        }
    elif tagged:
        tag_paths = {
            va.video_path
            for va in db.scalars(
                select(VideoTagAssignment).where(
                    VideoTagAssignment.user_id == user.id,
                )
            ).all()
        }
    recent_paths = _recent_view_paths(db, user.id) if recent else None
    list_sort = "newest" if recent else sort
    fav_paths = None
    if favorites:
        fav_paths = {
            f.video_path
            for f in db.scalars(select(Favorite).where(Favorite.user_id == user.id)).all()
        }
    result = scan_videos(
        db,
        sort=list_sort,
        page=page,
        limit=limit,
        seed=None if recent else seed,
        force_refresh=refresh,
        favorites_only=bool(favorites),
        favorite_paths=fav_paths,
        tagged_paths=tag_paths,
        media_mode=media_mode,
        allowed_library_ids=allowed,
        q=q,
        library_id=library_id,
        recent_paths=recent_paths,
    )
    if not favorites:
        page_paths = [item["path"] for item in result["items"]]
        if page_paths:
            fav_page = set(
                db.scalars(
                    select(Favorite.video_path).where(
                        Favorite.user_id == user.id,
                        Favorite.video_path.in_(page_paths),
                    )
                ).all()
            )
            for item in result["items"]:
                item["favorited"] = item["path"] in fav_page
    _annotate_shared(db, result["items"])
    result["library_ids"] = sorted(allowed)
    result["q"] = (q or "").strip()
    result["library_id"] = library_id
    result["recent"] = bool(recent)
    if tag_name:
        result["tag_name"] = tag_name
    if tag_id is not None:
        result["tag_id"] = tag_id
    return result


def _annotate_shared(db: Session, items: list) -> None:
    """标注当前页媒体是否有有效分享。"""
    if not items:
        return
    page_paths = [item["path"] for item in items if item.get("path")]
    active: set[str] = set()
    if page_paths:
        active = set(
            db.scalars(
                select(ShareLink.video_path).where(
                    ShareLink.video_path.in_(page_paths),
                    ShareLink.is_active == True,  # noqa: E712
                )
            ).all()
        )
        now = utcnow()
        expired = set(
            db.scalars(
                select(ShareLink.video_path).where(
                    ShareLink.video_path.in_(page_paths),
                    ShareLink.is_active == True,  # noqa: E712
                    or_(
                        and_(ShareLink.expires_at.is_not(None), ShareLink.expires_at <= now),
                        and_(ShareLink.max_views.is_not(None), ShareLink.view_count >= ShareLink.max_views),
                    ),
                )
            ).all()
        )
        active -= expired
    for item in items:
        item["shared"] = item.get("path") in active


class FavoriteRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


@app.get("/api/favorites")
def list_favorites(db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    rows = db.scalars(
        select(Favorite).where(Favorite.user_id == user.id).order_by(Favorite.created_at.desc())
    ).all()
    return {"items": [{"path": r.video_path, "created_at": r.created_at.isoformat()} for r in rows]}


@app.post("/api/favorites")
def add_favorite(
    body: FavoriteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    exists = db.scalar(
        select(Favorite).where(Favorite.user_id == user.id, Favorite.video_path == body.path)
    )
    if not user_can_access_media_path(db, user.id, body.path):
        raise HTTPException(status_code=403, detail="无权访问该媒体")
    if exists:
        return {"ok": True, "favorited": True}
    db.add(Favorite(user_id=user.id, video_path=body.path))
    db.commit()
    return {"ok": True, "favorited": True}


@app.delete("/api/favorites")
def remove_favorite(
    body: FavoriteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    row = db.scalar(
        select(Favorite).where(Favorite.user_id == user.id, Favorite.video_path == body.path)
    )
    if row:
        db.delete(row)
        db.commit()
    return {"ok": True, "favorited": False}


# ---------- 标记 ----------


@app.get("/api/tags")
def list_tags(db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    tags = db.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.id.asc())).all()
    counts: dict[int, int] = {}
    if tags:
        rows = db.execute(
            select(VideoTagAssignment.tag_id, func.count())
            .where(
                VideoTagAssignment.user_id == user.id,
                VideoTagAssignment.tag_id.in_([t.id for t in tags]),
            )
            .group_by(VideoTagAssignment.tag_id)
        ).all()
        counts = {tid: c for tid, c in rows}
    return {
        "items": [
            {
                "id": t.id,
                "name": t.name,
                "count": counts.get(t.id, 0),
                "created_at": t.created_at.isoformat(sep=" ", timespec="seconds") if t.created_at else "",
                "updated_at": t.updated_at.isoformat(sep=" ", timespec="seconds") if t.updated_at else "",
            }
            for t in tags
        ]
    }


@app.post("/api/tags", status_code=status.HTTP_201_CREATED)
def create_tag(body: TagCreateRequest, db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    name = body.name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "标记名称不能为空"})
    if db.scalar(select(Tag).where(Tag.user_id == user.id, Tag.name == name)):
        return JSONResponse(status_code=409, content={"detail": "标记名称已存在"})
    tag = Tag(user_id=user.id, name=name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    write_audit_log(db, user=user, action="tag_create", detail=f"创建标记: {name}")
    return {"ok": True, "id": tag.id, "name": tag.name}


@app.put("/api/tags/{tag_id}")
def update_tag(tag_id: int, body: TagUpdateRequest, db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    tag = db.scalar(select(Tag).where(Tag.id == tag_id, Tag.user_id == user.id))
    if not tag:
        raise HTTPException(status_code=404, detail="标记不存在")
    name = body.name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "标记名称不能为空"})
    existing = db.scalar(select(Tag).where(Tag.user_id == user.id, Tag.name == name, Tag.id != tag_id))
    if existing:
        return JSONResponse(status_code=409, content={"detail": "标记名称已存在"})
    old_name = tag.name
    tag.name = name
    tag.updated_at = utcnow()
    db.commit()
    write_audit_log(db, user=user, action="tag_update", detail=f"重命名标记: {old_name} → {name}")
    return {"ok": True, "id": tag.id, "name": tag.name}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int, db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    tag = db.scalar(select(Tag).where(Tag.id == tag_id, Tag.user_id == user.id))
    if not tag:
        raise HTTPException(status_code=404, detail="标记不存在")
    name = tag.name
    # 子表先批量清理（未定义 relationship，同 flush ORM 删除不保证先子后父）
    count = db.execute(delete(VideoTagAssignment).where(VideoTagAssignment.tag_id == tag.id)).rowcount or 0
    db.delete(tag)
    db.commit()
    write_audit_log(db, user=user, action="tag_delete", detail=f"删除标记: {name}（{count} 个关联已清理）")
    return {"ok": True, "deleted_id": tag_id}


@app.get("/api/videos/tags")
def video_tags(path: str = Query(..., min_length=1, max_length=1024), db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    if not user_can_access_media_path(db, user.id, path):
        raise HTTPException(status_code=403, detail="无权访问该媒体")
    tags = db.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.id.asc())).all()
    assigned = {
        va.tag_id
        for va in db.scalars(
            select(VideoTagAssignment).where(
                VideoTagAssignment.user_id == user.id,
                VideoTagAssignment.video_path == path,
            )
        ).all()
    }
    return {
        "path": path,
        "items": [
            {
                "id": t.id,
                "name": t.name,
                "assigned": t.id in assigned,
            }
            for t in tags
        ],
    }


@app.put("/api/videos/tags")
def set_video_tags(body: TagAssignRequest, db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    path = body.path.strip()
    if not path:
        return JSONResponse(status_code=400, content={"detail": "路径不能为空"})
    if not user_can_access_media_path(db, user.id, path):
        raise HTTPException(status_code=403, detail="无权访问该媒体")

    wanted = {int(x) for x in (body.tag_ids or [])}
    # 校验所有 tag_id 归属当前用户
    if wanted:
        valid_ids = {
            t.id
            for t in db.scalars(
                select(Tag).where(Tag.id.in_(wanted), Tag.user_id == user.id)
            ).all()
        }
        if valid_ids != wanted:
            raise HTTPException(status_code=400, detail="包含无效或不属于您的标记ID")

    existing = list(
        db.scalars(
            select(VideoTagAssignment).where(
                VideoTagAssignment.user_id == user.id,
                VideoTagAssignment.video_path == path,
            )
        ).all()
    )
    have = {va.tag_id for va in existing}

    added = []
    removed = []
    for va in existing:
        if va.tag_id not in wanted:
            db.delete(va)
            removed.append(str(va.tag_id))
    db.flush()
    for tid in sorted(wanted - have):
        db.add(VideoTagAssignment(user_id=user.id, tag_id=tid, video_path=path))
        added.append(str(tid))

    db.commit()
    if added or removed:
        a_names = _tag_id_names(db, added, user_id=user.id) if added else []
        r_names = _tag_id_names(db, removed, user_id=user.id) if removed else []
        write_audit_log(
            db,
            user=user,
            action="tag_assign",
            detail=f"标记 {path}: 添加{a_names or '无'} 移除{r_names or '无'}",
        )
    return {"ok": True, "assigned": sorted(wanted)}


def _tag_id_names(db: Session, ids: list[str], *, user_id: int) -> list[str]:
    if not ids:
        return []
    int_ids = [int(x) for x in ids]
    tags = db.scalars(select(Tag).where(Tag.id.in_(int_ids), Tag.user_id == user_id)).all()
    return [t.name for t in tags]


_refresh_lock = threading.Lock()
_refresh_last: dict[int, float] = {}
_REFRESH_COOLDOWN = 30.0


@app.post("/api/videos/refresh")
def refresh_videos(
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    """手动刷新视频扫描缓存；total 仅统计当前用户可见库。"""
    if not is_staff(user):
        now = time.time()
        with _refresh_lock:
            last = _refresh_last.get(user.id, 0.0)
            if now - last < _REFRESH_COOLDOWN:
                remain = int(_REFRESH_COOLDOWN - (now - last))
                raise HTTPException(status_code=429, detail=f"刷新过于频繁，请 {remain} 秒后再试")
            _refresh_last[user.id] = now
    invalidate_scan_cache()
    allowed = get_user_library_ids(db, user.id)
    result = scan_videos(
        db,
        sort="newest",
        page=1,
        limit=1,
        force_refresh=True,
        allowed_library_ids=allowed,
    )
    return {"ok": True, "total": result["total"], "library_ids": sorted(allowed)}


@app.get("/api/videos/stream/{file_path_encoded:path}")
def video_stream(
    file_path_encoded: str,
    request: Request,
    download: bool = Query(False),
    db: Session = Depends(get_db),
    payload=Depends(require_stream_access),
):
    user_info = lookup_stream_user(db, payload.sub)
    if not user_info or not user_info[1]:
        raise HTTPException(status_code=401, detail="未登录或账户已禁用")
    if int(user_info[2]) != int(payload.cv):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    rel = urllib.parse.unquote(file_path_encoded)
    if not user_can_access_media_path(db, user_info[0], rel):
        raise HTTPException(status_code=403, detail="无权访问该媒体(未分配对应存储库)")
    return stream_video_file(
        file_path_encoded,
        download=download,
        range_header=request.headers.get("range"),
        if_range=request.headers.get("if-range"),
    )


@app.get("/api/videos/subtitles/{file_path_encoded:path}")
def video_subtitles(
    file_path_encoded: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    rel = urllib.parse.unquote(file_path_encoded)
    if not user_can_access_media_path(db, user.id, rel):
        raise HTTPException(status_code=403, detail="无权访问该媒体")
    sub = find_sidecar_subtitle(rel)
    if not sub:
        raise HTTPException(status_code=404, detail="没有字幕")
    try:
        raw = sub.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="字幕无法读取") from exc
    if sub.suffix.lower() == ".srt":
        raw = srt_to_vtt(raw)
    elif not raw.lstrip().upper().startswith("WEBVTT"):
        raw = "WEBVTT\n\n" + raw
    return Response(content=raw, media_type="text/vtt; charset=utf-8")


@app.get("/api/libraries")
def my_libraries(db: Session = Depends(get_db), user: User = Depends(require_password_ok)):
    allowed = get_user_library_ids(db, user.id)
    if not allowed:
        return {"items": []}
    libs = db.scalars(
        select(Library).where(Library.id.in_(allowed), Library.enabled == True).order_by(Library.id.asc())  # noqa: E712
    ).all()
    return {"items": [{"id": lib.id, "name": lib.name, "path": lib.path} for lib in libs]}


@app.post("/api/videos/view")
def record_view(
    body: VideoViewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    if not user_can_access_media_path(db, user.id, body.path):
        raise HTTPException(status_code=403, detail="无权访问该媒体")
    wrote = write_view_log(db, user=user, path=body.path)
    return {"ok": True, "recorded": wrote}


@app.put("/api/videos/edit")
def video_edit(
    body: VideoEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
):
    _assert_media_access(db, user, body.old_path)
    result = edit_video(body)
    if body.new_name and result.get("path") and result["path"] != body.old_path:
        _repoint_favorites(db, body.old_path, result["path"])
        _repoint_shares(db, body.old_path, result["path"], title=result.get("title"))
        _repoint_tags(db, body.old_path, result["path"])
        db.commit()
    detail_parts = [f"编辑视频: {body.old_path}"]
    if body.new_name:
        detail_parts.append(f"→ {result['path']}")
    if body.new_date:
        detail_parts.append(f"mtime={body.new_date}")
    write_audit_log(db, user=user, action="edit", detail=" ".join(detail_parts))
    return result


@app.delete("/api/videos/delete")
def video_delete(
    body: VideoDeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
):
    _assert_media_access(db, user, body.path)
    result = delete_video(body.path)
    deleted = (result.get("deleted") or body.path).rstrip("/")
    # 精确 path + 目录前缀（合集内文件）一并清理收藏/分享/标记
    favs = list(db.scalars(select(Favorite).where(_path_exact_or_under(Favorite.video_path, deleted))).all())
    for fav in favs:
        db.delete(fav)
    shares = list(db.scalars(select(ShareLink).where(_path_exact_or_under(ShareLink.video_path, deleted))).all())
    if shares:
        # 访问明细先批量删除（未定义 relationship，同 flush 不保证先子后父）
        db.execute(delete(ShareView).where(ShareView.share_id.in_([s.id for s in shares])))
    for share in shares:
        db.delete(share)
    tag_assignments = list(db.scalars(select(VideoTagAssignment).where(_path_exact_or_under(VideoTagAssignment.video_path, deleted))).all())
    for va in tag_assignments:
        db.delete(va)
    db.commit()
    write_audit_log(db, user=user, action="delete", detail=f"删除媒体: {deleted}")
    slog("media_delete", user=user.username, path=deleted)
    return result


@app.put("/api/albums/rename")
def album_rename(
    body: AlbumRenameRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
):
    _assert_media_access(db, user, body.old_path)
    result = rename_album(body)
    old = body.old_path.rstrip("/")
    new = result["path"].rstrip("/")
    # 收藏合集路径 + 合集内图片收藏/分享前缀同步
    for fav in db.scalars(select(Favorite).where(_path_exact_or_under(Favorite.video_path, old))).all():
        if fav.video_path == old:
            fav.video_path = new
        elif fav.video_path.startswith(old + "/"):
            fav.video_path = new + fav.video_path[len(old) :]
    for share in db.scalars(select(ShareLink).where(_path_exact_or_under(ShareLink.video_path, old))).all():
        if share.video_path == old:
            share.video_path = new
            share.title = result.get("title") or share.title
        elif share.video_path.startswith(old + "/"):
            share.video_path = new + share.video_path[len(old) :]
    for va in db.scalars(select(VideoTagAssignment).where(_path_exact_or_under(VideoTagAssignment.video_path, old))).all():
        if va.video_path == old:
            va.video_path = new
        elif va.video_path.startswith(old + "/"):
            va.video_path = new + va.video_path[len(old) :]
    db.commit()
    write_audit_log(db, user=user, action="edit", detail=f"重命名合集: {old} → {new}")
    return result


@app.get("/api/albums/images")
def album_images_list(
    path: str = Query(..., min_length=1, max_length=1024),
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    """按需返回单个合集的完整图片列表（列表接口不再内联 images，减小响应体积）。"""
    rel = path.strip().replace("\\", "/").lstrip("/")
    if not user_can_access_media_path(db, user.id, rel):
        raise HTTPException(status_code=403, detail="无权访问该媒体")
    images = [im.model_dump() for im in load_album_images(rel)]
    if not images:
        raise HTTPException(status_code=404, detail="合集不存在")
    return {"ok": True, "path": rel, "count": len(images), "images": images}


@app.put("/api/albums/images")
def album_images_edit(
    body: AlbumImagesEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
):
    _assert_media_access(db, user, body.album_path)
    result = edit_album_images(body)
    for ch in result.get("changed") or []:
        old_p, new_p = ch.get("old_path"), ch.get("path")
        if not old_p or not new_p or old_p == new_p:
            continue
        _repoint_favorites(db, old_p, new_p)
        _repoint_shares(db, old_p, new_p, title=ch.get("title"))
        _repoint_tags(db, old_p, new_p)
    db.commit()
    write_audit_log(
        db,
        user=user,
        action="edit",
        detail=f"批量编辑合集图片: {body.album_path} ({len(result.get('changed') or [])})",
    )
    return result


# ---------- 后台 ----------

app.include_router(admin_router)
app.include_router(share_router)


# ---------- 前端 SPA ----------


@app.get("/")
def index_page():
    return HTMLResponse(_spa_html(), headers={"Cache-Control": "no-cache"})


@app.get("/s/{token}")
def share_page(token: str):
    """公开分享页：返回同一 SPA，前端按路径渲染"""
    return HTMLResponse(_spa_html(), headers={"Cache-Control": "no-cache"})


@app.get("/static/style.css")
def style_css():
    return FileResponse(
        STATIC_DIR / "style.css",
        media_type="text/css",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/manifest.webmanifest")
def pwa_manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/static/sw.js")
def service_worker():
    # SW 必须随时回源校验更新(不发长期缓存);默认 scope 仅为 /static/,显式放行 /
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


class _NoCacheStaticFiles(StaticFiles):
    """JS 子模块 URL 无版本号(入口 main.js?v=mtime 已破缓存)，必须 no-cache：
    StaticFiles 默认不发 Cache-Control，浏览器会按 Last-Modified 启发式新鲜期直接
    复用旧缓存，部署后出现「新入口 + 旧子模块」混跑白屏。
    no-cache 仅强制每次回源协商，304/ETag 逻辑继承 StaticFiles 不变。"""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp


app.mount("/static/js", _NoCacheStaticFiles(directory=str(STATIC_DIR / "js")), name="static-js")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
