"""前端依赖的后端契约：路由完整性、refresh 端点、合集图片按需加载、docs 默认关闭。

test_frontend_api_urls_resolve 可防止 bug-017（refresh 装饰器丢失）类回归。

与其他测试文件一致：fixture 内用独立内存库 + importlib.reload 重建 app 模块，
避免模块级单例（engine/MEDIA_ROOT）跨文件污染。
"""
from __future__ import annotations

import re
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
    (root / "clip.mp4").write_bytes(b"fake")
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-with-enough-length-32")
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

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

    # 建内置超管 + 默认库（与 init_db 等价的最小集合）
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
    db.commit()
    db.close()

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    yield {
        "client": client,
        "root": root,
        "main": main_mod,
        "database": database,
        "vh": vh,
    }
    main_mod.app.dependency_overrides.clear()


def _login(client) -> None:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text


def test_refresh_endpoint_registered_and_ok(web):
    """bug-017 回归：refresh_videos 必须挂到 /api/videos/refresh。"""
    client = web["client"]
    _login(client)
    r = client.post("/api/videos/refresh")
    assert r.status_code == 200, f"refresh 端点缺失或报错: {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert "total" in body


def test_frontend_api_urls_resolve(web):
    """index.html 与 js/*.js 中出现的所有 /api/ URL 都必须能匹配到已注册路由。"""
    static = ROOT / "app" / "static"
    sources = [static / "index.html", *sorted((static / "js").glob("*.js"))]
    raw = set()
    for f in sources:
        raw |= set(re.findall(r'["`\'](/api/[^"`\'\s]+?)["`\']', f.read_text(encoding="utf-8")))
    assert raw, "未从 HTML 提取到任何 API URL"

    routes = [r.path for r in web["main"].app.routes if hasattr(r, "methods")]

    def resolves(url: str) -> bool:
        url = url.split("?")[0]
        if "${" in url:
            prefix = url.split("${", 1)[0]
            return any(rt.startswith(prefix.rstrip("/")) or rt.startswith(prefix) for rt in routes)
        for rt in routes:
            if rt == url:
                return True
            if "{" in rt:
                base = rt.split("{", 1)[0].rstrip("/")
                if base and (url == base or url.startswith(base + "/")):
                    return True
        return False

    missing = sorted(u for u in raw if not resolves(u))
    assert not missing, f"前端调用的以下 API 在后端不存在: {missing}"


def test_album_images_on_demand(web):
    """列表接口不再内联合集 images；GET /api/albums/images 按需返回。"""
    client = web["client"]
    root = web["root"]
    _login(client)
    # 子目录库 photos/ 下的一级子目录按产品语义聚合为"合集"
    (root / "photos" / "trip").mkdir(parents=True)
    (root / "photos" / "trip" / "a.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "photos" / "trip" / "b.jpg").write_bytes(b"\xff\xd8\xff")

    # 直接登记存储库（用 dependency override 同款连接，保证与请求同事务可见）
    import app.database as database

    override = web["main"].app.dependency_overrides[database.get_db]
    gen = override()
    session = next(gen)
    lib = database.Library(name="照片", path="photos", enabled=True)
    session.add(lib)
    session.commit()
    session.refresh(lib)
    # 超管授权该库
    admin_user = session.query(database.User).filter_by(username="admin").one()
    session.add(database.UserLibrary(user_id=admin_user.id, library_id=lib.id))
    session.commit()

    r = client.get(
        "/api/videos/list",
        params={"media_mode": "images", "limit": 50, "refresh": "true"},
    )
    assert r.status_code == 200, r.text
    albums = [it for it in r.json()["items"] if it.get("kind") == "album"]
    assert albums, "应扫描到 photos/trip 合集"
    album = albums[0]
    assert album["count"] == 2
    assert "images" not in album, "合集 images 不应内联进列表响应"

    r2 = client.get("/api/albums/images", params={"path": album["path"]})
    assert r2.status_code == 200, r2.text
    imgs = r2.json()["images"]
    assert len(imgs) == 2
    assert {im["path"] for im in imgs} == {"photos/trip/a.jpg", "photos/trip/b.jpg"}


def test_docs_disabled_by_default(web):
    client = web["client"]
    r = client.get("/docs")
    assert r.status_code == 404
    r = client.get("/openapi.json")
    assert r.status_code == 404


def test_spa_html_injects_css_version(web):
    """SPA 返回的 HTML 应把 style.css?v= / js/main.js?v= 替换为 mtime 数字。"""
    r = web["client"].get("/")
    assert r.status_code == 200
    m = re.search(r'style\.css\?v=(\d+)', r.text)
    assert m, "未注入 CSS 版本号"
    m2 = re.search(r'/static/js/main\.js\?v=(\d+)', r.text)
    assert m2, "未注入入口 JS 版本号"


def test_spa_uses_external_modules_only(web):
    """SPA 只加载外链 ES module(不留内联脚本)—— CSP script-src 'self' 的底线。"""
    r = web["client"].get("/")
    assert r.status_code == 200
    assert 'type="module" src="/static/js/main.js' in r.text
    assert "<script>" not in r.text.lower(), "仍存在内联 <script> 块"
    for mod in ("state", "util", "icons", "feed", "player", "admin", "auth", "tags", "search", "share-page"):
        assert mod in "".join(sorted(p.name for p in (ROOT / "app" / "static" / "js").glob("*.js")))
