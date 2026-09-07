"""删除白名单、LIKE 转义、合集嵌套改名"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(root))
    import importlib
    import app.video_handler as vh

    importlib.reload(vh)
    vh.MEDIA_ROOT = root.resolve()
    return root, vh


@pytest.fixture()
def main_mod():
    import app.main as main

    return main


def _touch_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xd9")


def test_delete_rejects_empty_dot_and_library_root(media_root):
    root, vh = media_root
    with pytest.raises(HTTPException) as ei:
        vh.delete_video("")
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        vh.delete_video(".")
    assert ei.value.status_code == 400

    (root / "samples").mkdir()
    with pytest.raises(HTTPException) as ei:
        vh.delete_video("samples")
    assert ei.value.status_code == 400
    assert "存储库根" in str(ei.value.detail)


def test_delete_album_requires_images(media_root):
    root, vh = media_root
    empty = root / "samples" / "empty_dir"
    empty.mkdir(parents=True)
    with pytest.raises(HTTPException) as ei:
        vh.delete_video("samples/empty_dir")
    assert "只能删除图片合集" in str(ei.value.detail)

    album = root / "samples" / "demo_album"
    _touch_image(album / "a.jpg")
    result = vh.delete_video("samples/demo_album")
    assert result["kind"] == "album"
    assert not album.exists()


def test_escape_like_and_path_prefix_avoids_underscore_wildcard(main_mod):
    assert main_mod._escape_like("demo_album") == r"demo\_album"
    assert main_mod._escape_like("100%") == r"100\%"
    assert main_mod._escape_like(r"a\b") == r"a\\b"

    from app.database import Base, Favorite, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db: Session = SessionLocal()
    try:
        u = User(username="t", password_hash="x", role="user", is_active=True)
        db.add(u)
        db.flush()
        db.add_all(
            [
                Favorite(user_id=u.id, video_path="samples/demo_album"),
                Favorite(user_id=u.id, video_path="samples/demo_album/a.jpg"),
                Favorite(user_id=u.id, video_path="samples/demoXalbum/b.jpg"),
                Favorite(user_id=u.id, video_path="samples/demo-album/c.jpg"),
            ]
        )
        db.commit()
        rows = db.scalars(
            select(Favorite).where(
                main_mod._path_exact_or_under(Favorite.video_path, "samples/demo_album")
            )
        ).all()
        paths = sorted(r.video_path for r in rows)
        assert paths == [
            "samples/demo_album",
            "samples/demo_album/a.jpg",
        ]
    finally:
        db.close()
        engine.dispose()


def test_edit_album_images_uses_relative_nested_path(media_root):
    root, vh = media_root
    album = root / "lib" / "album"
    nested = album / "nested"
    _touch_image(nested / "deep_yellow.jpg")
    _touch_image(album / "deep_yellow.jpg")

    result = vh.edit_album_images(
        vh.AlbumImagesEditRequest(
            album_path="lib/album",
            renames=[{"old_name": "nested/deep_yellow.jpg", "new_name": "deep_gold.jpg"}],
        )
    )
    assert len(result["changed"]) == 1
    assert result["changed"][0]["path"] == "lib/album/nested/deep_gold.jpg"
    assert (nested / "deep_gold.jpg").is_file()
    assert (album / "deep_yellow.jpg").is_file()
    assert not (nested / "deep_yellow.jpg").exists()
