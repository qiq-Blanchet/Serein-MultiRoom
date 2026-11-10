"""M5 Phase 3: opt-in, crash-recoverable *same room ID* restore.

The administrator must explicitly confirm a validated plan with its archive
SHA-256 and room slug. Gateway Key and all destination secrets remain untouched.
No caller-controlled file paths are extracted; tar members are streamed only
from pre-verified config/ and data/ entries. All operations are scoped to one
registered immutable room UUID; the human room slug is the portable identity.

Interrupted renames leave a journal and .restore-previous-* directories in
that same room. Room startup is blocked until administrator recovery.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import os
import re
import shutil
import sqlite3
import tarfile
from uuid import uuid4

from .file_lock import exclusive_lock
from .room_archive import (
    MAX_BYTES, MAX_FILES, ARCHIVE_FORMAT, LEGACY_ARCHIVE_FORMAT,
    _hash_file, _room_paths, _write_archive, backup_file, preview_room_backup,
)
from .room_manager import RoomError

_SCOPE = ("config", "data")
_JOURNAL = ".restore-journal.json"
_SHA = re.compile(r"^[a-f0-9]{64}$")
_LOCK_CONFLICT = "已有任务运行，请不要双开。"


def _schema(db):
    if not db.is_file() or db.is_symlink():
        raise RoomError("Restore database is missing or unsafe")
    try:
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RoomError("Restore database SQLite integrity failed")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            structure = conn.execute(
                "SELECT name,type,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RoomError("Restore database is unreadable") from exc
    digest = sha256(json.dumps(
        structure, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {"sqlite_user_version": version, "sqlite_schema_sha256": digest}


def _preflight(manager, room_id, name, *, expected_sha256=None):
    room, home, directory = _room_paths(manager, room_id)
    if (directory / _JOURNAL).exists():
        raise RoomError("Interrupted restore requires recovery before continuing")
    report = preview_room_backup(manager, room_id, name)
    # A stable room slug, NOT the old installation UUID or Gateway Key, is the
    # cross-VPS recovery identity. Checksums detect corruption, not authorship.
    actual_hash = report["archive_sha256"]
    if expected_sha256 is not None and expected_sha256 != actual_hash:
        raise RoomError("Backup changed since preview")
    current = _schema(home / "data" / "serein.db")
    claimed = {
        "sqlite_user_version": report["sqlite_user_version"],
        "sqlite_schema_sha256": report["sqlite_schema_sha256"],
    }
    if not isinstance(claimed["sqlite_user_version"], int) or not (
            isinstance(claimed["sqlite_schema_sha256"], str) and
            _SHA.fullmatch(claimed["sqlite_schema_sha256"])):
        raise RoomError("Archive lacks a compatible database schema fingerprint")
    # Older app versions require a separate migration procedure; restoring
    # incompatible schema into a current live service would risk data loss.
    compatible = claimed == current
    return {**report, "schema_compatible": compatible,
            "destination_schema": current,
            "connection_credentials": "keep_destination",
            "room_slug_matches": True, "restore_available": compatible,
            "requires_confirmation": True, "rollback_snapshot": True}


def plan_room_restore(manager, room_id, name):
    """Read-only, signed-room-aware restore plan. No database or config writes."""
    return _preflight(manager, room_id, name)


def _journal_file(home):
    return home / "backups" / _JOURNAL


def _atomic_json(file, document):
    pending = file.with_name("." + uuid4().hex + ".pending")
    try:
        with pending.open("x", encoding="utf-8") as out:
            os.chmod(pending, 0o600)
            json.dump(document, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(pending, file)
    finally:
        pending.unlink(missing_ok=True)


def _stage(archive_path, home, tag):
    stage = home / "backups" / (".restore-stage-" + tag)
    stage.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) < 2 or len(members) > MAX_FILES + 1:
                raise RoomError("Invalid restore file count")
            manifest_file = archive.extractfile(members[0])
            if members[0].name != "manifest.json" or manifest_file is None:
                raise RoomError("Missing restore manifest")
            manifest = json.load(manifest_file)
            if manifest.get("format") not in (ARCHIVE_FORMAT, LEGACY_ARCHIVE_FORMAT):
                raise RoomError("Invalid restore format")
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) != len(members)-1:
                raise RoomError("Restore archive inventory differs from manifest")
            total = 0
            for member, record in zip(members[1:], records):
                relative = record.get("path")
                if not isinstance(relative, str) or (
                        not member.isfile() or member.name != "payload/" + relative or
                        relative.startswith("/") or "\\" in relative or
                        any(part in ("", ".", "..") for part in relative.split("/"))):
                    raise RoomError("Unsafe restore archive member")
                if relative.split("/", 1)[0] not in _SCOPE:
                    # Older v1 archives contain keys. Never restore those:
                    # fresh destination Gateway/Chat/MCP identity must survive.
                    if manifest["format"] == LEGACY_ARCHIVE_FORMAT and relative.startswith("secrets/"):
                        continue
                    raise RoomError("Restore archive includes forbidden path")
                if record.get("size") != member.size:
                    raise RoomError("Restore file size mismatch")
                total += member.size
                if total > MAX_BYTES:
                    raise RoomError("Restore archive exceeds allowed size")
                destination = stage.joinpath(*relative.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = sha256()
                stream = archive.extractfile(member)
                if stream is None:
                    raise RoomError("Unreadable restore content")
                with destination.open("xb") as out:
                    os.chmod(destination, 0o600)
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                        out.write(block)
                if digest.hexdigest() != record.get("sha256"):
                    raise RoomError("Restore content changed during extraction")
        for scope in _SCOPE:
            if not (stage / scope).is_dir():
                raise RoomError("Restore archive has no " + scope + " directory")
        if not (stage / "config" / "config.toml").is_file():
            raise RoomError("Restore archive misses configuration")
        _schema(stage / "data" / "serein.db")
        return stage
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _remove_tree(path):
    if path.is_symlink():
        raise RoomError("Unsafe restore path is a symlink")
    if path.exists():
        shutil.rmtree(path)


def _recover_locked(manager, room_id):
    """Repair interrupted swaps using old data directories before restart."""
    _, home, backups = _room_paths(manager, room_id)
    journal = _journal_file(home)
    if not journal.exists():
        return {"recovered": False, "room_id": room_id}
    if journal.is_symlink():
        raise RoomError("Unsafe restore recovery journal")
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        tag = data["tag"]
        if (not re.fullmatch(r"[a-f0-9]{32}", tag) or
                data.get("room_id") != room_id):
            raise RoomError("Invalid restore recovery journal")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RoomError("Restore journal requires manual inspection") from exc

    # Never follow links or traverse outside the registered room directory.
    for scope in _SCOPE:
        previous = home / (".restore-previous-" + tag + "-" + scope)
        active = home / scope
        if previous.exists():
            if previous.is_symlink() or active.is_symlink():
                raise RoomError("Unsafe interrupted restore paths")
            if active.exists():
                discard = backups / (".restore-discard-" + tag + "-" + scope)
                if discard.exists():
                    _remove_tree(discard)
                os.replace(active, discard)
            os.replace(previous, active)
        if not active.is_dir():
            raise RoomError("Interrupted restore lost a mandatory directory")
    _schema(home / "data" / "serein.db")
    staged = backups / (".restore-stage-" + tag)
    _remove_tree(staged)
    for scope in _SCOPE:
        _remove_tree(backups / (".restore-discard-" + tag + "-" + scope))
    journal.unlink()
    manager._set_state(room_id, "stopped")
    return {"recovered": True, "room_id": room_id,
            "message": "Previous room data restored; start room explicitly"}


def recover_room_restore(manager, room_id, runtime):
    _, _, backups = _room_paths(manager, room_id)
    try:
        with exclusive_lock(backups / ".snapshot.lock"):
            room = manager.get(room_id)
            if runtime.status(manager, room).get("container") == "running":
                raise RoomError("Stop the room before interrupted restore recovery")
            return _recover_locked(manager, room_id)
    except RoomError:
        raise
    except RuntimeError as exc:
        if str(exc) == _LOCK_CONFLICT:
            raise RoomError("Room backup or restore is already running") from exc
        raise


def restore_room_backup(manager, room_id, name, runtime, *,
                        confirm_slug, expected_sha256):
    """Explicit restore with rollback archive and fail-closed crash journal.

    Restoration is never enabled by a preview alone. The old connection
    secrets remain in the target room (including a newly generated Gateway
    Key following VPS replacement). Only config and data are switched.
    """
    if (not isinstance(confirm_slug, str) or
            not isinstance(expected_sha256, str) or
            not _SHA.fullmatch(expected_sha256)):
        raise RoomError("Explicit room ID and archive SHA-256 confirmation required")
    _, home, backups = _room_paths(manager, room_id)
    try:
        with exclusive_lock(backups / ".snapshot.lock"):
            plan = _preflight(manager, room_id, name,
                              expected_sha256=expected_sha256)
            if confirm_slug != plan["room_slug"] or not plan["schema_compatible"]:
                raise RoomError("Room ID or database schema mismatch; restore denied")
            room = manager.get(room_id)
            if room["state"] not in ("ready", "stopped"):
                raise RoomError("Room must be healthy or stopped before restore")
            was_ready = room["state"] == "ready"
            if room["state"] == "stopped" and runtime.status(manager, room).get("container") == "running":
                raise RoomError("Stopped room container unexpectedly running")
            archive_path = backup_file(manager, room_id, name)
            tag = uuid4().hex
            staged = _stage(archive_path, home, tag)
            rollback = None
            committed = False
            try:
                if was_ready:
                    manager.stop(room_id, runtime)
                # The rollback is created while data is quiescent and before
                # *any* target data/config is replaced.
                rollback = _write_archive(manager, room_id)
                journal = _journal_file(home)
                _atomic_json(journal, {
                    "tag": tag, "room_id": room_id, "source_archive": name,
                    "rollback_archive": rollback["name"],
                })
                for scope in _SCOPE:
                    active = home / scope
                    previous = home / (".restore-previous-" + tag + "-" + scope)
                    if active.is_symlink() or previous.exists():
                        raise RoomError("Unsafe target directory for restore")
                    os.replace(active, previous)
                    os.replace(staged / scope, active)
                # The archive must still match the confirmed digest when
                # staging is complete; a concurrent file replacement cannot
                # be accepted merely because the first preflight passed.
                if _hash_file(archive_path) != expected_sha256:
                    raise RoomError("Backup changed during restore staging")
                # Force Docker to re-evaluate the same private volume paths;
                # do not remove its actual host data, old keys or backups.
                runtime.remove_stopped(manager, manager.get(room_id))
                if was_ready:
                    manager.start(room_id, runtime, _restore_in_progress=True)
                committed = True
                # Mark a successful restore BEFORE cleaning previous shells.
                # If the VPS crashes after this point the current data is
                # already healthy, and the permanent rollback archive exists.
                journal.unlink()
                for scope in _SCOPE:
                    _remove_tree(home / (".restore-previous-" + tag + "-" + scope))
                return {
                    "room_id": room_id, "room_slug": room["room_slug"],
                    "source_room_id": plan["source_room_id"],
                    "restored": True, "restarted": was_ready,
                    "rollback_backup": rollback["name"],
                    "gateway_key_unchanged": True,
                }
            except Exception:
                if not committed and _journal_file(home).exists():
                    # Stop any partially restarted container before touching
                    # its host-backed files. Never affect another room.
                    try:
                        runtime.stop(manager, manager.get(room_id))
                        runtime.remove_stopped(manager, manager.get(room_id))
                        _recover_locked(manager, room_id)
                    except Exception as recovery_error:
                        manager._set_state(room_id, "error", "restore_recovery_required")
                        raise RoomError("Restore interrupted; manual recovery required") from recovery_error
                elif was_ready:
                    manager._set_state(room_id, "stopped")
                raise
            finally:
                if staged.exists():
                    _remove_tree(staged)
    except RoomError:
        raise
    except RuntimeError as exc:
        if str(exc) == _LOCK_CONFLICT:
            raise RoomError("Room backup or restore is already running") from exc
        raise
