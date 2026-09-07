"""
database.py — SQLite 连接、ORM 模型与初始化逻辑
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Generator, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/nasTok.db")

_IS_SQLITE = str(DATABASE_URL).startswith("sqlite")

# SQLite 需要 check_same_thread=False 以配合 FastAPI 多线程；
# NullPool 避免默认 QueuePool 跨线程复用同一连接。
_engine_kwargs: dict = {"echo": False}
if _IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    _engine_kwargs["poolclass"] = NullPool
engine = create_engine(DATABASE_URL, **_engine_kwargs)


@event.listens_for(engine, "connect")
def _sqlite_on_connect(dbapi_conn, _connection_record):
    if not str(DATABASE_URL).startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# pbkdf2 参数（stdlib，避免 bcrypt/passlib 在部分容器环境不兼容）
_PBKDF2_ITERATIONS = 260000


def utcnow() -> datetime:
    """UTC 当前时间（naive）。datetime.utcnow() 在 3.12+ 弃用；存量 DB 全为 naive，保持语义一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        plain.encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_password(plain: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, hex_hash = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            plain.encode("utf-8"),
            salt.encode("utf-8"),
            iters,
        )
        return hmac.compare_digest(dk.hex(), hex_hash)
    except (ValueError, TypeError):
        return False


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")  # sysadmin | admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credentials_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    logs: Mapped[List["AuditLog"]] = relationship(back_populates="user")

    def verify_password(self, plain: str) -> bool:
        return _verify_password(plain, self.password_hash)

    @staticmethod
    def hash_password(plain: str) -> str:
        return _hash_password(plain)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # login / view / edit / delete
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped[Optional[User]] = relationship(back_populates="logs")


class Library(Base):
    """视频存储库配置：path 为相对 MEDIA_ROOT 的子目录名，空字符串表示仅根目录文件（不递归子目录）"""

    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False, default="", unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserLibrary(Base):
    """用户可访问的存储库（新建用户默认无分配）"""

    __tablename__ = "user_libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    library_id: Mapped[int] = mapped_column(Integer, ForeignKey("libraries.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "library_id", name="uq_user_library"),
    )


DEFAULT_TAG_NAME = "稍后再看"


# ---------- 共享 Pydantic Schema ----------

class TagCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class TagUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class TagAssignRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)
    tag_ids: list[int] = Field(default_factory=list)


# ---------- 标记 ORM ----------


class Tag(Base):
    """用户自建的标记（用户内名称唯一）"""
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_tag_user_name"),
    )


class VideoTagAssignment(Base):
    """视频/图片/合集打标关联"""
    __tablename__ = "video_tag_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    video_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "tag_id", "video_path", name="uq_vta_user_tag_path"),
    )


def ensure_default_tags(db: Session, user: User) -> None:
    """幂等为用户添加默认标记"""
    existing = {t.name for t in db.scalars(select(Tag).where(Tag.user_id == user.id)).all()}
    if DEFAULT_TAG_NAME not in existing:
        db.add(Tag(user_id=user.id, name=DEFAULT_TAG_NAME))


class Favorite(Base):
    """用户收藏的视频（按相对路径记录）"""

    __tablename__ = "favorites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    video_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "video_path", name="uq_fav_user_path"),
    )


class ShareLink(Base):
    """公开分享链接（免登录可播）；video_path 唯一由 init_db 中的索引保证。"""

    __tablename__ = "share_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    video_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_viewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    max_views: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ShareView(Base):
    """分享访问明细（按 IP 日去重计入 unique）"""

    __tablename__ = "share_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    share_id: Mapped[int] = mapped_column(Integer, ForeignKey("share_links.id"), nullable=False, index=True)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    user_agent: Mapped[str] = mapped_column(String(256), nullable=False, default="")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- 轻量 schema 迁移 ----------
# PRAGMA user_version 驱动;所有迁移必须幂等(旧库 user_version=0 会全部重放)。
# 新增迁移:追加 (版本号, 函数) 到 _MIGRATIONS 尾部,并把 SCHEMA_VERSION +1。

SCHEMA_VERSION = 3


def _migrate_v1(conn) -> None:
    """基线:全部表结构 + 审计日志复合索引。"""
    Base.metadata.create_all(bind=conn)
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_audit_user_action_created "
            "ON audit_logs (user_id, action, created_at)"
        )
    )


