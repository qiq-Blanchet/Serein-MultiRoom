"""Offline real SQLite room restore tests; no user data or real Docker touched."""
import json
import os
import shutil
import sqlite3
import tarfile

import pytest

from serein.core.store import Store
from serein.file_lock import exclusive_lock
from serein.room_archive import backup_file, create_room_backup, room_backups
from serein.room_manager import RoomError, RoomManager
from serein.room_restore import (
    plan_room_restore, restore_room_backup, recover_room_restore,
)


class Runtime:
    def __init__(self):
        self.running = set()
        self.calls = []
        self.fail_recreate = False

    def status(self, manager, room):
        return {"container": "running" if room["room_id"] in self.running else "exited"}

    def start(self, manager, room):
        self.calls.append(("start", room["room_id"]))
        self.running.add(room["room_id"])

    def stop(self, manager, room):
        self.calls.append(("stop", room["room_id"]))
        self.running.discard(room["room_id"])

    def wait_healthy(self, manager, room, *, timeout):
        return room["room_id"] in self.running

    def remove_stopped(self, manager, room):
        self.calls.append(("recreate", room["room_id"]))
        if self.fail_recreate:
            self.fail_recreate = False
            raise RoomError("Synthetic Docker recreate failure")
        assert room["room_id"] not in self.running


def seed(manager, rid, label):
    home = manager.room_dir(rid)
    with Store(home / "data" / "serein.db") as store:
        store.create("scene-" + label, "scene", "Synthetic " + label,
                     "SECRET_ONLY_" + label)
        store.conn.commit()
    pictures = home / "data" / "uploads"
    pictures.mkdir(exist_ok=True)
    (pictures / "image.png").write_bytes(label.encode())


def get_scene(manager, rid, label):
    with Store(manager.room_dir(rid) / "data" / "serein.db") as store:
        return store.conn.execute(
            "SELECT 1 FROM documents WHERE id=?", ("scene-" + label,)
        ).fetchone() is not None


def setup(tmp_path):
    source = RoomManager(tmp_path / "old-server")
    dest = RoomManager(tmp_path / "new-server")
    source_id = source.create("A", room_slug="xiaogi-main", source="blank")["room"]["room_id"]
    dest_created = dest.create("A", room_slug="xiaogi-main", source="blank")
    target_id = dest_created["room"]["room_id"]
    other_id = dest.create("B", room_slug="other-main", source="blank")["room"]["room_id"]
    seed(source, source_id, "OLD")
    seed(dest, target_id, "CURRENT")
    seed(dest, other_id, "OTHER")
    original = create_room_backup(source, source_id, Runtime())
    archive = backup_file(source, source_id, original["name"])
    shutil.copy2(archive, dest.room_dir(target_id) / "backups" / original["name"])
    return source, source_id, dest, target_id, other_id, original


def test_same_slug_restores_on_new_vps_with_new_gateway_key_and_rollback(tmp_path):
    source, source_id, dest, target, other, archive = setup(tmp_path)
    assert source_id != target
    old_key = (source.room_dir(source_id) / "secrets" / "gateway-key").read_bytes()
    current_key = (dest.room_dir(target) / "secrets" / "gateway-key").read_bytes()
    current_api_token = (dest.room_dir(target) / "secrets" / "api-token").read_bytes()
    assert old_key != current_key
    runtime = Runtime()
    dest.start(target, runtime)
    plan = plan_room_restore(dest, target, archive["name"])
    assert plan["same_installation"] is False
    assert plan["source_room_id"] == source_id
    assert plan["restore_available"] is True
    assert plan["schema_compatible"] is True

    result = restore_room_backup(
        dest, target, archive["name"], runtime,
        confirm_slug="xiaogi-main", expected_sha256=plan["archive_sha256"])
    assert result["restored"] and result["restarted"]
    assert result["gateway_key_unchanged"]
    assert dest.get(target)["state"] == "ready"
    assert get_scene(dest, target, "OLD")
    assert not get_scene(dest, target, "CURRENT")
    assert get_scene(dest, other, "OTHER")
    assert (dest.room_dir(target) / "data" / "uploads" / "image.png").read_bytes() == b"OLD"
    assert (dest.room_dir(target) / "secrets" / "gateway-key").read_bytes() == current_key
    assert (dest.room_dir(target) / "secrets" / "api-token").read_bytes() == current_api_token
    assert result["rollback_backup"] != archive["name"]
    assert result["rollback_backup"] in [x["name"] for x in room_backups(dest, target)]
    assert not (dest.room_dir(target) / "backups" / ".restore-journal.json").exists()
    assert runtime.calls == [("start", target), ("stop", target),
                              ("recreate", target), ("start", target)]
    assert all(room_id == target for _, room_id in runtime.calls)


