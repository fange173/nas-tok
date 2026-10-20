"""2026-09-07 审核后续：空标记筛选、搜索、JWT 吊销、分享 ACL、删除用户清分享。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
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
    (root / "other.mkv").write_bytes(b"fake-mkv")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-length-32")

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.auth as auth
    import app.admin as admin
    import app.share as share
    import app.main as main_mod

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # 与生产引擎一致强制外键（database.py connect 监听器），
    # 否则父子表删除顺序类缺陷在测试里永远无法复现
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    database.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    importlib.reload(auth)
    importlib.reload(admin)
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
    db.add(database.Library(name="默认库", path="", enabled=True))
    db.flush()
    default_lib = db.query(database.Library).filter_by(name="默认库").one()
    db.add(database.UserLibrary(user_id=admin_user.id, library_id=default_lib.id))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    yield {
        "client": client,
        "session": Session,
        "root": root,
        "main": main_mod,
        "database": database,
        "auth": auth,
        "tmp_path": tmp_path,
        "monkeypatch": monkeypatch,
    }
    main_mod.app.dependency_overrides.clear()


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_empty_tag_filter_returns_nothing(web):
    client = web["client"]
    _login(client)
    tag_id = client.post("/api/tags", json={"name": "空标记"}).json()["id"]
    r = client.get("/api/videos/list", params={"tag_id": tag_id, "refresh": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("tagged_only") is True
    assert body["items"] == []


def test_search_filters_by_title(web):
    client = web["client"]
    _login(client)
    r = client.get("/api/videos/list", params={"q": "clip", "refresh": "true"})
    assert r.status_code == 200
    paths = [it["path"] for it in r.json()["items"]]
    assert "clip.mp4" in paths
    r = client.get("/api/videos/list", params={"q": "no-such-file"})
    assert r.json()["items"] == []


def test_mkv_marked_unplayable(web):
    client = web["client"]
    _login(client)
    r = client.get("/api/videos/list", params={"refresh": "true", "limit": 50})
    items = {it["path"]: it for it in r.json()["items"]}
    assert items["clip.mp4"]["playable"] is True
    assert items["other.mkv"]["playable"] is False


def test_password_change_invalidates_other_session(web):
    client = web["client"]
    other = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(web["main"].app)
    _login(client)
    _login(other)
    r = client.post(
        "/api/auth/change-password",
        json={"old_password": "admin123", "new_password": "new-pass-456"},
    )
    assert r.status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me").status_code == 200


def test_share_requires_staff(web):
    client = web["client"]
    _login(client)
    r = client.post("/api/admin/users", json={"username": "bob", "password": "bob12345", "role": "user"})
    uid = r.json()["id"]
    from fastapi.testclient import TestClient

    uc = TestClient(web["main"].app)
    _login(uc, "bob", "bob12345")
    denied = uc.post("/api/shares", json={"path": "clip.mp4"})
    assert denied.status_code == 403

    Session = web["session"]
    db = Session()
    try:
        lib = db.query(web["database"].Library).filter_by(name="默认库").one()
        db.add(web["database"].UserLibrary(user_id=uid, library_id=lib.id))
        db.commit()
    finally:
        db.close()
    from app.video_handler import invalidate_media_access_cache

    invalidate_media_access_cache(user_id=uid)
    still = uc.post("/api/shares", json={"path": "clip.mp4"})
    assert still.status_code == 403
    ok = client.post("/api/shares", json={"path": "clip.mp4"})
    assert ok.status_code == 201, ok.text
    token = ok.json()["token"]
    pub = TestClient(web["main"].app)
    got = pub.get(f"/api/share/{token}")
    assert got.status_code == 200
    assert "path" not in got.json()


def test_admin_delete_share_with_views(web):
    """回归：删除带访问记录的分享曾因 share_views 外键约束 500。"""
    client = web["client"]
    _login(client)
    ok = client.post("/api/shares", json={"path": "clip.mp4"})
    assert ok.status_code == 201, ok.text
    token = ok.json()["token"]
    share_id = ok.json().get("id")
    # 公开访问一次，产生 ShareView 记录
    from fastapi.testclient import TestClient

    pub = TestClient(web["main"].app)
    assert pub.get(f"/api/share/{token}").status_code == 200
    if share_id is None:
        listed = client.get("/api/admin/shares").json()["items"]
        share_id = next(s["id"] for s in listed if s["token"] == token)

    r = client.delete(f"/api/admin/shares/{share_id}")
    assert r.status_code == 200, r.text
    assert pub.get(f"/api/share/{token}").status_code == 404
    # 访问明细一并清理
    Session = web["session"]
    db = Session()
    try:
        assert db.query(web["database"].ShareView).filter_by(share_id=share_id).count() == 0
    finally:
        db.close()


def test_delete_user_revokes_shares(web):
    client = web["client"]
    _login(client)
    r = client.post("/api/admin/users", json={"username": "sharer", "password": "sharer12", "role": "user"})
    uid = r.json()["id"]
    Session = web["session"]
    db = Session()
    try:
        lib = db.query(web["database"].Library).filter_by(name="默认库").one()
        db.add(web["database"].UserLibrary(user_id=uid, library_id=lib.id))
        db.add(web["database"].ShareLink(
            token="user-share-token-1",
            video_path="clip.mp4",
            title="clip",
            created_by=uid,
            is_active=True,
        ))
        db.commit()
    finally:
        db.close()
    from fastapi.testclient import TestClient

    assert client.delete(f"/api/admin/users/{uid}").status_code == 200
    assert TestClient(web["main"].app).get("/api/share/user-share-token-1").status_code == 404


def test_health_reports_db_and_media(web):
    r = web["client"].get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["db"] is True
    assert body["media"] is True


def test_my_libraries_endpoint(web):
    client = web["client"]
    _login(client)
    r = client.get("/api/libraries")
    assert r.status_code == 200
    names = [it["name"] for it in r.json()["items"]]
    assert "默认库" in names


def test_gbk_subtitle_served_and_flagged(web):
    """GBK 编码 srt 不再 500;列表按页标注 has_subtitle。"""
    client = web["client"]
    root = web["root"]
    (root / "clip.srt").write_bytes("1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("gbk"))
    _login(client)
    r = client.get("/api/videos/subtitles/clip.mp4")
    assert r.status_code == 200, r.text
    assert r.text.startswith("WEBVTT") and "你好" in r.text and "00:00:01.000" in r.text
    items = client.get("/api/videos/list", params={"sort": "newest", "refresh": "true"}).json()["items"]
    flags = {it["path"]: it.get("has_subtitle") for it in items}
    assert flags["clip.mp4"] is True
    assert flags["other.mkv"] is False


def test_share_password_bruteforce_locked(web):
    client = web["client"]
    _login(client)
    r = client.post("/api/shares", json={"path": "clip.mp4", "password": "right-pass"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]

    from fastapi.testclient import TestClient

    anon = TestClient(web["main"].app)
    import app.share as share

    for _ in range(share.SHARE_AUTH_MAX_FAILURES):
        assert anon.post(f"/api/share/{token}/auth", json={"password": "nope"}).status_code == 401
    # 达到上限后连正确密码也 429
    assert anon.post(f"/api/share/{token}/auth", json={"password": "right-pass"}).status_code == 429
    share._share_auth_failures.clear()
    r = anon.post(f"/api/share/{token}/auth", json={"password": "right-pass"})
    assert r.status_code == 200 and r.json()["stream_url"]


def test_settings_size_capped(web):
    client = web["client"]
    _login(client)
    assert client.put("/api/auth/settings", json={"settings": {"rate": 1.5}}).status_code == 200
    r = client.put("/api/auth/settings", json={"settings": {"blob": "x" * 9000}})
    assert r.status_code == 413
    assert client.get("/api/auth/settings").json()["settings"] == {"rate": 1.5}


def test_share_hidden_when_library_disabled(web):
    client = web["client"]
    _login(client)
    token = client.post("/api/shares", json={"path": "clip.mp4"}).json()["token"]
    from fastapi.testclient import TestClient

    anon = TestClient(web["main"].app)
    assert anon.get(f"/api/share/{token}").status_code == 200
    lib_id = client.get("/api/admin/libraries").json()["items"][0]["id"]
    assert client.put(f"/api/admin/libraries/{lib_id}", json={"enabled": False}).status_code == 200
    assert anon.get(f"/api/share/{token}").status_code == 404
    assert anon.get(f"/api/share/{token}/stream").status_code == 404
    client.put(f"/api/admin/libraries/{lib_id}", json={"enabled": True})
    assert anon.get(f"/api/share/{token}").status_code == 200


def test_album_cover_in_list(web):
    import os
    import time

    root = web["root"]
    album = root / "trip"
    (album / "sub").mkdir(parents=True)
    (album / "b.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (album / "sub" / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    old = time.time() - 3600
    os.utime(album / "sub" / "a.jpg", (old, old))
    client = web["client"]
    _login(client)
    # 默认库只扫根目录;为相册建子目录库
    r = client.post("/api/admin/libraries", json={"name": "Trip", "path": "trip"})
    assert r.status_code == 201, r.text
    items = client.get("/api/videos/list", params={"media_mode": "images", "sort": "newest", "refresh": "true"}).json()["items"]
    albums = [it for it in items if it["kind"] == "album"]
    assert albums and albums[0]["cover"] == "trip/sub/a.jpg"