def _migrate_v2(conn) -> None:
    """分享路径唯一:先清历史重复再建唯一索引。"""
    # 同路径多条:优先保留最早的有效分享,否则保留最新一条
    dup_paths = conn.execute(
        text(
            "SELECT video_path FROM share_links "
            "GROUP BY video_path HAVING COUNT(*) > 1"
        )
    ).fetchall()
    for (vpath,) in dup_paths:
        rows = conn.execute(
            text(
                "SELECT id, is_active FROM share_links "
                "WHERE video_path = :p ORDER BY id ASC"
            ),
            {"p": vpath},
        ).fetchall()
        keep_id = None
        for rid, active in rows:
            if active in (1, True, "1"):
                keep_id = rid
                break
        if keep_id is None and rows:
            keep_id = rows[-1][0]
        for rid, _ in rows:
            if rid == keep_id:
                continue
            conn.execute(text("DELETE FROM share_views WHERE share_id = :id"), {"id": rid})
            conn.execute(text("DELETE FROM share_links WHERE id = :id"), {"id": rid})
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_share_video_path "
            "ON share_links (video_path)"
        )
    )


def _column_names(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _migrate_v3(conn) -> None:
    """会话吊销版本号 + 分享过期/密码/次数上限。"""
    _add_column_if_missing(conn, "users", "credentials_version", "credentials_version INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "share_links", "expires_at", "expires_at DATETIME")
    _add_column_if_missing(conn, "share_links", "password_hash", "password_hash VARCHAR(255) NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "share_links", "max_views", "max_views INTEGER")
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_share_views_viewed_at "
            "ON share_views (viewed_at)"
        )
    )


_MIGRATIONS = [
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
]


def run_migrations() -> list[int]:
    """执行所有高于当前 user_version 的迁移,返回本次应用到的版本号列表。"""
    applied: list[int] = []
    with engine.begin() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar() or 0
        for version, fn in _MIGRATIONS:
            if version > current:
                fn(conn)
                # PRAGMA 不支持参数绑定;version 来自代码常量,安全
                conn.execute(text(f"PRAGMA user_version = {version}"))
                applied.append(version)
    return applied


def init_db() -> None:
    """创建/迁移表结构,并初始化默认 admin 账户与默认存储库"""
    # sqlite:////app/data/nasTok.db → /app/data
    raw = DATABASE_URL
    if raw.startswith("sqlite:///"):
        db_path = raw[len("sqlite:///") :]
        data_dir = os.path.dirname(db_path)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)

    applied = run_migrations()
    if applied:
        print(f"[NasTok] schema 已迁移到 v{max(applied)}(本次: {applied})")

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.username == "admin"))
        force_reset = os.getenv("RESET_ADMIN_PASSWORD", "").lower() in ("1", "true", "yes")

        if not admin:
            admin = User(
                username="admin",
                password_hash=User.hash_password("admin123"),
                role="sysadmin",
                is_active=True,
                must_change_password=False,
            )
            db.add(admin)
            print("[NasTok] 已创建默认系统管理员：用户名 admin / 密码 admin123")
        else:
            # 旧版 bcrypt 等非 pbkdf2 哈希无法校验 → 一律恢复默认密码
            legacy_hash = not (admin.password_hash or "").startswith("pbkdf2_sha256$")
            if force_reset or legacy_hash:
                admin.password_hash = User.hash_password("admin123")
                admin.must_change_password = False
                admin.is_active = True
                admin.role = "sysadmin"
                bump_credentials(admin)
                reason = "RESET_ADMIN_PASSWORD" if force_reset else "旧版密码哈希"
                print(f"[NasTok] 已重置系统管理员密码为 admin123（原因：{reason}）")
            else:
                if admin.role != "sysadmin":
                    admin.role = "sysadmin"
                    print("[NasTok] 已将内置 admin 账户角色迁移为 sysadmin")
                print(
                    "[NasTok] 系统管理员账户已存在。"
                    "若忘记密码，请在 docker-compose.yml 将 RESET_ADMIN_PASSWORD=true 后重启一次，再改回 false。"
                )

        # 默认存储库：只扫描 MEDIA_ROOT 根目录文件（不递归子目录）
        default_lib = db.scalar(select(Library).where(Library.name == "默认库"))
        if not default_lib:
            # 兼容旧数据：若已有 path="" 的库则跳过
            empty = db.scalar(select(Library).where(Library.path == ""))
            if not empty:
                db.add(Library(name="默认库", path="", enabled=True))
                db.flush()

        # 自动发现 MEDIA_ROOT 下的一级子目录作为存储库（已存在则跳过）
        media_root = os.getenv("MEDIA_ROOT", "/media/videos")
        if os.path.isdir(media_root):
            existing_paths = {lib.path for lib in db.scalars(select(Library)).all()}
            for entry in sorted(os.listdir(media_root)):
                full = os.path.join(media_root, entry)
                if os.path.isdir(full) and entry not in existing_paths:
                    db.add(Library(name=entry, path=entry, enabled=True))
                    existing_paths.add(entry)

        db.flush()
        # 为所有现有用户补齐默认标记
        for u in db.scalars(select(User)).all():
            ensure_default_tags(db, u)
        db.flush()
        # 内置超管始终拥有全部存储库；其他用户需手动分配（新建默认无库）
        admin = db.scalar(select(User).where(User.username == "admin"))
        if admin:
            all_lib_ids = [lib.id for lib in db.scalars(select(Library)).all()]
            have = set(
                db.scalars(
                    select(UserLibrary.library_id).where(UserLibrary.user_id == admin.id)
                ).all()
            )
            for lid in all_lib_ids:
                if lid not in have:
                    db.add(UserLibrary(user_id=admin.id, library_id=lid))

        db.commit()
    finally:
        db.close()