def test_restore_rejects_wrong_room_hash_and_foreign_slug_without_stopping(tmp_path):
    _, _, dest, target, other, archive = setup(tmp_path)
    runtime = Runtime()
    dest.start(target, runtime)
    plan = plan_room_restore(dest, target, archive["name"])
    with pytest.raises(RoomError, match="room ID|Room ID"):
        restore_room_backup(dest,target,archive["name"],runtime,
                            confirm_slug="other-main",
                            expected_sha256=plan["archive_sha256"])
    with pytest.raises(RoomError, match="changed since preview"):
        restore_room_backup(dest,target,archive["name"],runtime,
                            confirm_slug="xiaogi-main",expected_sha256="f"*64)
    foreign = dest.room_dir(other) / "backups" / archive["name"]
    shutil.copy2(backup_file(dest,target,archive["name"]), foreign)
    with pytest.raises(RoomError, match="different AI room"):
        plan_room_restore(dest,other,archive["name"])
    assert runtime.calls == [("start",target)]
    assert get_scene(dest,target,"CURRENT")
    assert get_scene(dest,other,"OTHER")


def test_restore_failure_rewinds_target_file_dirs_and_keeps_rollback_backup(tmp_path):
    _, _, dest, target, other, archive = setup(tmp_path)
    runtime=Runtime()
    dest.start(target,runtime)
    runtime.fail_recreate=True
    digest=plan_room_restore(dest,target,archive["name"])["archive_sha256"]
    with pytest.raises(RoomError,match="Synthetic Docker recreate failure"):
        restore_room_backup(dest,target,archive["name"],runtime,
            confirm_slug="xiaogi-main",expected_sha256=digest)
    assert get_scene(dest,target,"CURRENT")
    assert not get_scene(dest,target,"OLD")
    assert get_scene(dest,other,"OTHER")
    assert dest.get(target)["state"]=="stopped"
    assert not (dest.room_dir(target)/"backups"/".restore-journal.json").exists()
    assert len(room_backups(dest,target))==2  # source + pre-restore rollback


def test_interrupted_restore_journal_refuses_boot_and_repairs_previous_room(tmp_path):
    _, _, dest,target,other,archive=setup(tmp_path)
    runtime=Runtime()
    home=dest.room_dir(target)
    tag="c"*32
    data=home/"data"
    old=home/(".restore-previous-"+tag+"-data")
    backup_dir=home/"backups"
    journal=backup_dir/".restore-journal.json"
    journal.write_text(json.dumps({"tag":tag,"room_id":target,
                                   "source_archive":archive["name"]}))
    os.replace(data,old)  # Crash in middle of first directory swap.
    with pytest.raises(RoomError,match="Interrupted restore"):
        dest.start(target,runtime)
    assert runtime.calls == []
    result=recover_room_restore(dest,target,runtime)
    assert result["recovered"] is True
    assert get_scene(dest,target,"CURRENT")
    assert get_scene(dest,other,"OTHER")
    assert not journal.exists()
    dest.start(target,runtime)
    assert dest.get(target)["state"]=="ready"


def test_restore_requires_schema_compatibility_before_pausing(tmp_path):
    _, _, dest,target,other,archive=setup(tmp_path)
    db=dest.room_dir(target)/"data"/"serein.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE new_schema_only(id TEXT)")
        conn.commit()
    runtime=Runtime()
    plan=plan_room_restore(dest,target,archive["name"])
    assert plan["schema_compatible"] is False
    assert plan["restore_available"] is False
    with pytest.raises(RoomError,match="schema mismatch"):
        restore_room_backup(dest,target,archive["name"],runtime,
            confirm_slug="xiaogi-main",expected_sha256=plan["archive_sha256"])
    assert runtime.calls==[]
    assert get_scene(dest,target,"CURRENT")
    assert get_scene(dest,other,"OTHER")


def test_restore_held_snapshot_lock_does_not_modify_target(tmp_path):
    _, _, dest,target,_,archive=setup(tmp_path)
    runtime=Runtime()
    path=dest.room_dir(target)/"backups"/".snapshot.lock"
    with exclusive_lock(path):
        with pytest.raises(RoomError,match="already running"):
            restore_room_backup(dest,target,archive["name"],runtime,
                confirm_slug="xiaogi-main",
                expected_sha256=plan_room_restore_without_lock(dest,target,archive["name"]))
    assert get_scene(dest,target,"CURRENT")


def plan_room_restore_without_lock(dest,target,name):
    return plan_room_restore(dest,target,name)["archive_sha256"]
