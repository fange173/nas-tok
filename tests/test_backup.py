"""数据库备份:make_db_snapshot 一致性快照 + /api/admin/backup 端点权限与下载。"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def web(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"fake-video")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-length-32")

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.auth as auth
    import app.admin as admin_mod
    import app.share as share
    import app.main as main_mod

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    importlib.reload(auth)
    importlib.reload(admin_mod)
    importlib.reload(share)
    importlib.reload(main_mod)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main_mod.app.dependency_overrides[database.get_db] = override_get_db

    db = Session()
    admin_user = database.User(
        username="admin",
        password_hash=database.User.hash_password("admin123"),
        role="sysadmin",
        is_active=True,
        must_change_password=False,
    )
    db.add(admin_user)
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    yield {
        "client": client,
        "main": main_mod,
        "admin": admin_mod,
        "tmp_path": tmp_path,
        "monkeypatch": monkeypatch,
    }
    main_mod.app.dependency_overrides.clear()


def _login(client) -> None:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text


def test_make_db_snapshot_consistent_copy(tmp_path, monkeypatch):
    """WAL 模式下备份文件完整可读、行数一致。"""
    import app.database as database

    src = tmp_path / "live.db"
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [(i, f"u{i}") for i in range(50)])
    conn.commit()
    # WAL 未 checkpoint 也须经 backup 拿到一致性快照
    dest = tmp_path / "snap.db"

    monkeypatch.setattr(database, "sqlite_db_file", lambda: str(src))
    try:
        database.make_db_snapshot(str(dest))
    finally:
        conn.close()

    check = sqlite3.connect(dest)
    try:
        (count,) = check.execute("SELECT COUNT(*) FROM users").fetchone()
    finally:
        check.close()
    assert count == 50


def test_backup_endpoint_downloads_snapshot(web):
    """staff 可下载 octet-stream 快照,Content-Disposition 带时间戳文件名。"""
    client = web["client"]
    tmp_path = web["tmp_path"]

    # 用真实源文件替换快照源(内存引擎无法 backup)
    src = tmp_path / "live.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('backup-ok')")
    conn.commit()
    conn.close()
    web["monkeypatch"].setattr(
        "app.admin.make_db_snapshot",
        lambda dest: shutil.copy(str(src), dest),
    )

    r = client.post("/api/admin/backup")
    assert r.status_code == 401  # 未登录

    _login(client)
    r = client.post("/api/admin/backup")
    assert r.status_code == 200, r.text
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert "nasTok-backup-" in r.headers.get("Content-Disposition", "")
    assert r.content.startswith(b"SQLite format 3")
    # 审计日志已记录
    logs = client.get("/api/admin/logs", params={"action": "admin"}).json()["items"]
    assert any("数据库备份" in it["detail"] for it in logs)


def test_backup_endpoint_requires_staff(web):
    client = web["client"]
    _login(client)
    r = client.post("/api/admin/users", json={"username": "viewer", "password": "viewer123", "role": "user"})
    assert r.status_code == 201, r.text

    from fastapi.testclient import TestClient

    vc = TestClient(web["main"].app)
    r = vc.post("/api/auth/login", json={"username": "viewer", "password": "viewer123"})
    assert r.status_code == 200
    r = vc.post("/api/admin/backup")
    assert r.status_code == 403


def test_make_db_snapshot_missing_source_raises(tmp_path, monkeypatch):
    """源文件不存在:必须报错,绝不静默生成空快照(评审 M3)。"""
    import app.database as database

    monkeypatch.setattr(database, "sqlite_db_file", lambda: str(tmp_path / "nope.db"))
    with pytest.raises(RuntimeError, match="不存在"):
        database.make_db_snapshot(str(tmp_path / "out.db"))
    assert not (tmp_path / "out.db").exists()


def test_backup_endpoint_maps_runtimeerror_to_400(web, monkeypatch):
    """快照失败:端点把 RuntimeError 映射为 400,而非 500。"""
    client = web["client"]
    _login(client)
    monkeypatch.setattr("app.admin.make_db_snapshot", lambda dest: (_ for _ in ()).throw(RuntimeError("数据库文件不存在,无法备份")))
    r = client.post("/api/admin/backup")
    assert r.status_code == 400
    assert "无法备份" in r.json()["detail"]