def write_audit_log(
    db: Session,
    *,
    user: Optional[User],
    action: str,
    detail: str,
) -> None:
    log = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "anonymous",
        action=action,
        detail=detail,
    )
    db.add(log)
    db.commit()


_view_dedupe_lock = threading.Lock()
_view_dedupe_seen: dict[tuple, float] = {}


def write_view_log(
    db: Session,
    *,
    user: User,
    path: str,
) -> bool:
    """
    记录观看日志：同一用户同一视频在自然日内只记一条，避免刷爆 SQLite。
    进程内加锁 + 内存去重，消除并发双写窗口。
    返回是否新写入。
    """
    detail = f"观看视频: {path}"
    day = utcnow().strftime("%Y-%m-%d")
    key = (user.id, path, day)
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    now = time.time()
    with _view_dedupe_lock:
        if len(_view_dedupe_seen) > 4000:
            cutoff = now - 86400
            for k, ts in list(_view_dedupe_seen.items()):
                if ts < cutoff:
                    _view_dedupe_seen.pop(k, None)
        if key in _view_dedupe_seen:
            return False

    exists = db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.user_id == user.id,
            AuditLog.action == "view",
            AuditLog.detail == detail,
            AuditLog.created_at >= day_start,
        )
        .limit(1)
    )
    if exists:
        with _view_dedupe_lock:
            _view_dedupe_seen[key] = now
        return False

    write_audit_log(db, user=user, action="view", detail=detail)
    with _view_dedupe_lock:
        _view_dedupe_seen[key] = now
    return True


def bump_credentials(user: User) -> None:
    """改密/重置/禁用时递增，使旧 JWT 立即失效。"""
    user.credentials_version = int(user.credentials_version or 0) + 1


def cleanup_old_logs(days: int = 90) -> int:
    """删除超过 days 天的审计日志与分享访问明细,返回删除条数。"""
    cutoff = utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        audit_n = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff)).rowcount or 0
        view_n = db.execute(delete(ShareView).where(ShareView.viewed_at < cutoff)).rowcount or 0
        db.commit()
        return int(audit_n) + int(view_n)
    finally:
        db.close()


def sqlite_db_file() -> Optional[str]:
    """DATABASE_URL 是 SQLite 文件库时返回文件路径,否则 None。"""
    if not str(DATABASE_URL).startswith("sqlite:///"):
        return None
    return str(DATABASE_URL)[len("sqlite:///") :]


def make_db_snapshot(dest_path: str) -> None:
    """用 SQLite online backup API 生成运行中数据库的一致性快照(WAL 安全)。
    源不存在/连接失败一律 RuntimeError(端点映射 400),绝不静默生成空快照。"""
    src_file = sqlite_db_file()
    if not src_file:
        raise RuntimeError("仅支持 SQLite 文件数据库备份")
    if not os.path.exists(src_file):
        raise RuntimeError("数据库文件不存在,无法备份")
    try:
        src = sqlite3.connect(src_file)
        try:
            dst = sqlite3.connect(dest_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"数据库快照失败: {exc}") from exc


def restore_db_snapshot(src_path: str) -> None:
    """用上传的 SQLite 快照替换当前库文件并重跑迁移。调用方须先校验文件头。"""
    dest = sqlite_db_file()
    if not dest:
        raise RuntimeError("仅支持 SQLite 文件数据库恢复")
    if not os.path.exists(src_path):
        raise RuntimeError("恢复源文件不存在")
    dest_dir = os.path.dirname(dest) or "."
    os.makedirs(dest_dir, exist_ok=True)
    engine.dispose()
    if os.path.exists(dest):
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        safety = os.path.join(dest_dir, f"nasTok.pre-restore-{stamp}.db")
        try:
            make_db_snapshot(safety)
        except RuntimeError:
            import shutil

            shutil.copy2(dest, safety)
    import shutil

    shutil.copy2(src_path, dest)
    for suffix in ("-wal", "-shm"):
        extra = dest + suffix
        if os.path.exists(extra):
            try:
                os.unlink(extra)
            except OSError:
                pass
    engine.dispose()
    run_migrations()
