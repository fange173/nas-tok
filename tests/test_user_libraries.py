"""按用户分配存储库：默认无库、权限边界、列表过滤。"""
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
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "x.mp4").write_bytes(b"fake")
    (root / "b" / "y.mp4").write_bytes(b"fake")
    monkeypatch.setenv("MEDIA_ROOT", str(root))

    import importlib
    import app.database as database
    import app.video_handler as vh
    import app.admin as admin
    import app.auth as auth

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    importlib.reload(admin)

    sysadmin = database.User(
        username="admin",
        password_hash=database.User.hash_password("admin123"),
        role="sysadmin",
        is_active=True,
    )
    mgr = database.User(
        username="mgr",
        password_hash=database.User.hash_password("x"),
        role="admin",
        is_active=True,
    )
    user = database.User(
        username="u1",
        password_hash=database.User.hash_password("x"),
        role="user",
        is_active=True,
    )
    lib_a = database.Library(name="A", path="a", enabled=True)
    lib_b = database.Library(name="B", path="b", enabled=True)
    db.add_all([sysadmin, mgr, user, lib_a, lib_b])
    db.commit()
    for u in (sysadmin, mgr, user):
        db.refresh(u)
    for lib in (lib_a, lib_b):
        db.refresh(lib)

    return {
        "db": db,
        "admin_mod": admin,
        "auth": auth,
        "vh": vh,
        "database": database,
        "sysadmin": sysadmin,
        "mgr": mgr,
        "user": user,
        "lib_a": lib_a,
        "lib_b": lib_b,
    }


def test_new_user_has_no_libraries(env):
    admin = env["admin_mod"]
    db = env["db"]
    body = admin.CreateUserRequest(username="newbie", password="secret1", role="user")
    out = admin.create_user(body, db, env["sysadmin"])
    assert out["library_ids"] == []
    assert out["libraries"] == []


def test_assign_permission_matrix(env):
    admin = env["admin_mod"]
    assert admin._can_assign_libraries(env["sysadmin"], env["user"]) is True
    assert admin._can_assign_libraries(env["sysadmin"], env["mgr"]) is True
    assert admin._can_assign_libraries(env["sysadmin"], env["sysadmin"]) is True
    assert admin._can_assign_libraries(env["mgr"], env["mgr"]) is True
    assert admin._can_assign_libraries(env["mgr"], env["user"]) is True
    assert admin._can_assign_libraries(env["mgr"], env["sysadmin"]) is False


def test_scan_filters_by_assigned_libraries(env):
    db = env["db"]
    vh = env["vh"]
    database = env["database"]
    user = env["user"]
    lib_a = env["lib_a"]

    db.add(database.UserLibrary(user_id=user.id, library_id=lib_a.id))
    db.commit()

    all_items = vh.scan_videos(db, sort="newest", page=1, limit=50, allowed_library_ids=None)
    assert all_items["total"] >= 2

    filtered = vh.scan_videos(
        db,
        sort="newest",
        page=1,
        limit=50,
        allowed_library_ids={lib_a.id},
    )
    assert filtered["total"] == 1
    assert filtered["items"][0]["library_id"] == lib_a.id

    empty = vh.scan_videos(db, sort="newest", page=1, limit=50, allowed_library_ids=set())
    assert empty["total"] == 0


def test_access_cache_invalidates_on_revoke(env):
    db = env["db"]
    vh = env["vh"]
    database = env["database"]
    user = env["user"]
    lib_a = env["lib_a"]

    db.add(database.UserLibrary(user_id=user.id, library_id=lib_a.id))
    db.commit()
    assert vh.user_can_access_media_path(db, user.id, "a/x.mp4") is True
    # 第二次应走缓存
    assert vh.user_can_access_media_path(db, user.id, "a/x.mp4") is True

    for row in db.scalars(
        select(database.UserLibrary).where(database.UserLibrary.user_id == user.id)
    ).all():
        db.delete(row)
    db.commit()
    # 未失效前缓存仍可能放行；失效后应拒绝
    vh.invalidate_media_access_cache(user_id=user.id)
    assert vh.user_can_access_media_path(db, user.id, "a/x.mp4") is False


def test_set_user_libraries(env):
    admin = env["admin_mod"]
    db = env["db"]
    user = env["user"]
    lib_a = env["lib_a"]
    lib_b = env["lib_b"]

    out = admin.set_user_libraries(
        user.id,
        admin.UserLibrariesRequest(library_ids=[lib_a.id, lib_b.id]),
        db,
        env["mgr"],
    )
    assert out["library_ids"] == sorted([lib_a.id, lib_b.id])

    # 重叠重存（含已有库）不应撞唯一约束
    out2 = admin.set_user_libraries(
        user.id,
        admin.UserLibrariesRequest(library_ids=[lib_a.id]),
        db,
        env["mgr"],
    )
    assert out2["library_ids"] == [lib_a.id]

    with pytest.raises(Exception) as ei:
        admin.set_user_libraries(
            env["sysadmin"].id,
            admin.UserLibrariesRequest(library_ids=[lib_a.id]),
            db,
            env["mgr"],
        )
    assert getattr(ei.value, "status_code", None) == 403
