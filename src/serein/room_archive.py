"""M5 private full-room snapshots. Never read or restore another room.

A full room archive requires its memory service to be stopped for a coherent
file/SQLite snapshot. The original single-room backup tooling is untouched.
Archive contains secrets: access is administrator-only, with 0600 files.
Restore is intentionally preview-only in this phase.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from uuid import uuid4

from .room_manager import RoomError
from .file_lock import exclusive_lock

ARCHIVE_FORMAT = "serein-room-backup-v1"
ARCHIVE_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}\.tar\.gz$")
SCOPES = ("config", "data", "secrets")
MAX_FILES = 50000
MAX_BYTES = 16 * 1024 * 1024 * 1024


def _hash_file(file):
    digest = sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _room_paths(manager, room_id):
    room = manager.get(room_id)  # Always validate immutable UUID in the registry
    home = manager.room_dir(room_id)
    if home.is_symlink():
        raise RoomError("Room directory is a symbolic link")
    backup_dir = home / "backups"
    if backup_dir.is_symlink():
        raise RoomError("Room backup directory is a symbolic link")
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    return room, home, backup_dir


def _collect(home):
    files = []
    total = 0
    for scope in SCOPES:
        directory = home / scope
        if not directory.is_dir() or directory.is_symlink():
            raise RoomError("Room scope is missing or unsafe: " + scope)
        for item in sorted(directory.rglob("*")):
            if item.is_symlink():
                raise RoomError("Symbolic links cannot enter a room backup")
            if item.is_dir():
                continue
            if not item.is_file() or item.stat().st_nlink != 1:
                raise RoomError("Special or hard-linked files cannot enter a room backup")
            # SQLite backup API incorporates WAL pages; including live sidecars
            # alongside the completed database would create an inconsistent restore.
            if item.name.endswith(("-wal", "-shm")) and Path(str(item).rsplit("-", 1)[0]).exists():
                continue
            size = item.stat().st_size
            total += size
            files.append((item.relative_to(home).as_posix(), item))
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise RoomError("Room backup exceeds safety limits")
    if not (home / "data" / "serein.db").is_file():
        raise RoomError("Room memory database is missing")
    if not (home / "secrets" / "gateway-key").is_file():
        raise RoomError("Room Gateway Key is missing")
    return files


def _sqlite_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(target) as destination:
                original.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RoomError("SQLite snapshot failed integrity check")
    except sqlite3.DatabaseError as exc:
        raise RoomError("Invalid SQLite database in room backup") from exc
    target.chmod(0o600)


def _write_archive(manager, room_id):
    room, home, backups = _room_paths(manager, room_id)
    files = _collect(home)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12] + ".tar.gz"
    final = backups / name
    pending = backups / ("." + uuid4().hex + ".pending")
    try:
        with tempfile.TemporaryDirectory(prefix=".room-sqlite-", dir=backups) as working:
            staged = Path(working)
            content = []
            for relative, original in files:
                source = original
                if original.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                    source = staged / relative
                    _sqlite_copy(original, source)
                record = {"path": relative, "size": source.stat().st_size,
                          "sha256": _hash_file(source)}
                content.append((record, source))
            key_digest = sha256((home / "secrets" / "gateway-key").read_bytes().strip()).hexdigest()
            with sqlite3.connect(staged / "data" / "serein.db") as database:
                db_user_version = database.execute("PRAGMA user_version").fetchone()[0]
                schema_rows = database.execute(
                    "SELECT name,type,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                schema_digest = sha256(json.dumps(
                    schema_rows, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
            manifest = {
                "format": ARCHIVE_FORMAT, "room_id": room_id,
                "room_slug": room["room_slug"], "created_at": datetime.now(timezone.utc).isoformat(),
                "image": room["image"], "gateway_key_sha256": key_digest,
                "sqlite_user_version": db_user_version, "sqlite_schema_sha256": schema_digest,
                "includes_secrets": True,
                "files": [record for record, _ in content],
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")
            with tarfile.open(pending, mode="w:gz", dereference=False) as archive:
                meta = tarfile.TarInfo("manifest.json")
                meta.mode = 0o600
                meta.size = len(manifest_bytes)
                archive.addfile(meta, BytesIO(manifest_bytes))
                for record, source in content:
                    info = tarfile.TarInfo("payload/" + record["path"])
                    info.size = record["size"]
                    info.mode = 0o600
                    with source.open("rb") as stream:
                        archive.addfile(info, stream)
        pending.chmod(0o600)
        os.replace(pending, final)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return {"name": name, "room_id": room_id, "room_slug": room["room_slug"],
            "bytes": final.stat().st_size, "sha256": _hash_file(final),
            "file_count": len(content), "includes_secrets": True}


def create_room_backup(manager, room_id, runtime, *, pause_running=True):
    """Stop only the target room, snapshot it, then resume its prior state.

    A nonblocking, per-room OS lock prevents two browser tabs from racing:
    one task must never resume a room while another is still copying files.
    """
    _, _, backups = _room_paths(manager, room_id)
    lock = backups / ".snapshot.lock"
    if lock.is_symlink():
        raise RoomError("Unsafe room backup lock")
    try:
        with exclusive_lock(lock):
            room = manager.get(room_id)
            if room["state"] not in ("stopped", "ready"):
                raise RoomError("Room must be stopped or ready to create a complete backup")
            status = runtime.status(manager, room)
            running = status.get("container") == "running"
            if room["state"] == "stopped" and running:
                raise RoomError("Room appears running despite stopped registry state")
            if room["state"] == "ready" and not pause_running:
                raise RoomError("Stop the room for a coherent complete backup")
            paused = room["state"] == "ready"
            if paused:
                manager.stop(room_id, runtime)
            try:
                result = _write_archive(manager, room_id)
            finally:
                if paused:
                    manager.start(room_id, runtime)
            result["restarted"] = paused
            return result
    except RoomError:
        # RoomError itself derives from RuntimeError. Do not turn actual
        # validation, SQLite or symlink failures into a false lock error.
        raise
    except RuntimeError as exc:
        if str(exc) != "已有任务运行，请不要双开。":
            raise
        raise RoomError("Room backup is already running") from exc

def room_backups(manager, room_id):
    _, _, directory = _room_paths(manager, room_id)
    result = []
    for path in directory.iterdir():
        if not ARCHIVE_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        result.append({"name": path.name, "bytes": path.stat().st_size,
                       "modified_ns": path.stat().st_mtime_ns})
    return sorted(result, key=lambda item: item["name"], reverse=True)


def backup_file(manager, room_id, name):
    if not isinstance(name, str) or not ARCHIVE_NAME.fullmatch(name):
        raise RoomError("Invalid backup file identifier")
    _, _, directory = _room_paths(manager, room_id)
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise RoomError("Backup file not found")
    return path


def preview_room_backup(manager, room_id, name):
    """Validate every member and hash without extracting or modifying a room."""
    path = backup_file(manager, room_id, name)
    room = manager.get(room_id)
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_FILES + 1:
                raise RoomError("Invalid backup member count")
            meta = members[0]
            if meta.name != "manifest.json" or not meta.isfile() or meta.size > 8 * 1024 * 1024:
                raise RoomError("Invalid room backup manifest")
            stream = archive.extractfile(meta)
            if stream is None:
                raise RoomError("Missing room backup manifest")
            manifest = json.loads(stream.read(8 * 1024 * 1024 + 1))
            if manifest.get("format") != ARCHIVE_FORMAT:
                raise RoomError("Unsupported room backup format")
            if (manifest.get("room_id") != room_id or
                    manifest.get("room_slug") != room["room_slug"]):
                raise RoomError("This backup belongs to a different AI room")
            expected_key = sha256((manager.room_dir(room_id) / "secrets" /
                                   "gateway-key").read_bytes().strip()).hexdigest()
            if manifest.get("gateway_key_sha256") != expected_key:
                raise RoomError("Backup Gateway identity does not match this room")
            entries = manifest.get("files")
            if not isinstance(entries, list) or len(entries) != len(members) - 1:
                raise RoomError("Backup file list does not match manifest")
            seen = set()
            total = 0
            for member, entry in zip(members[1:], entries):
                relative = entry.get("path")
                if (not isinstance(relative, str) or relative.startswith("/") or
                        "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/")) or
                        relative.split("/", 1)[0] not in SCOPES or
                        not member.isfile() or member.name != "payload/" + relative or
                        relative in seen or
                        not isinstance(entry.get("size"), int) or entry["size"] != member.size):
                    raise RoomError("Unsafe backup member path or file size")
                seen.add(relative)
                total += member.size
                if total > MAX_BYTES:
                    raise RoomError("Backup exceeds size limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RoomError("Unreadable backup member")
                digest = sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                if digest.hexdigest() != entry.get("sha256"):
                    raise RoomError("Backup checksum mismatch")
    except (tarfile.TarError, OSError, ValueError, KeyError, TypeError,
            json.JSONDecodeError) as exc:
        raise RoomError("Invalid or damaged room archive") from exc
    return {"name": name, "room_id": room_id, "room_slug": room["room_slug"],
            "created_at": manifest["created_at"], "image": manifest["image"],
            "sqlite_user_version": manifest.get("sqlite_user_version"),
            "sqlite_schema_sha256": manifest.get("sqlite_schema_sha256"),
            "files": len(entries), "total_uncompressed_bytes": total,
            "archive_sha256": _hash_file(path), "includes_secrets": True,
            "verified": True, "restore_available": False}
