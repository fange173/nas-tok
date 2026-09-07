"""
auth.py — JWT Cookie 鉴权、登录限流、轻量流鉴权
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from jwt import InvalidTokenError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import User, get_db, utcnow
from app.database import _verify_password

SECRET_KEY = os.getenv("SECRET_KEY", "nastok-dev-secret-change-me")
# 仅在反向代理（nginx 等）之后部署时才信任 X-Forwarded-For；
# 直连部署时为防伪造绕过登录限流，忽略该头
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")
ALGORITHM = "HS256"
COOKIE_NAME = "nastok_token"

SESSION_EXPIRE_HOURS = 24
REMEMBER_EXPIRE_DAYS = 90

# 登录失败限流：同 IP 连续失败 N 次后锁定 M 秒
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "8"))
LOGIN_LOCK_SECONDS = int(os.getenv("LOGIN_LOCK_SECONDS", "300"))
# HTTPS 反代部署时设为 true,使会话 cookie 仅走加密信道(直连 HTTP 保持 false)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

_login_guard_lock = threading.Lock()
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_locked_until: dict[str, float] = {}


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    remember: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=6, max_length=128)


class TokenPayload(BaseModel):
    sub: str
    uid: int
    role: str
    mcp: bool = False  # must_change_password
    cv: int = 0
    exp: datetime


def warn_if_weak_secret() -> None:
    weak = {"nastok-dev-secret-change-me", "change-me-to-a-long-random-secret-in-production", ""}
    if SECRET_KEY in weak or len(SECRET_KEY) < 24:
        print(
            "[NasTok] 警告: SECRET_KEY 过于简单或仍为默认值，"
            "请在 docker-compose.yml 中设置随机长密钥。"
        )


def create_access_token(user: User, remember: bool = False) -> tuple[str, int]:
    if remember:
        expire = utcnow() + timedelta(days=REMEMBER_EXPIRE_DAYS)
        max_age = REMEMBER_EXPIRE_DAYS * 24 * 3600
    else:
        expire = utcnow() + timedelta(hours=SESSION_EXPIRE_HOURS)
        max_age = SESSION_EXPIRE_HOURS * 3600

    payload = {
        "sub": user.username,
        "uid": user.id,
        "role": user.role,
        "mcp": bool(user.must_change_password),
        "cv": int(user.credentials_version or 0),
        "exp": expire,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, max_age


def set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=max_age,
        path="/",
        secure=COOKIE_SECURE,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", secure=COOKIE_SECURE)


def _decode_token(token: str) -> TokenPayload:
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return TokenPayload(
            sub=data["sub"],
            uid=int(data["uid"]),
            role=data.get("role", "user"),
            mcp=bool(data.get("mcp", False)),
            cv=int(data.get("cv", 0) or 0),
            exp=datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc).replace(tzinfo=None),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的登录凭证",
        ) from exc


def get_client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _sweep_login_guard(now: float) -> None:
    """清理已过期的锁定与失效窗口,防止 IP 字典无界增长。"""
    for ip in list(_login_locked_until.keys()):
        if _login_locked_until.get(ip, 0) <= now:
            _login_locked_until.pop(ip, None)
            _login_failures.pop(ip, None)
    for ip in list(_login_failures.keys()):
        window = [t for t in _login_failures[ip] if now - t < LOGIN_LOCK_SECONDS]
        if window:
            _login_failures[ip] = window
        else:
            _login_failures.pop(ip, None)


def check_login_allowed(ip: str) -> None:
    now = time.time()
    with _login_guard_lock:
        _sweep_login_guard(now)
        until = _login_locked_until.get(ip, 0)
        if until > now:
            remain = int(until - now)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"登录失败次数过多，请 {remain} 秒后再试",
            )


def record_login_failure(ip: str) -> None:
    now = time.time()
    with _login_guard_lock:
        _sweep_login_guard(now)
        window = [t for t in _login_failures[ip] if now - t < LOGIN_LOCK_SECONDS]
        window.append(now)
        _login_failures[ip] = window
        if len(window) >= LOGIN_MAX_FAILURES:
            _login_locked_until[ip] = now + LOGIN_LOCK_SECONDS
            _login_failures[ip] = []


def clear_login_failures(ip: str) -> None:
    with _login_guard_lock:
        _login_failures.pop(ip, None)
        _login_locked_until.pop(ip, None)


def token_is_remembered(token: str) -> bool:
    """按 token 剩余有效期推断会话类型：远超普通会话时长即视为 remember 会话。"""
    try:
        payload = _decode_token(token)
    except HTTPException:
        return False
    remaining = (payload.exp - utcnow()).total_seconds()
    return remaining > SESSION_EXPIRE_HOURS * 3600


def get_current_user(
    nastok_token: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    if not nastok_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    payload = _decode_token(nastok_token)
    user = db.scalar(select(User).where(User.id == payload.uid))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    if int(payload.cv) != int(user.credentials_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录")
    return user


ROLE_SYSADMIN = "sysadmin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"
STAFF_ROLES = frozenset({ROLE_SYSADMIN, ROLE_ADMIN})


def is_sysadmin(user: User) -> bool:
    """系统管理员：role=sysadmin，或兼容旧数据的内置 admin 账号。"""
    if not user:
        return False
    if user.role == ROLE_SYSADMIN:
        return True
    return user.username == "admin" and user.role in (ROLE_SYSADMIN, ROLE_ADMIN, "admin")


def is_staff(user: User) -> bool:
    """可进后台：系统管理员或管理员。"""
    if not user:
        return False
    if user.role in STAFF_ROLES:
        return True
    # 兼容：内置 admin 尚未迁移时仍视为 staff
    return user.username == "admin"


def require_staff(user: User = Depends(get_current_user)) -> User:
    if not is_staff(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def require_sysadmin(user: User = Depends(get_current_user)) -> User:
    if not is_sysadmin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限")
    return user


def require_password_ok(user: User = Depends(get_current_user)) -> User:
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请先修改初始密码",
            headers={"X-Must-Change-Password": "1"},
        )
    return user


def require_stream_access(
    nastok_token: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> TokenPayload:
    """
    视频流轻量鉴权：只校验 JWT，不查库（Range 请求极多）。
    禁用用户会在 token 过期或下次业务 API 时失效。
    """
    if not nastok_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    payload = _decode_token(nastok_token)
    if payload.mcp:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请先修改初始密码")
    return payload


_DUMMY_PASSWORD_HASH = User.hash_password("nastok-dummy-not-a-real-password")


def authenticate_user(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        _verify_password(password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not user.verify_password(password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用")
    return user


def user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "is_staff": is_staff(user),
        "is_sysadmin": is_sysadmin(user),
    }
