"""Range/206 流响应:200 全量、206 分段、416 越界、非法头忽略(单一解析器)、If-Range。

对象级用例直接调 stream_video_file;端点级用例(TestClient)验证 ASGI 管线后的真实线上行为
—— 曾有回归:非法 Range 回落 FileResponse 被 starlette 二次解析为 400/416/多段 bug(评审 H1)。
"""
from __future__ import annotations

import asyncio
import urllib.parse
from email.utils import formatdate
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTENT = bytes(range(256)) * 10  # 2560 字节,确定性内容


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    import importlib
    import app.video_handler as vh

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    (root / "samples").mkdir()
    (root / "samples" / "a.mp4").write_bytes(CONTENT)
    return root, vh


def _stream(vh, range_header=None, download=False, if_range=None):
    encoded = urllib.parse.quote("samples/a.mp4", safe="")
    return vh.stream_video_file(encoded, download=download, range_header=range_header, if_range=if_range)


def _body(resp) -> bytes:
    async def _collect():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    return asyncio.run(_collect())


# ---------- 对象级 ----------

def test_no_range_returns_200_full(media_root):
    """无 Range → FileResponse 200 全量(FileResponse 由 ASGI 层读文件,此处只验分支)。"""
    _, vh = media_root
    resp = _stream(vh)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Last-Modified"]


def test_range_start_end_returns_206(media_root):
    _, vh = media_root
    resp = _stream(vh, "bytes=0-99")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 0-99/{len(CONTENT)}"
    assert resp.headers["Content-Length"] == "100"
    assert resp.headers["Last-Modified"]
    assert _body(resp) == CONTENT[:100]


def test_range_open_end(media_root):
    _, vh = media_root
    resp = _stream(vh, "bytes=2000-")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 2000-2559/{len(CONTENT)}"
    assert _body(resp) == CONTENT[2000:]


def test_range_suffix(media_root):
    _, vh = media_root
    resp = _stream(vh, "bytes=-50")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 2510-2559/{len(CONTENT)}"
    assert _body(resp) == CONTENT[-50:]


def test_range_end_beyond_size_is_clamped(media_root):
    _, vh = media_root
    resp = _stream(vh, "bytes=2550-99999")
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == f"bytes 2550-2559/{len(CONTENT)}"
    assert _body(resp) == CONTENT[2550:]


def test_range_start_beyond_size_returns_416(media_root):
    _, vh = media_root
    with pytest.raises(HTTPException) as ei:
        _stream(vh, "bytes=3000-4000")
    assert ei.value.status_code == 416
    assert ei.value.headers["Content-Range"] == f"bytes */{len(CONTENT)}"


def test_invalid_range_headers_are_ignored_and_streamed_200(media_root):
    """非法/多段/未知单位 Range → 忽略,由本模块流式回 200 全量(不回落 FileResponse)。"""
    _, vh = media_root
    for bad in ("bytes=", "bytes=50-10", "bytes=a-b", "bytes=-0", "bytes=0-10,20-30", "items=0-10"):
        resp = _stream(vh, bad)
        assert isinstance(resp, StreamingResponse), bad
        assert resp.status_code == 200, bad
        assert resp.headers["Content-Length"] == str(len(CONTENT))
        assert "Content-Range" not in resp.headers
        assert _body(resp) == CONTENT, bad


def test_download_returns_fileresponse_starlette_handles_range(media_root):
    """下载分支:由 starlette 处理 Range(断点续传),本模块只负责 attachment 语义。"""
    _, vh = media_root
    resp = _stream(vh, "bytes=0-99", download=True)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("Content-Disposition", "")


def test_if_range_matching_mtime_allows_206(media_root):
    root, vh = media_root
    mtime_http = formatdate((root / "samples" / "a.mp4").stat().st_mtime, usegmt=True)
    resp = _stream(vh, "bytes=0-9", if_range=mtime_http)
    assert resp.status_code == 206
    assert _body(resp) == CONTENT[:10]


def test_if_range_stale_falls_back_to_200_full(media_root):
    _, vh = media_root
    resp = _stream(vh, "bytes=0-9", if_range="Wed, 01 Jan 2020 00:00:00 GMT")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
    assert _body(resp) == CONTENT


def test_if_range_ignored_without_range_header(media_root):
    _, vh = media_root
    resp = _stream(vh, if_range="Wed, 01 Jan 2020 00:00:00 GMT")
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200


def test_range_serves_across_chunk_boundary(media_root):
    """大于单块(512KB)的范围请求也能完整返回。"""
    root, vh = media_root
    big = bytes(i % 256 for i in range(vh._STREAM_CHUNK + 12345))
    (root / "samples" / "big.bin").write_bytes(big)
    encoded = urllib.parse.quote("samples/big.bin", safe="")
    resp = vh.stream_video_file(encoded, range_header="bytes=100-")
    assert resp.status_code == 206
    assert _body(resp) == big[100:]


