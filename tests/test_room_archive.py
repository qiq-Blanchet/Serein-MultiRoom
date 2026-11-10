"""Offline tests: coherent full-room archives must never mix AI rooms.

All source records and gateway credentials in this file are synthetic.
"""
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile

import pytest

from serein.core.store import Store
from serein.room_archive import (
    backup_file, create_room_backup, preview_room_backup, room_backups,
)
from serein.room_manager import RoomError, RoomManager


class Runtime:
    def __init__(self):
        self.running = set()
        self.calls = []

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


def _seed(manager, room_id, tag):
    home = manager.room_dir(room_id)
    with Store(home / "data" / "serein.db") as store:
        store.create("scene-" + tag, "scene", tag, "SECRET_SCENE_" + tag)
        store.conn.execute(
            "INSERT INTO background_state(name,value_json) VALUES (?,?)",
            ("private-memo", json.dumps({"memo": "SECRET_MEMO_" + tag})),
        )
        store.conn.commit()
    (home / "data" / "uploads").mkdir()
    (home / "data" / "uploads" / "image.png").write_bytes(("IMAGE_" + tag).encode())
    return home


def test_full_room_backup_includes_one_rooms_config_and_data_without_connection_keys(tmp_path):
    manager = RoomManager(tmp_path / "isolated")
    first = manager.create("A", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    second = manager.create("B", source="blank", room_slug="other-main")["room"]["room_id"]
    a = _seed(manager, first, "A")
    _seed(manager, second, "B")
    runtime = Runtime()
    manager.start(first, runtime)
    created = create_room_backup(manager, first, runtime)
    assert created["room_id"] == first
    assert created["room_slug"] == "xiaogi-main"
    assert created["restarted"] is True
    assert runtime.calls == [("start",first), ("stop",first), ("start",first)]
    assert manager.get(first)["state"] == "ready"
    assert manager.get(second)["state"] == "stopped"
    assert len(room_backups(manager, first)) == 1
    assert room_backups(manager, second) == []

    archive = backup_file(manager, first, created["name"])
    assert archive.stat().st_mode & 0o077 == 0
    with tarfile.open(archive,"r:gz") as packed:
        names = packed.getnames()
        assert names[0] == "manifest.json"
        assert all(n == "manifest.json" or n.startswith("payload/") for n in names)
        assert "payload/config/config.toml" in names
        assert "payload/secrets/gateway-key" not in names
        assert "payload/secrets/api-token" not in names
        assert not any(name.startswith("payload/secrets/") for name in names)
        assert "payload/data/serein.db" in names
        assert "payload/data/uploads/image.png" in names
        assert all(not n.startswith("payload/backups/") for n in names)
        assert packed.extractfile("payload/data/uploads/image.png").read() == b"IMAGE_A"
        data = packed.extractfile("payload/data/serein.db").read()
        assert b"SECRET_SCENE_B" not in data and b"SECRET_MEMO_B" not in data
        manifest = json.load(packed.extractfile("manifest.json"))
        assert manifest["room_id"] == first
        assert manifest["room_slug"] == "xiaogi-main"
        assert manifest["includes_secrets"] is False
        assert "gateway_key_sha256" not in manifest
        assert manifest["format"] == "serein-room-backup-v2"
        assert isinstance(manifest["sqlite_user_version"], int)
        assert len(manifest["sqlite_schema_sha256"]) == 64
        assert "SECRET_SCENE_A" not in json.dumps(manifest)
        local = tmp_path/"inspect.db"
        local.write_bytes(data)
        with sqlite3.connect(local) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0]=="ok"
            assert "SECRET_MEMO_A" in conn.execute(
                "SELECT value_json FROM background_state WHERE name='private-memo'"
            ).fetchone()[0]
    preview = preview_room_backup(manager, first, created["name"])
    assert preview["verified"] is True
    assert preview["includes_secrets"] is False
    assert preview["same_installation"] is True
    assert preview["sqlite_schema_sha256"] == manifest["sqlite_schema_sha256"]
    assert preview["restore_available"] is False
    assert preview["files"] > 3
    assert "gateway_key" not in preview
    with pytest.raises(RoomError):
        backup_file(manager, second, "../" + created["name"])
    assert (a / "data" / "uploads" / "image.png").read_bytes() == b"IMAGE_A"


