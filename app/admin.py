"""
admin.py — 后台管理：审计日志、用户管理、存储库配置
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.auth import (
    ROLE_ADMIN,
    ROLE_SYSADMIN,
    ROLE_USER,
    is_sysadmin,
    require_staff,
    require_sysadmin,
    user_to_dict,
)
from app.database import AuditLog, Favorite, Library, ShareLink, ShareView, Tag, TagCreateRequest, TagUpdateRequest, User, UserLibrary, VideoTagAssignment, SessionLocal, bump_credentials, ensure_default_tags, get_db, make_db_snapshot, reset_db_to_factory, restore_db_snapshot, utcnow, write_audit_log
from app.slog import slog
from app.video_handler import (
    MEDIA_ROOT,
    browse_media_dirs,
    count_videos_in_dir,
    discover_media_dirs,
    ensure_library_dir,
    find_nested_library_conflict,
    get_user_library_ids,
    grant_library_to_users,
    invalidate_media_access_cache,
    invalidate_scan_cache,
    library_byte_sizes,
    library_video_counts,
    normalize_library_path,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field("user", pattern="^(admin|user)$")


class UserStatusRequest(BaseModel):
    is_active: bool


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)


def _can_manage_target(actor: User, target: User) -> bool:
    """能否对目标执行启停/重置密码/删除等管理操作。"""
    if target.id == actor.id:
        return False
    if target.username == "admin" or is_sysadmin(target):
        return False
    if is_sysadmin(actor):
        return target.role in (ROLE_ADMIN, ROLE_USER)
    if actor.role == ROLE_ADMIN:
        return target.role == ROLE_USER
    return False


def _can_assign_libraries(actor: User, target: User) -> bool:
    """能否配置目标的存储库：超管所有人；管理员可配自己与普通用户。"""
    if not actor or not target:
        return False
    if is_sysadmin(actor):
        return True
    if actor.role == ROLE_ADMIN:
        if target.id == actor.id:
            return True
        if target.username == "admin" or is_sysadmin(target):
            return False
        return target.role == ROLE_USER
    return False


def _can_manage_tags(actor: User, target: User) -> bool:
    """能否管理目标的标记：sysadmin 全部；admin 自己和普通用户。"""
    if not actor or not target:
        return False
    if is_sysadmin(actor):
        return True
    if actor.role == ROLE_ADMIN:
        if target.id == actor.id:
            return True
        if target.username == "admin" or is_sysadmin(target):
            return False
        return target.role == ROLE_USER
    return False


class UserLibrariesRequest(BaseModel):
    library_ids: list[int] = Field(default_factory=list)


def _user_with_libraries(db: Session, user: User) -> dict:
    data = user_to_dict(user)
    lib_ids = sorted(get_user_library_ids(db, user.id))
    libs = []
    if lib_ids:
        rows = db.scalars(select(Library).where(Library.id.in_(lib_ids)).order_by(Library.id.asc())).all()
        libs = [{"id": lib.id, "name": lib.name, "path": lib.path} for lib in rows]
    data["library_ids"] = lib_ids
    data["libraries"] = libs
    return data


def _sysadmin_user_ids(db: Session) -> list[int]:
    return [u.id for u in db.scalars(select(User)).all() if is_sysadmin(u)]


def _grant_new_library(db: Session, lib: Library, actor: User) -> None:
    """新建库默认分给操作者与全部超管。"""
    uids = set(_sysadmin_user_ids(db))
    if actor:
        uids.add(actor.id)
    grant_library_to_users(db, lib.id, sorted(uids))


class LibraryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    path: str = Field("", max_length=512, description="相对 MEDIA_ROOT 的子目录，空=仅根目录文件")
    enabled: bool = True


class LibraryUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    enabled: Optional[bool] = None


def _lib_item(lib: Library, counts: dict[int, int] | None = None, sizes: dict[int, int] | None = None) -> dict:
    root = (MEDIA_ROOT / lib.path) if lib.path else MEDIA_ROOT
    exists = root.is_dir()
    if not exists:
        video_count = 0
    elif not lib.enabled:
        # 停用库不在扫描缓存里，单独按磁盘计数，避免 UI 显示 0 误导
        video_count = count_videos_in_dir(root, recursive=bool(lib.path))
    else:
        video_count = (counts or {}).get(lib.id, 0)
    return {
        "id": lib.id,
        "name": lib.name,
        "path": lib.path,
        "enabled": lib.enabled,
        "exists": exists,
        "absolute_path": str(root),
        "video_count": video_count,
        "byte_size": int((sizes or {}).get(lib.id, 0)),
        "scan_mode": "recursive" if lib.path else "root-only",
    }


@router.get("/logs")
def get_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    username: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_staff),
):
    q = select(AuditLog)
    count_q = select(func.count(AuditLog.id))
    if username:
        q = q.where(AuditLog.username == username)
        count_q = count_q.where(AuditLog.username == username)
    if action:
        q = q.where(AuditLog.action == action)
        count_q = count_q.where(AuditLog.action == action)

    total = db.scalar(count_q) or 0
    logs = db.scalars(
        q.order_by(AuditLog.created_at.desc()).offset((page - 1) * limit).limit(limit)
    ).all()

    return {
        "total": total,
        "page": page,
        "limit": limit,
        "items": [
            {
                "id": log.id,
                "user_id": log.user_id,
                "username": log.username,
                "action": log.action,
                "detail": log.detail,
                "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for log in logs
        ],
    }


@router.post("/backup")
def download_db_backup(
    db: Session = Depends(get_db),
    admin: User = Depends(require_sysadmin),
):
    """下载数据库一致性快照(SQLite online backup;临时文件响应后自动清理)。"""
    ts = utcnow().strftime("%Y%m%d-%H%M%S")
    fd, tmp_path = tempfile.mkstemp(prefix="nastok-backup-", suffix=".db")
    os.close(fd)
    try:
        make_db_snapshot(tmp_path)
    except (RuntimeError, OSError) as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise

    def _cleanup() -> None:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    write_audit_log(db, user=admin, action="admin", detail="下载数据库备份")
    slog("backup_download", user=admin.username)
    return FileResponse(
        tmp_path,
        media_type="application/octet-stream",
        filename=f"nasTok-backup-{ts}.db",
        background=BackgroundTask(_cleanup),
    )


@router.post("/restore")
async def restore_db_backup(
    db: Session = Depends(get_db),
    admin: User = Depends(require_sysadmin),
    file: UploadFile = File(...),
):
    """用上传的 SQLite 快照替换当前库。成功后需重新登录。"""
    raw = await file.read()
    if not raw.startswith(b"SQLite format 3"):
        raise HTTPException(status_code=400, detail="不是有效的 SQLite 数据库文件")
    fd, tmp_path = tempfile.mkstemp(prefix="nastok-restore-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(raw)
        import sqlite3

        conn = sqlite3.connect(tmp_path)
        try:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if not row:
                raise RuntimeError("备份文件缺少 users 表，拒绝恢复")
        finally:
            conn.close()
        restore_db_snapshot(tmp_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"恢复失败: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    who = admin.username
    slog("backup_restore", user=who, size=len(raw))
    return {"ok": True}


class FactoryResetRequest(BaseModel):
    confirm: str = Field(..., min_length=1, max_length=32)


@router.post("/reset")
def factory_reset(
    body: FactoryResetRequest,
    admin: User = Depends(require_sysadmin),
):
    """恢复默认设置:清空全部数据并重建出厂库。重置前自动保留安全快照。"""
    if body.confirm != "RESET":
        raise HTTPException(status_code=400, detail="请传入 confirm=RESET 确认重置")
    try:
        safety = reset_db_to_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"重置失败: {exc}") from exc
    slog("factory_reset", user=admin.username, safety=safety)
    # 重置后 Depends 注入的旧库会话已失效;用新库会话补审计,失败不阻断重置结果
    try:
        with SessionLocal() as s:
            factory_admin = s.scalar(select(User).where(User.username == "admin"))
            write_audit_log(
                s,
                user=factory_admin,
                action="admin",
                detail=f"恢复默认设置(操作者 {admin.username};重置前快照 {os.path.basename(safety)})",
            )
    except Exception:
        pass
    return {"ok": True, "safety": os.path.basename(safety)}


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_staff)):
    users = db.scalars(select(User).order_by(User.id.asc())).all()
    return {"items": [_user_with_libraries(db, u) for u in users]}


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if body.username == "admin":
        raise HTTPException(status_code=400, detail="不能创建保留用户名 admin")

    role = body.role
    if role == ROLE_SYSADMIN:
        raise HTTPException(status_code=400, detail="不能创建系统管理员")
    if role == ROLE_ADMIN and not is_sysadmin(actor):
        raise HTTPException(status_code=403, detail="仅系统管理员可创建管理员")
    if role not in (ROLE_ADMIN, ROLE_USER):
        raise HTTPException(status_code=400, detail="无效角色")
    if not is_sysadmin(actor) and role != ROLE_USER:
        raise HTTPException(status_code=403, detail="管理员只能创建普通用户")

    user = User(
        username=body.username,
        password_hash=User.hash_password(body.password),
        role=role,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ensure_default_tags(db, user)
    db.commit()
    # 新建用户默认不分配任何存储库
    write_audit_log(db, user=actor, action="admin", detail=f"创建用户 {user.username} (role={user.role})")
    return _user_with_libraries(db, user)


@router.get("/users/{user_id}/libraries")
def get_user_libraries(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_assign_libraries(actor, user):
        raise HTTPException(status_code=403, detail="无权配置该用户的存储库")
    return _user_with_libraries(db, user)


@router.put("/users/{user_id}/libraries")
def set_user_libraries(
    user_id: int,
    body: UserLibrariesRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_assign_libraries(actor, user):
        raise HTTPException(status_code=403, detail="无权配置该用户的存储库")

    wanted = sorted({int(x) for x in (body.library_ids or [])})
    if wanted:
        found = set(db.scalars(select(Library.id).where(Library.id.in_(wanted))).all())
        missing = [i for i in wanted if i not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"存储库不存在: {missing}")

    existing = list(db.scalars(select(UserLibrary).where(UserLibrary.user_id == user.id)).all())
    for row in existing:
        db.delete(row)
    # 必须先落库删除，否则同库重新插入会撞 uq_user_library
    db.flush()
    for lid in wanted:
        db.add(UserLibrary(user_id=user.id, library_id=lid))
    db.commit()
    invalidate_media_access_cache(user_id=user.id)
    write_audit_log(
        db,
        user=actor,
        action="admin",
        detail=f"配置用户 {user.username} 存储库: {wanted or '无'}",
    )
    return _user_with_libraries(db, user)


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UserStatusRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能禁用或变更自身账户状态")
    if not _can_manage_target(actor, user):
        raise HTTPException(status_code=403, detail="无权管理该用户")
    user.is_active = body.is_active
    if not body.is_active:
        bump_credentials(user)
    db.commit()
    invalidate_media_access_cache(user_id=user.id)
    write_audit_log(
        db,
        user=actor,
        action="admin",
        detail=f"{'启用' if body.is_active else '禁用'}用户 {user.username}",
    )
    return user_to_dict(user)


@router.put("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    body: ResetPasswordRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="请使用「修改密码」修改自己的密码")
    if not _can_manage_target(actor, user):
        raise HTTPException(status_code=403, detail="无权重置该用户密码")
    user.password_hash = User.hash_password(body.password)
    user.must_change_password = True
    bump_credentials(user)
    db.commit()
    invalidate_media_access_cache(user_id=user.id)
    write_audit_log(db, user=actor, action="admin", detail=f"重置用户密码 {user.username}")
    return {"ok": True, "id": user.id}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除自身账户")
    if not _can_manage_target(actor, user):
        raise HTTPException(status_code=403, detail="无权删除该用户")
    uname = user.username
    # 子表用批量 DELETE 立即落库再删父行：ShareView/ShareLink 等模型未定义
    # relationship，ORM 逐个 delete 在同一 flush 中不保证先子后父，外键强制时会失败
    share_ids = db.scalars(select(ShareLink.id).where(ShareLink.created_by == user.id)).all()
    if share_ids:
        db.execute(delete(ShareView).where(ShareView.share_id.in_(share_ids)))
        db.execute(delete(ShareLink).where(ShareLink.id.in_(share_ids)))
    db.execute(delete(UserLibrary).where(UserLibrary.user_id == user.id))
    db.execute(delete(Favorite).where(Favorite.user_id == user.id))
    db.execute(delete(VideoTagAssignment).where(VideoTagAssignment.user_id == user.id))
    db.execute(delete(Tag).where(Tag.user_id == user.id))
    db.delete(user)
    db.commit()
    invalidate_media_access_cache(user_id=user_id)
    write_audit_log(db, user=actor, action="admin", detail=f"删除用户 {uname}")
    return {"ok": True, "deleted_id": user_id}


@router.get("/libraries")
def list_libraries(db: Session = Depends(get_db), _: User = Depends(require_staff)):
    libs = db.scalars(select(Library).order_by(Library.id.asc())).all()
    registered = {lib.path for lib in libs if lib.path}
    discoverable = [d for d in discover_media_dirs() if d["path"] not in registered]
    counts = library_video_counts(db)
    sizes = library_byte_sizes(db)
    return {
        "items": [_lib_item(lib, counts, sizes) for lib in libs],
        "discoverable": discoverable,
        "media_root": str(MEDIA_ROOT),
        "hint": "请把 NAS 目录挂载到 /media/videos/<子目录>，再添加相对路径（如 movies）。默认库只扫根目录文件，子目录需单独启用。",
    }


@router.post("/libraries/sync")
def sync_libraries(db: Session = Depends(get_db), admin: User = Depends(require_staff)):
    """自动登记 MEDIA_ROOT 下尚未登记的一级子目录为存储库并启用"""
    existing = {lib.path: lib for lib in db.scalars(select(Library)).all()}
    added = []
    skipped = []
    for d in discover_media_dirs():
        if d["path"] in existing:
            continue
        try:
            ensure_library_dir(d["path"])
        except HTTPException as ex:
            skipped.append(f"{d['path']}({ex.detail})")
            continue
        conflict = find_nested_library_conflict(db, d["path"])
        if conflict:
            skipped.append(f"{d['path']}(与「{conflict.name}」嵌套)")
            continue
        # 名称冲突则加后缀
        name = d["name"]
        if db.scalar(select(Library).where(Library.name == name)):
            name = f"{name}-{d['path']}"
        lib = Library(name=name, path=d["path"], enabled=True)
        db.add(lib)
        db.flush()  # 让后续冲突检测能看到新库
        _grant_new_library(db, lib, admin)
        added.append(d["path"])
    db.commit()
    invalidate_scan_cache()
    if added:
        write_audit_log(db, user=admin, action="admin", detail=f"同步存储库: {', '.join(added)}")
    libs = db.scalars(select(Library).order_by(Library.id.asc())).all()
    counts = library_video_counts(db)
    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "items": [_lib_item(lib, counts) for lib in libs],
    }


@router.post("/libraries", status_code=status.HTTP_201_CREATED)
def create_library(
    body: LibraryCreateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_staff),
):
    path = normalize_library_path(body.path)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="库名称不能为空")

    if db.scalar(select(Library).where(Library.name == name)):
        raise HTTPException(status_code=409, detail="库名称已存在")
    existed = db.scalar(select(Library).where(Library.path == path))
    if existed:
        raise HTTPException(
            status_code=409,
            detail=f"路径「{path or '/'}」已登记为库「{existed.name}」，请直接启用它，无需重复添加",
        )

    ensure_library_dir(path)
    conflict = find_nested_library_conflict(db, path)
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"与已有库「{conflict.name}」（{conflict.path or '/'}）路径嵌套冲突。"
                f"请只保留父目录库或子目录库之一，避免重复扫描"
            ),
        )

    lib = Library(name=name, path=path, enabled=body.enabled)
    db.add(lib)
    db.flush()
    _grant_new_library(db, lib, admin)
    db.commit()
    db.refresh(lib)
    invalidate_scan_cache()
    write_audit_log(
        db,
        user=admin,
        action="admin",
        detail=f"添加存储库 {lib.name} ({lib.path or '/'})",
    )
    return _lib_item(lib, library_video_counts(db))


@router.get("/libraries/browse")
def browse_libraries(
    path: str = Query("", description="相对 MEDIA_ROOT 的当前目录"),
    db: Session = Depends(get_db),
    _: User = Depends(require_staff),
):
    """检索 / 浏览可添加的存储目录"""
    data = browse_media_dirs(path)
    registered = {
        lib.path: {"id": lib.id, "name": lib.name, "enabled": lib.enabled}
        for lib in db.scalars(select(Library)).all()
    }
    for child in data["children"]:
        info = registered.get(child["path"])
        child["registered"] = bool(info)
        child["library"] = info
    data["registered"] = data["path"] in registered
    data["library"] = registered.get(data["path"])
    return data


@router.delete("/libraries/{lib_id}")
def delete_library(
    lib_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_staff),
):
    """删除存储库配置（不删除磁盘上的视频文件）"""
    lib = db.get(Library, lib_id)
    if not lib:
        raise HTTPException(status_code=404, detail="存储库不存在")

    name, path = lib.name, lib.path
    # UserLibrary 子表先批量清理（未定义 relationship，同 flush ORM 删除不保证先子后父）
    db.execute(delete(UserLibrary).where(UserLibrary.library_id == lib.id))
    db.delete(lib)
    db.commit()
    invalidate_scan_cache()
    write_audit_log(
        db,
        user=admin,
        action="admin",
        detail=f"删除存储库 {name} ({path or '/'})",
    )
    return {"ok": True, "deleted_id": lib_id, "name": name, "path": path}


@router.put("/libraries/{lib_id}")
def update_library(
    lib_id: int,
    body: LibraryUpdateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_staff),
):
    lib = db.get(Library, lib_id)
    if not lib:
        raise HTTPException(status_code=404, detail="存储库不存在")

    if body.name is not None:
        conflict = db.scalar(
            select(Library).where(Library.name == body.name, Library.id != lib_id)
        )
        if conflict:
            raise HTTPException(status_code=409, detail="库名称已存在")
        lib.name = body.name
    if body.enabled is not None:
        lib.enabled = body.enabled

    db.commit()
    invalidate_scan_cache()
    write_audit_log(
        db,
        user=admin,
        action="admin",
        detail=f"更新存储库 {lib.name} enabled={lib.enabled}",
    )
    return _lib_item(lib, library_video_counts(db))


class ShareStatusRequest(BaseModel):
    is_active: bool


@router.get("/shares")
def list_shares(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
    path: str = Query("", description="按媒体相对路径精确过滤"),
    db: Session = Depends(get_db),
    _: User = Depends(require_staff),
):
    stmt = select(ShareLink)
    if path:
        stmt = stmt.where(ShareLink.video_path == path)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ShareLink.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()
    user_ids = {r.created_by for r in rows}
    users = {}
    if user_ids:
        for u in db.scalars(select(User).where(User.id.in_(user_ids))).all():
            users[u.id] = u.username

    items = []
    for s in rows:
        items.append(
            {
                "id": s.id,
                "token": s.token,
                "path": s.video_path,
                "title": s.title or Path(s.video_path).stem,
                "url": f"/s/{s.token}",
                "created_by": s.created_by,
                "created_by_name": users.get(s.created_by, f"#{s.created_by}"),
                "created_at": s.created_at.isoformat(sep=" ", timespec="seconds") if s.created_at else "",
                "is_active": bool(s.is_active),
                "view_count": int(s.view_count or 0),
                "unique_view_count": int(s.unique_view_count or 0),
                "last_viewed_at": s.last_viewed_at.isoformat(sep=" ", timespec="seconds")
                if s.last_viewed_at
                else "",
                "expires_at": s.expires_at.isoformat(sep=" ", timespec="seconds") if s.expires_at else "",
                "has_password": bool((s.password_hash or "").strip()),
                "max_views": s.max_views,
            }
        )
    return {"total": total, "page": page, "limit": limit, "items": items}


@router.get("/shares/{share_id}")
def share_detail(
    share_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_staff),
):
    share = db.get(ShareLink, share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")

    recent = db.scalars(
        select(ShareView)
        .where(ShareView.share_id == share_id)
        .order_by(ShareView.id.desc())
        .limit(50)
    ).all()
    creator = db.get(User, share.created_by)
    return {
        "item": {
            "id": share.id,
            "token": share.token,
            "path": share.video_path,
            "title": share.title or Path(share.video_path).stem,
            "url": f"/s/{share.token}",
            "created_by": share.created_by,
            "created_by_name": creator.username if creator else f"#{share.created_by}",
            "created_at": share.created_at.isoformat(sep=" ", timespec="seconds") if share.created_at else "",
            "is_active": bool(share.is_active),
            "view_count": int(share.view_count or 0),
            "unique_view_count": int(share.unique_view_count or 0),
            "last_viewed_at": share.last_viewed_at.isoformat(sep=" ", timespec="seconds")
            if share.last_viewed_at
            else "",
        },
        "recent_views": [
            {
                "id": v.id,
                "viewed_at": v.viewed_at.isoformat(sep=" ", timespec="seconds") if v.viewed_at else "",
                "ip_hash": v.ip_hash,
                "user_agent": v.user_agent,
            }
            for v in recent
        ],
    }


@router.put("/shares/{share_id}")
def update_share(
    share_id: int,
    body: ShareStatusRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_staff),
):
    share = db.get(ShareLink, share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")
    share.is_active = bool(body.is_active)
    db.commit()
    write_audit_log(
        db,
        user=admin,
        action="admin",
        detail=f"{'启用' if share.is_active else '停用'}分享 #{share.id} {share.video_path}",
    )
    return {"ok": True, "id": share.id, "is_active": share.is_active}


@router.delete("/shares/{share_id}")
def delete_share(
    share_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_staff),
):
    share = db.get(ShareLink, share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")
    path = share.video_path
    # 访问明细用批量 DELETE 立即落库：ShareView/ShareLink 未定义 relationship，
    # ORM 逐个 delete 在同一 flush 里不保证先子后父，会触发外键约束失败
    db.execute(delete(ShareView).where(ShareView.share_id == share_id))
    db.delete(share)
    db.commit()
    write_audit_log(db, user=admin, action="admin", detail=f"删除分享 #{share_id} {path}")
    return {"ok": True, "deleted_id": share_id}


# ---------- 标记管理 ----------


@router.get("/tags")
def admin_list_tags(
    user_id: Optional[int] = Query(None, ge=1),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    q = select(Tag)
    count_q = select(func.count(Tag.id))
    if user_id is not None:
        # 限定可管理目标范围
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not _can_manage_tags(actor, target):
            raise HTTPException(status_code=403, detail="无权管理该用户的标记")
        q = q.where(Tag.user_id == user_id)
        count_q = count_q.where(Tag.user_id == user_id)
    else:
        # 仅返回可管理用户范围的标记
        managed_ids = set()
        for u in db.scalars(select(User)).all():
            if _can_manage_tags(actor, u):
                managed_ids.add(u.id)
        if not managed_ids:
            return {"total": 0, "page": page, "limit": limit, "items": []}
        q = q.where(Tag.user_id.in_(managed_ids))
        count_q = count_q.where(Tag.user_id.in_(managed_ids))

    total = db.scalar(count_q) or 0
    tags = db.scalars(q.order_by(Tag.user_id.asc(), Tag.id.asc()).offset((page - 1) * limit).limit(limit)).all()
    users = {u.id: u.username for u in db.scalars(select(User)).all()}
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "items": [
            {
                "id": t.id,
                "user_id": t.user_id,
                "username": users.get(t.user_id, f"#{t.user_id}"),
                "name": t.name,
                "created_at": t.created_at.isoformat(sep=" ", timespec="seconds") if t.created_at else "",
                "updated_at": t.updated_at.isoformat(sep=" ", timespec="seconds") if t.updated_at else "",
            }
            for t in tags
        ],
    }


@router.get("/users/{user_id}/tags")
def admin_user_tags(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_manage_tags(actor, user):
        raise HTTPException(status_code=403, detail="无权管理该用户的标记")
    tags = db.scalars(select(Tag).where(Tag.user_id == user.id).order_by(Tag.id.asc())).all()
    return {
        "items": [
            {
                "id": t.id,
                "name": t.name,
                "created_at": t.created_at.isoformat(sep=" ", timespec="seconds") if t.created_at else "",
                "updated_at": t.updated_at.isoformat(sep=" ", timespec="seconds") if t.updated_at else "",
            }
            for t in tags
        ]
    }


@router.post("/users/{user_id}/tags", status_code=status.HTTP_201_CREATED)
def admin_create_tag(
    user_id: int,
    body: TagCreateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_manage_tags(actor, user):
        raise HTTPException(status_code=403, detail="无权管理该用户的标记")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="标记名称不能为空")
    if db.scalar(select(Tag).where(Tag.user_id == user.id, Tag.name == name)):
        raise HTTPException(status_code=409, detail="标记名称已存在")
    tag = Tag(user_id=user.id, name=name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    write_audit_log(db, user=actor, action="tag_create", detail=f"为用户 {user.username} 创建标记: {name}")
    return {"ok": True, "id": tag.id, "name": tag.name}


@router.put("/users/{user_id}/tags/{tag_id}")
def admin_update_tag(
    user_id: int,
    tag_id: int,
    body: TagUpdateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_manage_tags(actor, user):
        raise HTTPException(status_code=403, detail="无权管理该用户的标记")
    tag = db.scalar(select(Tag).where(Tag.id == tag_id, Tag.user_id == user.id))
    if not tag:
        raise HTTPException(status_code=404, detail="标记不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="标记名称不能为空")
    existing = db.scalar(select(Tag).where(Tag.user_id == user.id, Tag.name == name, Tag.id != tag_id))
    if existing:
        raise HTTPException(status_code=409, detail="标记名称已存在")
    old_name = tag.name
    tag.name = name
    tag.updated_at = utcnow()
    db.commit()
    write_audit_log(db, user=actor, action="tag_update", detail=f"重命名用户 {user.username} 的标记: {old_name} → {name}")
    return {"ok": True, "id": tag.id, "name": tag.name}


@router.delete("/users/{user_id}/tags/{tag_id}")
def admin_delete_tag(
    user_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_staff),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not _can_manage_tags(actor, user):
        raise HTTPException(status_code=403, detail="无权管理该用户的标记")
    tag = db.scalar(select(Tag).where(Tag.id == tag_id, Tag.user_id == user.id))
    if not tag:
        raise HTTPException(status_code=404, detail="标记不存在")
    name = tag.name
    # 子表先批量清理（未定义 relationship，同 flush ORM 删除不保证先子后父）
    count = db.execute(delete(VideoTagAssignment).where(VideoTagAssignment.tag_id == tag.id)).rowcount or 0
    db.delete(tag)
    db.commit()
    write_audit_log(db, user=actor, action="tag_delete", detail=f"删除用户 {user.username} 的标记: {name}（{count} 个关联已清理）")
    return {"ok": True, "deleted_id": tag_id}
