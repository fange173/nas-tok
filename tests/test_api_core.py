"""API 级核心链路:安全响应头、登录限流、标签权限边界、改名级联。

与 test_frontend_routes.py 同一约定:fixture 内建独立内存库 + importlib.reload 重建 app 模块,
依赖注入替换 get_db,避免模块级单例污染。
"""
from __future__ import annotations

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
    import app.admin as admin
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
    # 与 init_db 一致:内置超管拥有全部存储库
    default_lib = db.query(database.Library).filter_by(name="默认库").one()
    db.add(database.UserLibrary(user_id=admin_user.id, library_id=default_lib.id))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    yield {
        "client": client,
        "root": root,
        "main": main_mod,
        "database": database,
        "auth": auth,
        "session": Session,
    }
    main_mod.app.dependency_overrides.clear()


def _login(client, username="admin", password="admin123") -> None:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def test_security_headers_present(web):
    r = web["client"].get("/api/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp, "全部 JS 已外置,CSP 必须收 script-src"
    assert "frame-ancestors 'none'" in csp


def test_login_rate_limit_locks_after_max_failures(web):
    client = web["client"]
    for _ in range(auth_max := 8):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
    # 超限后连正确密码也被 429
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 429
    assert auth_max


def test_tag_permission_boundaries(web):
    """无库用户读/写标记 403;管理员打标后,tagged/tag_id 过滤生效;外部 tag_id 400。"""
    client = web["client"]
    _login(client)

    # 建一个普通用户(默认无库)
    r = client.post("/api/admin/users", json={"username": "viewer", "password": "viewer123", "role": "user"})
    assert r.status_code == 201, r.text

    # 管理员建标记并打到 clip.mp4
    r = client.post("/api/tags", json={"name": "精选"})
    tag_id = r.json()["id"]
    r = client.put("/api/videos/tags", json={"path": "clip.mp4", "tag_ids": [tag_id]})
    assert r.status_code == 200, r.text

    # 外部/不存在的 tag_id → 400
    r = client.put("/api/videos/tags", json={"path": "clip.mp4", "tag_ids": [999999]})
    assert r.status_code == 400

    # tagged / tag_id 过滤可见
    r = client.get("/api/videos/list", params={"tagged": "true", "refresh": "true"})
    assert [it["path"] for it in r.json()["items"]] == ["clip.mp4"]
    r = client.get("/api/videos/list", params={"tag_id": tag_id, "refresh": "true"})
    assert r.json()["tag_name"] == "精选"

    # 普通用户:无库 → 读/写 403
    from fastapi.testclient import TestClient

    vc = TestClient(web["main"].app)
    _login(vc, "viewer", "viewer123")
    assert vc.get("/api/videos/tags", params={"path": "clip.mp4"}).status_code == 403
    assert vc.put("/api/videos/tags", json={"path": "clip.mp4", "tag_ids": []}).status_code == 403


def test_rename_cascades_favorite_tag_and_share(web):
    """改名后 favorites/video_tag_assignments/share_links 的 video_path 同步。"""
    client = web["client"]
    _login(client)

    client.post("/api/favorites", json={"path": "clip.mp4"})
    tag_id = client.post("/api/tags", json={"name": "稍后"}).json()["id"]
    client.put("/api/videos/tags", json={"path": "clip.mp4", "tag_ids": [tag_id]})
    r = client.post("/api/shares", json={"path": "clip.mp4"})
    assert r.status_code == 201, r.text

    r = client.put("/api/videos/edit", json={"old_path": "clip.mp4", "new_name": "clip_new.mp4"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "clip_new.mp4"
    assert (web["root"] / "clip_new.mp4").is_file()

    favs = client.get("/api/favorites").json()["items"]
    assert [f["path"] for f in favs] == ["clip_new.mp4"]

    tags = client.get("/api/videos/tags", params={"path": "clip_new.mp4"}).json()["items"]
    assert [t["id"] for t in tags if t["assigned"]] == [tag_id]

    # 分享仍有效且指向新路径
    shares = client.get("/api/admin/shares").json()["items"]
    assert shares and shares[0]["path"] == "clip_new.mp4"
    token = shares[0]["token"]
    r = client.get(f"/api/share/{token}")
    assert r.status_code == 200

    # 数据库层复核(防 API 表象)
    Session = web["session"]
    db = Session()
    try:
        from app.database import Favorite, ShareLink, VideoTagAssignment

        assert db.query(Favorite).one().video_path == "clip_new.mp4"
        assert db.query(VideoTagAssignment).one().video_path == "clip_new.mp4"
        assert db.query(ShareLink).one().video_path == "clip_new.mp4"
    finally:
        db.close()


def test_favorites_and_tag_filter_intersect(web):
    """喜欢页 + 标记筛选 = 交集（此前标记过滤会覆盖收藏过滤，显示全部带标视频）。"""
    client = web["client"]
    _login(client)

    (web["root"] / "b.mp4").write_bytes(b"fake-video-2")
    client.get("/api/videos/list", params={"refresh": "true"})

    # 只收藏 clip.mp4，但两个视频都打同一个标记
    client.post("/api/favorites", json={"path": "clip.mp4"})
    tag_id = client.post("/api/tags", json={"name": "测试"}).json()["id"]
    client.put("/api/videos/tags", json={"path": "clip.mp4", "tag_ids": [tag_id]})
    client.put("/api/videos/tags", json={"path": "b.mp4", "tag_ids": [tag_id]})

    r = client.get("/api/videos/list", params={"favorites": "true", "tag_id": tag_id, "refresh": "true"})
    assert r.status_code == 200
    assert [it["path"] for it in r.json()["items"]] == ["clip.mp4"]


def test_change_password_preserves_session_length(web):
    """未勾「记住登录」的 24h 会话，改密后刷新的 token 仍是短期会话，而非被延长成 10 年。"""
    client = web["client"]
    _login(client)  # 不带 remember

    import jwt as pyjwt

    def token_exp_remaining_hours():
        token = client.cookies.get("nastok_token")
        assert token, "改密后应刷新 cookie"
        data = pyjwt.decode(token, web["auth"].SECRET_KEY, algorithms=["HS256"])
        from datetime import datetime, timezone

        exp = datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc)
        return (exp - datetime.now(timezone.utc)).total_seconds() / 3600

    assert token_exp_remaining_hours() <= 25
    r = client.post(
        "/api/auth/change-password",
        json={"old_password": "admin123", "new_password": "new-pass-456"},
    )
    assert r.status_code == 200, r.text
    remaining = token_exp_remaining_hours()
    assert remaining <= 25, f"改密后 token 有效期应保持 ~24h，实际 {remaining:.0f}h"


def test_delete_user_cleans_favorites(web):
    """删除用户须一并清理其收藏（此前 favorites 成为孤儿数据永久残留）。"""
    client = web["client"]
    _login(client)

    r = client.post("/api/admin/users", json={"username": "favuser", "password": "favuser123", "role": "user"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    from fastapi.testclient import TestClient

    Session = web["session"]
    db = Session()
    try:
        from app.database import Library, UserLibrary

        lib = db.query(Library).filter_by(name="默认库").one()
        db.add(UserLibrary(user_id=uid, library_id=lib.id))
        db.commit()
    finally:
        db.close()

    uc = TestClient(web["main"].app)
    _login(uc, "favuser", "favuser123")
    assert uc.post("/api/favorites", json={"path": "clip.mp4"}).status_code == 200

    Session = web["session"]
    db = Session()
    try:
        from app.database import Favorite

        assert db.query(Favorite).filter_by(user_id=uid).count() == 1
        r = client.delete(f"/api/admin/users/{uid}")
        assert r.status_code == 200, r.text
        assert db.query(Favorite).filter_by(user_id=uid).count() == 0, "删除用户后收藏应被清理"
    finally:
        db.close()


def test_js_modules_served_no_cache(web):
    """JS 子模块无版本号，必须 no-cache 防止浏览器启发式缓存造成新旧模块混跑；
    同时保留 304 协商（继承 StaticFiles），回源校验不重复下载。"""
    client = web["client"]
    r = client.get("/static/js/main.js")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("Cache-Control", "")
    # ETag 协商命中 → 304（且同样带 no-cache）
    r2 = client.get("/static/js/main.js", headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304
    assert "no-cache" in r2.headers.get("Cache-Control", "")
    # 越界/不存在一律 404
    assert client.get("/static/js/nope.js").status_code == 404
    assert client.get("/static/js/..%2F..%2Fauth.py").status_code == 404


def test_pwa_routes_and_spa_links(web):
    """PWA 契约:manifest/sw.js 可访问且头正确;SPA 已挂 manifest/icon/SW 注册。"""
    client = web["client"]

    m = client.get("/static/manifest.webmanifest")
    assert m.status_code == 200
    assert m.headers["Content-Type"].startswith("application/manifest+json")
    body = m.json()
    assert body["display"] == "standalone"
    assert body["icons"], "manifest 缺 icons"
    assert body["start_url"] == "/"

    sw = client.get("/static/sw.js")
    assert sw.status_code == 200
    assert sw.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in sw.headers["Cache-Control"]
    assert "/api/" in sw.text  # SW 必须显式放行 API/流

    html = client.get("/").text
    assert '/static/manifest.webmanifest' in html
    assert '/static/icon.svg' in html
    # SW 注册逻辑在入口模块内(内联脚本已随模块化拆分移除)
    js = client.get("/static/js/main.js")
    assert js.status_code == 200
    assert "/static/sw.js" in js.text