def test_zero_byte_file_with_range(media_root):
    root, vh = media_root
    (root / "samples" / "empty.mp4").write_bytes(b"")
    encoded = urllib.parse.quote("samples/empty.mp4", safe="")
    resp = vh.stream_video_file(encoded, range_header="bytes=0-10")
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == "0"
    assert _body(resp) == b""


# ---------- 端点级(ASGI 管线后的真实行为) ----------

@pytest.fixture()
def web(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(CONTENT)
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
    default_lib = db.query(database.Library).filter_by(name="默认库").one()
    db.add(database.UserLibrary(user_id=admin_user.id, library_id=default_lib.id))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    yield {"client": client, "app": main_mod.app}
    main_mod.app.dependency_overrides.clear()


def _login(client) -> None:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text


def test_endpoint_range_206_and_security_headers(web):
    client = web["client"]
    assert client.get("/api/videos/stream/clip.mp4").status_code == 401
    _login(client)
    r = client.get("/api/videos/stream/clip.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 0-99/{len(CONTENT)}"
    assert r.content == CONTENT[:100]
    # 安全头中间件对流式响应同样生效
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")


def test_endpoint_invalid_range_final_status_is_200_full(web):
    """H1 回归:非法/多段 Range 经完整 ASGI 后必须是 200 全量,而不是 400/416/multipart。"""
    client = web["client"]
    _login(client)
    for bad in ("bytes=a-b", "bytes=50-10", "bytes=", "bytes=-0", "bytes=0-10,20-30", "items=0-10"):
        r = client.get("/api/videos/stream/clip.mp4", headers={"Range": bad})
        assert r.status_code == 200, bad
        assert r.content == CONTENT, bad
        assert "Content-Range" not in r.headers, bad


def test_endpoint_if_range_stale_returns_200(web):
    client = web["client"]
    _login(client)
    r0 = client.get("/api/videos/stream/clip.mp4")
    last_modified = r0.headers["Last-Modified"]
    r = client.get("/api/videos/stream/clip.mp4", headers={"Range": "bytes=0-9", "If-Range": last_modified})
    assert r.status_code == 206
    r2 = client.get("/api/videos/stream/clip.mp4", headers={"Range": "bytes=0-9", "If-Range": "Wed, 01 Jan 2020 00:00:00 GMT"})
    assert r2.status_code == 200
    assert r2.content == CONTENT


def test_endpoint_share_stream_also_supports_range(web):
    """分享流与登录流同一路径修复:206 + 非法头 200。"""
    client = web["client"]
    _login(client)
    created = client.post("/api/shares", json={"path": "clip.mp4"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    # 分享流免登录
    r = client.get(f"/api/share/{token}/stream", headers={"Range": "bytes=0-49"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 0-49/{len(CONTENT)}"
    r2 = client.get(f"/api/share/{token}/stream", headers={"Range": "bytes=a-b"})
    assert r2.status_code == 200
    assert r2.content == CONTENT


def test_endpoint_416_out_of_range(web):
    """端点级 416:Range 起始越界经完整 ASGI 管线后返回 416 + Content-Range */size。"""
    client = web["client"]
    _login(client)
    r = client.get("/api/videos/stream/clip.mp4", headers={"Range": "bytes=3000-4000"})
    assert r.status_code == 416
    assert r.headers["Content-Range"] == f"bytes */{len(CONTENT)}"


def test_share_stream_truly_anonymous(web):
    """分享流在无登录 cookie 的全新客户端上仍可 206(证明免鉴权路径,非登录态残留)。

    补 R3 Info-1 测试缺口:原 test_endpoint_share_stream_also_supports_range 在已登录
    TestClient 上测分享流,无法证明未来误加鉴权时会被测试捕获。此处用全新无 cookie
    客户端复验;同时确认匿名分享页不再泄露内部 path 字段(R3-L2)。
    """
    client = web["client"]
    _login(client)
    created = client.post("/api/shares", json={"path": "clip.mp4"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    # 全新客户端,无任何 cookie,证明分享流与分享页免鉴权
    from fastapi.testclient import TestClient

    anon = TestClient(web["app"])
    r = anon.get(f"/api/share/{token}/stream", headers={"Range": "bytes=0-49"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 0-49/{len(CONTENT)}"
    assert r.content == CONTENT[:50]
    # 匿名分享页不应再泄露内部 path 字段(R3-L2)
    page = anon.get(f"/api/share/{token}")
    assert page.status_code == 200
    assert "path" not in page.json()
