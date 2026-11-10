"""Room-scoped backup and selective exports using synthetic independent stores."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from serein.api.export import database_snapshot, markdown_archive
from serein.core.store import Store
from serein.room_manager import RoomManager


def _seed(db, label):
    with Store(db) as store:
        store.create(f"scene-{label}", "scene", f"Scene {label}", f"PRIVATE_SCENE_{label}")
        store.create(f"event-{label}", "event", f"Event {label}", f"PRIVATE_EVENT_{label}")
        for category in ("dream", "mood", "memo", "diary", "summary-task"):
            store.conn.execute(
                "INSERT INTO background_state(name,value_json) VALUES (?,?)",
                (f"private-{category}", json.dumps({"content":f"PRIVATE_{category.upper()}_{label}"})),
            )
        store.conn.commit()


def test_database_backup_contains_current_room_private_state_only(tmp_path):
    manager=RoomManager(tmp_path / "rooms")
    a=manager.create("xiaogi-main", source="blank",room_slug="xiaogi-main")["room"]["room_id"]
    b=manager.create("other-main", source="blank",room_slug="other-main")["room"]["room_id"]
    db_a=manager.room_dir(a)/"data"/"serein.db"
    db_b=manager.room_dir(b)/"data"/"serein.db"
    _seed(db_a, "A")
    _seed(db_b, "B")
    backup=database_snapshot(db_a)
    try:
        snapshot=Path(backup.path)
        assert snapshot.is_file()
        with sqlite3.connect(snapshot) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone()[0]=="ok"
            rows=connection.execute("SELECT body_md FROM revisions").fetchall()
            saved=" ".join(row[0] for row in rows)
            assert "PRIVATE_SCENE_A" in saved and "PRIVATE_EVENT_A" in saved
            assert "PRIVATE_SCENE_B" not in saved and "PRIVATE_EVENT_B" not in saved
            tasks=connection.execute(
                "SELECT value_json FROM background_state WHERE name LIKE 'private-%'"
            ).fetchall()
            data=" ".join(row[0] for row in tasks)
            for part in ("DREAM","MOOD","MEMO","DIARY","SUMMARY-TASK"):
                assert f"PRIVATE_{part}_A" in data
                assert f"PRIVATE_{part}_B" not in data
    finally:
        backup.background.func()


@pytest.mark.parametrize("kind,expected",[
    ("all",{"scene/","event/"}),
    ("scene",{"scene/"}),
    ("event",{"event/"}),
])
def test_selective_markdown_export_only_current_room_and_requested_category(
        tmp_path,kind,expected):
    manager=RoomManager(tmp_path/"rooms")
    ids=[
        manager.create("A",source="blank")["room"]["room_id"],
        manager.create("B",source="blank")["room"]["room_id"],
    ]
    db_a=manager.room_dir(ids[0])/"data"/"serein.db"
    db_b=manager.room_dir(ids[1])/"data"/"serein.db"
    _seed(db_a,"A")
    _seed(db_b,"B")
    result=markdown_archive(db_a,kind)
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        names=archive.namelist()
        folders={n for n in names if n.endswith("/")}
        assert folders==expected
        texts="\n".join(archive.read(n).decode() for n in names if n.endswith(".md"))
        assert "_B" not in texts
        if kind!="event":assert "PRIVATE_SCENE_A" in texts
        else:assert "PRIVATE_SCENE_A" not in texts
        if kind!="scene":assert "PRIVATE_EVENT_A" in texts
        else:assert "PRIVATE_EVENT_A" not in texts


def test_invalid_export_category_is_rejected(tmp_path):
    db=tmp_path/"storage.db"
    with Store(db):
        pass
    with pytest.raises(ValueError,match="Invalid export category"):
        markdown_archive(db,"secret")
