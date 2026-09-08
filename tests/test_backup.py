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


def test_reset_db_to_factory(tmp_path, monkeypatch):
    """恢复默认:脏库清空回出厂(仅默认 admin/默认库),旧数据完整留在安全快照。"""
    import os

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    import app.database as database

    db_file = tmp_path / "live.db"
    eng = create_engine(f"sqlite:///{db_file}")
    database.Base.metadata.create_all(eng)
    with sessionmaker(bind=eng)() as s:
        s.add(database.User(username="bob", password_hash=database.User.hash_password("bob12345"), role="user", is_active=True, must_change_password=False))
        s.add(database.Library(name="私人库", path="secret", enabled=True))
        s.commit()
    eng.dispose()

    # 全局指向该文件库(MEDIA_ROOT 指向不存在目录→不触发子目录自动发现)
    fresh = create_engine(f"sqlite:///{db_file}")
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(database, "engine", fresh)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=fresh))
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media-absent"))

    safety = database.reset_db_to_factory()

    assert os.path.exists(safety)
    snap = create_engine(f"sqlite:///{safety}")
    with sessionmaker(bind=snap)() as s:
        assert s.scalar(select(func.count()).select_from(database.User).where(database.User.username == "bob")) == 1
        assert s.scalar(select(func.count()).select_from(database.Library).where(database.Library.name == "私人库")) == 1
    snap.dispose()
    fresh.dispose()

    check = create_engine(f"sqlite:///{db_file}")
    with sessionmaker(bind=check)() as s:
        assert [u.username for u in s.scalars(select(database.User)).all()] == ["admin"]
        assert [l.name for l in s.scalars(select(database.Library)).all()] == ["默认库"]
        assert s.scalar(select(func.count()).select_from(database.ShareLink)) == 0
    check.dispose()


def test_factory_reset_endpoint(web, monkeypatch):
    """reset 端点:未登录 401 / 非 sysadmin 403 / 错确认词 400 / 正确调用回传安全快照名。"""
    from fastapi.testclient import TestClient

    client = web["client"]

    anon = TestClient(web["main"].app)
    assert anon.post("/api/admin/reset", json={"confirm": "RESET"}).status_code == 401

    _login(client)
    r = client.post("/api/admin/users", json={"username": "mgr", "password": "mgr12345", "role": "admin"})
    assert r.status_code == 201, r.text
    mc = TestClient(web["main"].app)
    assert mc.post("/api/auth/login", json={"username": "mgr", "password": "mgr12345"}).status_code == 200
    assert mc.post("/api/admin/reset", json={"confirm": "RESET"}).status_code == 403

    assert client.post("/api/admin/reset", json={"confirm": "yes"}).status_code == 400

    # 正确路径:替换重置本体(内存库不支持真重置)+隔离端点内审计用 SessionLocal,避免触碰真实库
    monkeypatch.setattr("app.admin.reset_db_to_factory", lambda: "nasTok.pre-reset-test.db")

    class _NoopSession:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.admin.SessionLocal", _NoopSession)
    r = client.post("/api/admin/reset", json={"confirm": "RESET"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["safety"] == "nasTok.pre-reset-test.db"


def test_backup_panel_structure():
    """前端:备份与重置独立 tab/面板,存储库工具栏不再内嵌备份按钮。"""
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert 'data-panel="backup"' in html
    assert 'id="panel-backup"' in html
    assert 'id="backup-download-btn"' in html
    assert 'id="backup-restore-input"' in html
    assert 'id="backup-reset-btn"' in html
    libs_zone = html.split('id="panel-libs"')[1].split('id="panel-backup"')[0]
    assert "backup" not in libs_zone
