"""存储库 ACL 与路径穿越:鉴权所用路径必须与真正读盘的路径一致。

曾有漏洞:ACL 只做字符串前缀匹配(`a/../b/x` 以 `a/` 开头 → 放行),
而读盘时 resolve 掉 `..` 落到 b 库;且拉流链路多次 unquote,`%2E%2E` 在 ACL 之后才被解码。
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
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "pub.mp4").write_bytes(b"public")
    (root / "b" / "secret.mp4").write_bytes(b"secret")
    (root / "b" / "secret.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
    # 文件名本身含 %XX:旧实现多次 unquote 会把 %41 解成 A 导致 404
    (root / "a" / "100%41.mp4").write_bytes(b"percent")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-length-32")

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.auth as auth
    import app.admin as admin
    import app.share as share
    import app.main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
    viewer = database.User(
        username="viewer",
        password_hash=database.User.hash_password("viewer123"),
        role="user",
        is_active=True,
    )
    lib_a = database.Library(name="A", path="a", enabled=True)
    lib_b = database.Library(name="B", path="b", enabled=True)
    db.add_all([viewer, lib_a, lib_b])
    db.flush()
    db.add(database.UserLibrary(user_id=viewer.id, library_id=lib_a.id))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    r = client.post("/api/auth/login", json={"username": "viewer", "password": "viewer123"})
    assert r.status_code == 200, r.text
    yield {"client": client, "vh": vh, "session": Session}
    main_mod.app.dependency_overrides.clear()
    vh.invalidate_media_access_cache()


def test_acl_rejects_dotdot_segments(web):
    vh = web["vh"]
    db = web["session"]()
    try:
        uid = 1
        assert vh.user_can_access_media_path(db, uid, "a/pub.mp4") is True
        assert vh.user_can_access_media_path(db, uid, "b/secret.mp4") is False
        assert vh.user_can_access_media_path(db, uid, "a/../b/secret.mp4") is False
        assert vh.user_can_access_media_path(db, uid, "a/./../b/secret.mp4") is False
    finally:
        db.close()


@pytest.mark.parametrize(
    "url",
    [
        "/api/videos/stream/a/%2E%2E/b/secret.mp4",
        "/api/videos/stream/a%2F..%2Fb%2Fsecret.mp4",
        # 二次编码:旧实现在 ACL 之后又 unquote 一次
        "/api/videos/stream/a/%252E%252E/b/secret.mp4",
        "/api/videos/stream/a/%252E%252E%252Fb%252Fsecret.mp4",
    ],
)
def test_stream_cannot_escape_assigned_library(web, url):
    r = web["client"].get(url)
    assert r.status_code in (400, 403, 404), (url, r.status_code)
    assert b"secret" not in r.content


def test_subtitles_cannot_escape_assigned_library(web):
    c = web["client"]
    for url in (
        "/api/videos/subtitles/a/%2E%2E/b/secret.mp4",
        "/api/videos/subtitles/a/%252E%252E/b/secret.mp4",
    ):
        r = c.get(url)
        assert r.status_code in (400, 403, 404), (url, r.status_code)


def test_json_endpoints_reject_traversal(web):
    c = web["client"]
    assert c.post("/api/favorites", json={"path": "a/../b/secret.mp4"}).status_code == 403
    assert c.get("/api/albums/images", params={"path": "a/../b"}).status_code == 403


def test_stream_allowed_and_percent_filename(web):
    r = web["client"].get("/api/videos/stream/a/pub.mp4")
    assert r.status_code == 200 and r.content == b"public"
    # 前端按段 encodeURIComponent:100%41.mp4 → 100%2541.mp4,uvicorn 解码一次得到 100%41.mp4。
    # starlette TestClient 会对 path 多解码一次(与 uvicorn 不同),故此处直接验证解码后的字符串
    vh = web["vh"]
    db = web["session"]()
    try:
        assert vh.user_can_access_media_path(db, 1, "a/100%41.mp4") is True
    finally:
        db.close()
    resp = vh.stream_video_file("a/100%41.mp4")
    assert resp.status_code == 200
    assert Path(resp.path).read_bytes() == b"percent"


def test_symlink_into_other_library_denied(web, tmp_path):
    """库 a 内指向库 b 的符号链接:按真实路径归属拒绝。"""
    vh = web["vh"]
    root = vh.MEDIA_ROOT
    (root / "a" / "link.mp4").symlink_to(root / "b" / "secret.mp4")
    db = web["session"]()
    try:
        assert vh.user_can_access_media_path(db, 1, "a/link.mp4") is False
    finally:
        db.close()
    assert web["client"].get("/api/videos/stream/a/link.mp4").status_code == 403
