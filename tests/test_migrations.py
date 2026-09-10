"""schema 迁移:user_version 驱动、幂等、旧库数据保留。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def patch_engine(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import app.database as database

    db_file = tmp_path / "mig.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(database, "engine", engine)
    return database, db_file


def test_migrations_apply_once_and_idempotent(patch_engine):
    database, db_file = patch_engine
    applied = database.run_migrations()
    assert applied == [1, 2, 3, 4]
    # 第二次全部跳过
    assert database.run_migrations() == []

    conn = sqlite3.connect(db_file)
    try:
        (ver,) = conn.execute("PRAGMA user_version").fetchone()
        assert ver == database.SCHEMA_VERSION
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for t in ("users", "libraries", "share_links", "video_tag_assignments", "ix_audit_user_action_created", "uq_share_video_path", "ix_share_views_viewed_at"):
            assert t in names, t
    finally:
        conn.close()


def test_migration_preserves_existing_user_data(patch_engine):
    """模拟旧库(user_version=0 且已有数据):迁移后数据不丢。"""
    database, db_file = patch_engine
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT, is_active INTEGER, must_change_password INTEGER, created_at TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'admin', 'x', 'sysadmin', 1, 0, '2026-01-01')")
    conn.commit()
    conn.close()

    applied = database.run_migrations()
    assert applied == [1, 2, 3, 4]

    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute("SELECT username FROM users").fetchall()
        assert rows == [("admin",)]
        (ver,) = conn.execute("PRAGMA user_version").fetchone()
        assert ver == database.SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_upgrades_legacy_schema_incrementally(tmp_path, monkeypatch):
    """手工复刻 v1 状态(表+审计索引,user_version=1) → 补 v2+v3。"""
    from sqlalchemy import create_engine, text
    import app.database as database

    db_file = tmp_path / "v1.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_user_action_created ON audit_logs (user_id, action, created_at)"))
        conn.execute(text("PRAGMA user_version = 1"))
    monkeypatch.setattr(database, "engine", engine)

    assert database.run_migrations() == [2, 3, 4]
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA user_version")).scalar() == 4
