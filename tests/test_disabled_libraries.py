"""存储库全部停用时不应回退扫描 MEDIA_ROOT。"""
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
def env(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"fake")
    sub = root / "movies"
    sub.mkdir()
    (sub / "a.mp4").write_bytes(b"fake")
    monkeypatch.setenv("MEDIA_ROOT", str(root))

    import importlib
    import app.database as database
    import app.video_handler as vh

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

    return {"db": db, "vh": vh, "database": database, "root": root}


def test_all_disabled_libraries_scan_empty(env):
    db = env["db"]
    vh = env["vh"]
    database = env["database"]

    db.add(database.Library(name="默认库", path="", enabled=False))
    db.add(database.Library(name="movies", path="movies", enabled=False))
    db.commit()

    items = vh._scan_filesystem(db)
    assert items == []


def test_enabled_library_still_scans(env):
    db = env["db"]
    vh = env["vh"]
    database = env["database"]

    lib = database.Library(name="movies", path="movies", enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)

    items = vh._scan_filesystem(db)
    assert len(items) == 1
    assert items[0].library_id == lib.id
    assert items[0].path.endswith("a.mp4")
