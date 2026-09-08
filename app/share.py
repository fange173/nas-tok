"""
share.py — 公开分享链接：创建、免鉴权播放、访问统计
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import COOKIE_SECURE, SECRET_KEY, get_client_ip, is_staff, require_password_ok
from app.database import ShareLink, ShareView, User, _verify_password, get_db, utcnow, write_audit_log
from app.slog import slog
from app.video_handler import (
    IMAGE_EXTENSIONS,
    _ensure_under_media,
    load_album_images,
    stream_video_file,
    user_can_access_media_path,
)

router = APIRouter(tags=["share"])


class CreateShareRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)
    expires_in_hours: Optional[int] = Field(None, ge=1, le=24 * 365)
    password: Optional[str] = Field(None, min_length=0, max_length=128)
    max_views: Optional[int] = Field(None, ge=1, le=1_000_000)


class ShareAuthRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=128)


SHARE_VIEW_THROTTLE_SECONDS = int(os.getenv("SHARE_VIEW_THROTTLE_SECONDS", "60"))
_share_view_throttle: dict[str, float] = {}
_share_view_lock = threading.Lock()
_UNLOCK_TTL = 86400


def _new_token() -> str:
    return secrets.token_urlsafe(18)


def _title_from_path(rel: str) -> str:
    return Path(rel).stem or rel


def _kind_for_rel(rel: str) -> str:
    try:
        target = _ensure_under_media(rel)
    except HTTPException:
        return "video"
    if target.is_dir():
        return "album"
    ext = Path(rel).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "video"


def _media_exists(rel: str) -> bool:
    try:
        p = _ensure_under_media(rel)
    except HTTPException:
        return False
    if p.is_file():
        return True
    if p.is_dir():
        return bool(load_album_images(rel))
    return False


def _share_expired(share: ShareLink, *, for_stream: bool = False) -> bool:
    if share.expires_at and share.expires_at <= utcnow():
        return True
    # 打开次数按「页面打开」计；拉流不再消耗次数，避免 max_views=1 时页 200、流 404
    if not for_stream and share.max_views is not None and int(share.view_count or 0) >= int(share.max_views):
        return True
    return False


def get_active_share(db: Session, token: str, *, for_stream: bool = False) -> ShareLink:
    share = db.scalar(select(ShareLink).where(ShareLink.token == token))
    if not share or not share.is_active or _share_expired(share, for_stream=for_stream):
        raise HTTPException(status_code=404, detail="分享不存在或已失效")
    if not _media_exists(share.video_path):
        raise HTTPException(status_code=404, detail="分享的媒体已不存在")
    return share


def _ip_hash(ip: str) -> str:
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()[:32]


def _ua_short(ua: str) -> str:
    return (ua or "")[:240]


def _unlock_cookie_name(token: str) -> str:
    return "nastok_su_" + token[:20]


def _unlock_payload(token: str, password_hash: str, exp: int) -> bytes:
    return f"{token}|{password_hash}|{exp}".encode()


def _make_unlock_value(token: str, password_hash: str) -> str:
    exp = int(time.time()) + _UNLOCK_TTL
    sig = hmac.new(SECRET_KEY.encode(), _unlock_payload(token, password_hash, exp), hashlib.sha256).hexdigest()[:24]
    return f"{exp}.{sig}"


def _unlock_valid(token: str, password_hash: str, value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        exp_s, sig = value.split(".", 1)
        if int(exp_s) < int(time.time()):
            return False
        expect = hmac.new(
            SECRET_KEY.encode(),
            _unlock_payload(token, password_hash, int(exp_s)),
            hashlib.sha256,
        ).hexdigest()[:24]
        return hmac.compare_digest(expect, sig)
    except (ValueError, TypeError):
        return False


def _share_unlocked(request: Request, share: ShareLink) -> bool:
    if not (share.password_hash or "").strip():
        return True
    return _unlock_valid(
        share.token,
        share.password_hash or "",
        request.cookies.get(_unlock_cookie_name(share.token)),
    )


def _require_unlocked(request: Request, share: ShareLink) -> None:
    if not _share_unlocked(request, share):
        raise HTTPException(status_code=401, detail="需要分享密码")


def record_share_view(db: Session, share: ShareLink, request: Request) -> None:
    """每次打开/拉流记一次 view_count；同一 IP 自然日只增加一次 unique_view_count。"""
    ip = get_client_ip(request)
    ip_h = _ip_hash(ip)
    throttle_key = f"{ip_h}:{share.id}"
    now = time.time()
    with _share_view_lock:
        if now - _share_view_throttle.get(throttle_key, 0.0) < SHARE_VIEW_THROTTLE_SECONDS:
            return
        _share_view_throttle[throttle_key] = now
        if len(_share_view_throttle) > 5000:
            cutoff = now - SHARE_VIEW_THROTTLE_SECONDS
            for k in list(_share_view_throttle.keys()):
                if _share_view_throttle[k] < cutoff:
                    _share_view_throttle.pop(k, None)
    ua = _ua_short(request.headers.get("user-agent", ""))
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    share.view_count = int(share.view_count or 0) + 1
    share.last_viewed_at = utcnow()

    seen = db.scalar(
        select(ShareView.id)
        .where(
            ShareView.share_id == share.id,
            ShareView.ip_hash == ip_h,
            ShareView.viewed_at >= day_start,
        )
        .limit(1)
    )
    if not seen:
        share.unique_view_count = int(share.unique_view_count or 0) + 1

    db.add(
        ShareView(
            share_id=share.id,
            ip_hash=ip_h,
            user_agent=ua,
        )
    )
    db.commit()


def _apply_share_options(share: ShareLink, body: CreateShareRequest, *, clear_password: bool) -> None:
    if body.expires_in_hours:
        share.expires_at = utcnow() + timedelta(hours=int(body.expires_in_hours))
    else:
        share.expires_at = None
    if body.password:
        share.password_hash = User.hash_password(body.password)
    elif clear_password:
        share.password_hash = ""
    share.max_views = body.max_views


def _share_payload(
    request: Request,
    share: ShareLink,
    *,
    restored: bool = False,
    reused: bool = False,
    rotated: bool = False,
) -> dict:
    url = str(request.base_url).rstrip("/") + f"/s/{share.token}"
    return {
        "ok": True,
        "shared": True,
        "restored": restored,
        "reused": reused,
        "rotated": rotated,
        "id": share.id,
        "token": share.token,
        "url": url,
        "path": share.video_path,
        "title": share.title or _title_from_path(share.video_path),
        "kind": _kind_for_rel(share.video_path),
        "created_at": share.created_at.isoformat(sep=" ", timespec="seconds") if share.created_at else "",
        "expires_at": share.expires_at.isoformat(sep=" ", timespec="seconds") if share.expires_at else "",
        "has_password": bool((share.password_hash or "").strip()),
        "max_views": share.max_views,
    }


def _dedupe_shares_for_path(db: Session, rel: str) -> Optional[ShareLink]:
    """同一媒体只保留一条分享记录。"""
    rows = list(
        db.scalars(
            select(ShareLink).where(ShareLink.video_path == rel).order_by(ShareLink.id.desc())
        ).all()
    )
    if not rows:
        return None
    primary = next((r for r in reversed(rows) if r.is_active), rows[0])
    for extra in rows:
        if extra.id == primary.id:
            continue
        # 访问明细先批量删除（未定义 relationship，同 flush 不保证先子后父）
        db.execute(delete(ShareView).where(ShareView.share_id == extra.id))
        db.delete(extra)
    if len(rows) > 1:
        db.flush()
    return primary


def _clear_share_views(db: Session, share: ShareLink) -> None:
    for v in db.scalars(select(ShareView).where(ShareView.share_id == share.id)).all():
        db.delete(v)


def _commit_existing_share(
    db: Session,
    request: Request,
    user: User,
    rel: str,
    existing: ShareLink,
    body: CreateShareRequest,
) -> dict:
    was_active = bool(existing.is_active) and not _share_expired(existing)
    if was_active:
        if existing.created_by != user.id and not is_staff(user):
            raise HTTPException(status_code=409, detail="该媒体已有分享链接")
        _apply_share_options(existing, body, clear_password=False)
        db.commit()
        db.refresh(existing)
        return _share_payload(request, existing, reused=True)

    old_token = existing.token
    existing.is_active = True
    existing.token = _new_token()
    existing.created_at = utcnow()
    existing.title = _title_from_path(rel)
    existing.created_by = user.id
    existing.view_count = 0
    existing.unique_view_count = 0
    existing.last_viewed_at = None
    _apply_share_options(existing, body, clear_password=True)
    _clear_share_views(db, existing)
    db.commit()
    db.refresh(existing)
    write_audit_log(
        db,
        user=user,
        action="share",
        detail=f"重新分享: {rel} token={existing.token} (旧 {old_token} 已作废)",
    )
    slog("share_rotate", user=user.username, path=rel, token=existing.token)
    return _share_payload(request, existing, restored=True, rotated=True)


@router.post("/api/shares", status_code=status.HTTP_201_CREATED)
def create_share(
    body: CreateShareRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    if not is_staff(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅管理员可分享")
    rel = body.path.strip().replace("\\", "/")
    if not user_can_access_media_path(db, user.id, rel):
        raise HTTPException(status_code=403, detail="无权分享该媒体(未分配对应存储库)")
    try:
        target = _ensure_under_media(rel)
    except HTTPException:
        target = None
    if not _media_exists(rel):
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    if target is not None and target.is_dir() and not load_album_images(rel):
        raise HTTPException(status_code=400, detail="空合集无法分享")

    existing = _dedupe_shares_for_path(db, rel)
    if existing:
        return _commit_existing_share(db, request, user, rel, existing, body)

    last_error: Optional[Exception] = None
    for _ in range(5):
        token = _new_token()
        share = ShareLink(
            token=token,
            video_path=rel,
            title=_title_from_path(rel),
            created_by=user.id,
            is_active=True,
        )
        _apply_share_options(share, body, clear_password=True)
        db.add(share)
        try:
            db.commit()
        except IntegrityError as exc:
            last_error = exc
            db.rollback()
            raced = _dedupe_shares_for_path(db, rel)
            if raced:
                return _commit_existing_share(db, request, user, rel, raced, body)
            continue
        db.refresh(share)
        write_audit_log(db, user=user, action="share", detail=f"分享媒体: {rel} token={token}")
        slog("share_create", user=user.username, path=rel, token=token)
        return _share_payload(request, share)

    raise HTTPException(
        status_code=409,
        detail="分享创建冲突，请重试",
    ) from last_error


@router.delete("/api/shares")
def revoke_share(
    body: CreateShareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_password_ok),
):
    """取消分享：停用该媒体的分享记录（保留记录；再次分享会换新 token）。"""
    rel = body.path.strip().replace("\\", "/")
    share = _dedupe_shares_for_path(db, rel)
    if not share:
        return {"ok": True, "shared": False, "path": rel}
    if share.created_by != user.id and not is_staff(user):
        raise HTTPException(status_code=403, detail="只能取消自己创建的分享")
    share.is_active = False
    db.commit()
    write_audit_log(db, user=user, action="share", detail=f"取消分享: {rel} token={share.token}")
    slog("share_revoke", user=user.username, path=rel, token=share.token)
    return {"ok": True, "shared": False, "id": share.id, "path": rel}


def _public_share_body(share: ShareLink, *, unlocked: bool) -> dict:
    kind = _kind_for_rel(share.video_path)
    payload = {
        "ok": True,
        "token": share.token,
        "title": share.title or _title_from_path(share.video_path),
        "kind": kind,
        "needs_password": bool((share.password_hash or "").strip()) and not unlocked,
        "view_count": share.view_count,
        "expires_at": share.expires_at.isoformat(sep=" ", timespec="seconds") if share.expires_at else "",
    }
    if payload["needs_password"]:
        return payload
    if kind == "album":
        images = load_album_images(share.video_path)
        payload["images"] = [
            {
                "title": im.title,
                "stream_url": f"/api/share/{share.token}/stream?i={idx}",
            }
            for idx, im in enumerate(images)
        ]
        payload["count"] = len(images)
    else:
        payload["stream_url"] = f"/api/share/{share.token}/stream"
    return payload


@router.get("/api/share/{token}")
def get_share_public(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    share = get_active_share(db, token)
    unlocked = _share_unlocked(request, share)
    if unlocked:
        record_share_view(db, share, request)
        db.refresh(share)
    return _public_share_body(share, unlocked=unlocked)


@router.post("/api/share/{token}/auth")
def auth_share(
    token: str,
    body: ShareAuthRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    share = get_active_share(db, token)
    if not (share.password_hash or "").strip():
        return _public_share_body(share, unlocked=True)
    if not _verify_password(body.password, share.password_hash):
        raise HTTPException(status_code=401, detail="分享密码错误")
    response.set_cookie(
        key=_unlock_cookie_name(token),
        value=_make_unlock_value(token, share.password_hash or ""),
        httponly=True,
        samesite="lax",
        max_age=_UNLOCK_TTL,
        path="/",
        secure=COOKIE_SECURE,
    )
    record_share_view(db, share, request)
    db.refresh(share)
    slog("share_unlock", token=token)
    return _public_share_body(share, unlocked=True)


@router.get("/api/share/{token}/stream")
def stream_share(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    i: Optional[int] = None,
):
    share = get_active_share(db, token, for_stream=True)
    _require_unlocked(request, share)
    rel = share.video_path
    if i is not None:
        images = load_album_images(rel)
        if i < 0 or i >= len(images):
            raise HTTPException(status_code=404, detail="图片不存在")
        rel = images[i].path
    encoded = urllib.parse.quote(rel, safe="")
    return stream_video_file(
        encoded,
        range_header=request.headers.get("range"),
        if_range=request.headers.get("if-range"),
    )
