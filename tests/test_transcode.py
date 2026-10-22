"""格式转换:探测、转封装、完整转码、权限与缓存淘汰(需要本机 ffmpeg,缺失时跳过)。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="需要 ffmpeg")


def _make(path: Path, *codec_args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         *codec_args, str(path)],
        check=True,
    )


def _streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, check=True,
    ).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


@pytest.fixture()
def web(tmp_path, monkeypatch):
    root = tmp_path / "media"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    _make(root / "a" / "hevc_ac3.mkv", "-c:v", "libx265", "-tag:v", "hev1", "-pix_fmt", "yuv420p",
          "-x265-params", "log-level=error", "-c:a", "ac3")
    _make(root / "a" / "xvid.avi", "-c:v", "mpeg4", "-c:a", "libmp3lame")
    _make(root / "b" / "secret.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-length-32")

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.auth as auth
    import app.admin as admin
    import app.share as share
    import app.transcode as transcode
    import app.main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    importlib.reload(auth)
    importlib.reload(admin)
    importlib.reload(share)
    importlib.reload(transcode)
    monkeypatch.setattr(transcode, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(transcode, "HWACCEL", "none")
    importlib.reload(main_mod)
    main_mod.transcode = transcode

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    main_mod.app.dependency_overrides[database.get_db] = override_get_db
    db = Session()
    viewer = database.User(username="viewer", password_hash=database.User.hash_password("viewer123"), role="user", is_active=True)
    lib_a = database.Library(name="A", path="a", enabled=True)
    lib_b = database.Library(name="B", path="b", enabled=True)
    db.add_all([viewer, lib_a, lib_b])
    db.flush()
    db.add(database.UserLibrary(user_id=viewer.id, library_id=lib_a.id))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    assert client.post("/api/auth/login", json={"username": "viewer", "password": "viewer123"}).status_code == 200
    yield {"client": client, "transcode": transcode, "root": root, "tmp": tmp_path}
    main_mod.app.dependency_overrides.clear()
    vh.invalidate_media_access_cache()


def _wait_done(client, path: str, mode: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/api/videos/prepare", params={"path": path, "mode": mode}).json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.2)
    raise AssertionError("转换超时")


def test_playinfo_reports_codecs(web):
    r = web["client"].get("/api/videos/playinfo", params={"path": "a/hevc_ac3.mkv"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ffmpeg"] is True
    assert d["probe"]["container"] == "matroska"
    assert d["probe"]["video"]["codec"] == "hevc" and d["probe"]["video"]["bit_depth"] == 8
    assert d["probe"]["audio"]["codec"] == "ac3"
    assert d["remux"]["status"] == "none"


def test_remux_fixes_tag_and_audio(web):
    c = web["client"]
    assert c.get("/api/videos/converted/a/hevc_ac3.mkv", params={"mode": "remux"}).status_code == 404
    r = c.post("/api/videos/prepare", json={"path": "a/hevc_ac3.mkv", "mode": "remux"})
    assert r.status_code == 200, r.text
    assert _wait_done(c, "a/hevc_ac3.mkv", "remux")["status"] == "done"
    r = c.get("/api/videos/converted/a/hevc_ac3.mkv", params={"mode": "remux"}, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.headers["content-type"].startswith("video/mp4")
    out = next((web["tmp"] / "cache").glob("*.mp4"))
    s = _streams(out)
    assert s["video"]["codec_name"] == "hevc" and s["video"]["codec_tag_string"] == "hvc1"
    assert s["audio"]["codec_name"] == "aac"


def test_transcode_to_h264(web):
    c = web["client"]
    c.post("/api/videos/prepare", json={"path": "a/xvid.avi", "mode": "transcode"})
    assert _wait_done(c, "a/xvid.avi", "transcode")["status"] == "done"
    r = c.get("/api/videos/converted/a/xvid.avi", params={"mode": "transcode"})
    assert r.status_code == 200 and len(r.content) > 0
    out = next((web["tmp"] / "cache").glob("*.mp4"))
    s = _streams(out)
    assert s["video"]["codec_name"] == "h264" and s["video"]["pix_fmt"] == "yuv420p"


def test_acl_enforced(web):
    c = web["client"]
    assert c.get("/api/videos/playinfo", params={"path": "b/secret.mkv"}).status_code == 403
    assert c.post("/api/videos/prepare", json={"path": "b/secret.mkv", "mode": "remux"}).status_code == 403
    assert c.post("/api/videos/prepare", json={"path": "a/../b/secret.mkv", "mode": "remux"}).status_code == 403
    assert c.get("/api/videos/converted/b/secret.mkv", params={"mode": "remux"}).status_code == 403
    assert c.post("/api/videos/prepare", json={"path": "a/xvid.avi", "mode": "bogus"}).status_code == 422


def test_cache_eviction_keeps_newest(web, monkeypatch):
    c = web["client"]
    tc = web["transcode"]
    monkeypatch.setattr(tc, "CACHE_MAX_BYTES", 1)
    c.post("/api/videos/prepare", json={"path": "a/hevc_ac3.mkv", "mode": "remux"})
    _wait_done(c, "a/hevc_ac3.mkv", "remux")
    c.post("/api/videos/prepare", json={"path": "a/xvid.avi", "mode": "transcode"})
    _wait_done(c, "a/xvid.avi", "transcode")
    files = list((web["tmp"] / "cache").glob("*.mp4"))
    assert len(files) == 1, "超过上限时应淘汰旧文件,只保留刚生成的"
    assert c.get("/api/videos/converted/a/xvid.avi", params={"mode": "transcode"}).status_code == 200


def test_queue_cap(web, monkeypatch):
    tc = web["transcode"]
    monkeypatch.setattr(tc, "MAX_PENDING", 0)
    r = web["client"].post("/api/videos/prepare", json={"path": "a/xvid.avi", "mode": "transcode"})
    assert r.status_code == 429