def test_preview_rejects_cross_room_archive_even_when_file_is_copied(tmp_path):
    manager = RoomManager(tmp_path / "isolated")
    a = manager.create("A", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    b = manager.create("B", source="blank", room_slug="other-main")["room"]["room_id"]
    runtime = Runtime()
    created = create_room_backup(manager, a, runtime)
    original = backup_file(manager, a, created["name"])
    other = manager.room_dir(b) / "backups" / created["name"]
    shutil.copy2(original, other)
    with pytest.raises(RoomError, match="different AI room"):
        preview_room_backup(manager, b, created["name"])
    assert manager.get(a)["state"] == manager.get(b)["state"] == "stopped"


def test_preview_rejects_checksum_tampering_and_unsafe_archive_members(tmp_path):
    manager = RoomManager(tmp_path / "isolated")
    a = manager.create("A", source="blank")["room"]["room_id"]
    created = create_room_backup(manager, a, Runtime())
    archive = backup_file(manager, a, created["name"])
    modified = archive.with_name("20261110T140000Z-aabbccddeeff.tar.gz")
    with tarfile.open(archive, "r:gz") as source, tarfile.open(modified,"w:gz") as target:
        for member in source.getmembers():
            content = source.extractfile(member).read()
            if member.name == "payload/config/config.toml":
                content = b"X" * len(content)
            entry = tarfile.TarInfo(member.name)
            entry.size = len(content)
            target.addfile(entry,io.BytesIO(content))
    with pytest.raises(RoomError, match="checksum"):
        preview_room_backup(manager,a,modified.name)

    unsafe = archive.with_name("20261110T140000Z-001122334455.tar.gz")
    with tarfile.open(archive, "r:gz") as source, tarfile.open(unsafe,"w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            name = "payload/../../other-room/secret" if member.name=="payload/config/config.toml" else member.name
            item = tarfile.TarInfo(name)
            item.size = len(data)
            target.addfile(item,io.BytesIO(data))
    with pytest.raises(RoomError, match="Unsafe"):
        preview_room_backup(manager,a,unsafe.name)


def test_archiver_rejects_symlinks_and_does_not_stop_other_rooms(tmp_path):
    manager=RoomManager(tmp_path/"isolated")
    a=manager.create("A",source="blank")["room"]["room_id"]
    b=manager.create("B",source="blank")["room"]["room_id"]
    runtime=Runtime()
    manager.start(a,runtime)
    manager.start(b,runtime)
    linked=manager.room_dir(a)/"data"/"outside.txt"
    outside=tmp_path/"outside.txt"
    outside.write_text("SYNTHETIC_PRIVATE_B")
    linked.symlink_to(outside)
    with pytest.raises(RoomError, match="Symbolic links"):
        create_room_backup(manager,a,runtime)
    assert manager.get(a)["state"]=="ready"
    assert manager.get(b)["state"]=="ready"
    assert b in runtime.running
    assert room_backups(manager,a)==[]
    assert not any("SYNTHETIC_PRIVATE_B" in p.read_text(errors="ignore")
                   for p in (manager.room_dir(a)/"backups").glob("*.tar.gz"))


def test_concurrent_snapshots_are_rejected_before_room_is_paused(tmp_path):
    from serein.file_lock import exclusive_lock
    manager = RoomManager(tmp_path / "rooms")
    room = manager.create("A", source="blank")["room"]["room_id"]
    runtime = Runtime()
    manager.start(room, runtime)
    path = manager.room_dir(room) / "backups" / ".snapshot.lock"
    with exclusive_lock(path):
        with pytest.raises(RoomError, match="already running"):
            create_room_backup(manager, room, runtime)
    assert manager.get(room)["state"] == "ready"
    assert runtime.running == {room}
    assert room_backups(manager, room) == []


def test_portable_room_archive_can_be_checked_after_vps_reinstallation(tmp_path):
    """Different installation UUIDs and new gateway tokens must not block backup."""
    old = RoomManager(tmp_path / "old-vps")
    old_id = old.create("Old", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    _seed(old, old_id, "OLD_VPS")
    archived = create_room_backup(old, old_id, Runtime())
    source = backup_file(old, old_id, archived["name"])
    new = RoomManager(tmp_path / "replacement-vps")
    new_created = new.create("New", source="blank", room_slug="xiaogi-main")
    new_id = new_created["room"]["room_id"]
    assert new_id != old_id
    assert new_created["gateway_key"] != (
        old.room_dir(old_id) / "secrets" / "gateway-key"
    ).read_text().strip()
    copy = new.room_dir(new_id) / "backups" / archived["name"]
    shutil.copyfile(source, copy)
    result = preview_room_backup(new, new_id, archived["name"])
    assert result["verified"] is True
    assert result["room_slug"] == "xiaogi-main"
    assert result["source_room_id"] == old_id
    assert result["room_id"] == new_id
    assert result["same_installation"] is False
    assert result["includes_secrets"] is False
    assert result["restore_available"] is False
    # Preview never writes into the replacement instance's existing data.
    with Store(new.room_dir(new_id) / "data" / "serein.db") as store:
        assert not store.conn.execute(
            "SELECT 1 FROM documents WHERE id=?", ("scene-OLD_VPS",)
        ).fetchone()


def test_legacy_archives_remain_previewable_without_matching_gateway_keys(tmp_path):
    """Old key-bearing v1 archives remain readable, but don't grant access."""
    from hashlib import sha256
    manager = RoomManager(tmp_path / "old-vps")
    old_id = manager.create("Old", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    result = create_room_backup(manager, old_id, Runtime())
    source = backup_file(manager, old_id, result["name"])
    with tarfile.open(source, "r:gz") as packed:
        contents = {member.name: packed.extractfile(member).read()
                    for member in packed.getmembers()}
    manifest = json.loads(contents["manifest.json"])
    manifest["format"] = "serein-room-backup-v1"
    manifest["includes_secrets"] = True
    old_key = (manager.room_dir(old_id) / "secrets" / "gateway-key").read_bytes()
    contents["payload/secrets/gateway-key"] = old_key
    manifest["files"].append({
        "path": "secrets/gateway-key", "size": len(old_key),
        "sha256": sha256(old_key).hexdigest(),
    })
    contents["manifest.json"] = json.dumps(manifest).encode("utf-8")
    legacy = source.with_name("20261110T140000Z-abcdefabcdef.tar.gz")
    with tarfile.open(legacy, "w:gz") as packed:
        for name, blob in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            packed.addfile(info, io.BytesIO(blob))
    fresh = RoomManager(tmp_path / "new-vps")
    new_id = fresh.create("New", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    assert new_id != old_id
    fresh_path = fresh.room_dir(new_id) / "backups" / legacy.name
    shutil.copyfile(legacy, fresh_path)
    preview = preview_room_backup(fresh, new_id, legacy.name)
    assert preview["verified"] is True
    assert preview["includes_secrets"] is True  # Important legacy warning.
    assert preview["same_installation"] is False
    assert preview["restore_available"] is False
    assert preview["room_slug"] == "xiaogi-main"
