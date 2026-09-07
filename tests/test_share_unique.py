"""单个媒体仅一条分享；停用后重分享恢复原记录并更新时间。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def share_env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    media = root / "clip.mp4"
    media.write_bytes(b"fake")
    monkeypatch.setenv("MEDIA_ROOT", str(root))

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.share as share

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_share_video_path ON share_links (video_path)")
        )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    importlib.reload(share)

    user = database.User(
        username="staff1",
        password_hash=database.User.hash_password("x"),
        role="admin",
        is_active=True,
    )
    db.add(user)
    lib = database.Library(name="默认库", path="", enabled=True)
    db.add(lib)
    db.flush()
    db.add(database.UserLibrary(user_id=user.id, library_id=lib.id))
    db.commit()
    db.refresh(user)

    class FakeURL:
        def __str__(self):
            return "http://test/"

    class FakeRequest:
        base_url = FakeURL()

    return {
        "db": db,
        "user": user,
        "share": share,
        "request": FakeRequest(),
        "path": "clip.mp4",
        "database": database,
        "engine": engine,
    }


def test_create_share_unique_and_restore(share_env):
    share = share_env["share"]
    db = share_env["db"]
    user = share_env["user"]
    request = share_env["request"]
    path = share_env["path"]
    database = share_env["database"]

    body = share.CreateShareRequest(path=path)
    first = share.create_share(body, request, db, user)
    token = first["token"]
    assert first["shared"] is True
    assert first["reused"] is False

    second = share.create_share(body, request, db, user)
    assert second["token"] == token
    assert second["reused"] is True
    assert len(db.scalars(select(database.ShareLink)).all()) == 1

    row = db.scalar(select(database.ShareLink).where(database.ShareLink.video_path == path))
    assert row is not None
    row.is_active = False
    old_time = datetime.utcnow() - timedelta(days=2)
    row.created_at = old_time
    db.commit()

    restored = share.create_share(body, request, db, user)
    assert restored["token"] != token
    assert restored["restored"] is True
    assert restored["rotated"] is True
    db.refresh(row)
    assert row.is_active is True
    assert row.created_at > old_time
    assert len(db.scalars(select(database.ShareLink)).all()) == 1


def test_revoke_share(share_env):
    share = share_env["share"]
    db = share_env["db"]
    user = share_env["user"]
    request = share_env["request"]
    path = share_env["path"]
    database = share_env["database"]

    body = share.CreateShareRequest(path=path)
    share.create_share(body, request, db, user)
    out = share.revoke_share(body, db, user)
    assert out["shared"] is False
    row = db.scalar(select(database.ShareLink).where(database.ShareLink.video_path == path))
    assert row is not None
    assert row.is_active is False


def test_dedupe_prefers_oldest_active(share_env):
    """旧有效 + 新停用：必须保留旧有效 token，并真正删掉重复行。"""
    share = share_env["share"]
    db = share_env["db"]
    user = share_env["user"]
    request = share_env["request"]
    path = share_env["path"]
    database = share_env["database"]

    # 绕过唯一约束直接插入历史脏数据：先删索引再插，再测应用层去重
    engine = share_env["engine"]
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_share_video_path"))

    old = database.ShareLink(
        token="token-old-active",
        video_path=path,
        title="old",
        created_by=user.id,
        is_active=True,
    )
    db.add(old)
    db.commit()
    db.refresh(old)

    newer = database.ShareLink(
        token="token-new-inactive",
        video_path=path,
        title="new",
        created_by=user.id,
        is_active=False,
    )
    db.add(newer)
    db.commit()

    body = share.CreateShareRequest(path=path)
    out = share.create_share(body, request, db, user)
    assert out["token"] == "token-old-active"
    assert out["reused"] is True

    rows = db.scalars(select(database.ShareLink).where(database.ShareLink.video_path == path)).all()
    assert len(rows) == 1
    assert rows[0].token == "token-old-active"
    assert rows[0].is_active is True


def test_reuse_commits_dedupe_deletes(share_env):
    """复用已启用分享时，去重删除必须落库（新会话可见）。"""
    share = share_env["share"]
    db = share_env["db"]
    user = share_env["user"]
    request = share_env["request"]
    path = share_env["path"]
    database = share_env["database"]
    engine = share_env["engine"]
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_share_video_path"))

    a = database.ShareLink(
        token="keep-me",
        video_path=path,
        title="a",
        created_by=user.id,
        is_active=True,
    )
    b = database.ShareLink(
        token="drop-me",
        video_path=path,
        title="b",
        created_by=user.id,
        is_active=True,
    )
    db.add_all([a, b])
    db.commit()

    body = share.CreateShareRequest(path=path)
    out = share.create_share(body, request, db, user)
    assert out["token"] == "keep-me"
    assert out["reused"] is True

    # 新会话验证已提交
    db2 = Session()
    try:
        rows = db2.scalars(select(database.ShareLink).where(database.ShareLink.video_path == path)).all()
        assert len(rows) == 1
        assert rows[0].token == "keep-me"
    finally:
        db2.close()
