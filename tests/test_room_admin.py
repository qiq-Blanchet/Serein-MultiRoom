"""M3 offline administrator control-plane tests. No private VPS or Docker access."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from serein.deployment import read_settings, save_settings
from serein.room_admin import admin_key_path, create_app, initialize_admin_key, read_admin_key
from serein.room_manager import RoomError, RoomManager


class FakeRuntime:
    def __init__(self):
        self.running = set()
        self.calls = []

    def status(self, manager, room):
        room_id = room["room_id"]
        return {
            "container": "running" if room_id in self.running else "exited",
            "health": "healthy" if room_id in self.running else "unknown",
        }

    def start(self, manager, room):
        self.calls.append(("start", room["room_id"]))
        self.running.add(room["room_id"])

    def wait_healthy(self, manager, room, *, timeout):
        return room["room_id"] in self.running

    def stop(self, manager, room):
        self.calls.append(("stop", room["room_id"]))
        self.running.discard(room["room_id"])

    def remove_stopped(self, manager, room):
        assert room["room_id"] not in self.running
        self.calls.append(("recreate", room["room_id"]))


@pytest.fixture
def console(tmp_path):
    manager = RoomManager(tmp_path / "control")
    path = initialize_admin_key(manager)
    key = read_admin_key(manager)
    runtime = FakeRuntime()
    app = create_app(manager, admin_key=key, runtime=runtime)
    client = TestClient(app)
    return client, manager, key, runtime, path


def auth(key):
    return {"Authorization": "Bearer " + key}


def test_admin_key_is_private_and_independent_of_gateway_key(console):
    client, manager, key, _, path = console
    assert path == admin_key_path(manager)
    assert len(key) >= 48
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    assert initialize_admin_key(manager) == path
    assert read_admin_key(manager) == key
    created = manager.create("A", source="blank")
    assert created["gateway_key"] != key
    assert client.get("/api/rooms", headers=auth(created["gateway_key"])).status_code == 401
    assert client.get("/api/rooms", headers=auth(key)).status_code == 200


def test_admin_api_requires_admin_secret_and_disallows_cross_origin_write(console):
    client, manager, key, runtime, _ = console
    assert client.get("/api/rooms").status_code == 401
    assert client.post("/api/rooms", json={"name": "A", "source": "blank"}).status_code == 401
    assert client.get("/api/rooms", headers=auth("synthetic-unauthorized")).status_code == 401
    response = client.post("/api/rooms", headers={
        **auth(key), "Origin": "https://hostile.invalid",
        "Host": "testserver",
    }, json={"name": "Nope", "source": "blank"})
    assert response.status_code == 403
    denied = client.post("/api/rooms", headers={
        **auth(key), "Sec-Fetch-Site": "cross-site",
    }, json={"name": "Nope", "source": "blank"})
    assert denied.status_code == 403
    assert manager.list_rooms() == []
    assert not runtime.calls


def test_web_create_default_clone_preserves_only_configuration(console):
    client, manager, key, runtime, _ = console
    first = client.post("/api/rooms", headers=auth(key),
                        json={"name": "First", "source": "blank"})
    assert first.status_code == 201, first.text
    data = first.json()
    a_id = data["room"]["room_id"]
    assert data["room"]["state"] == "stopped"
    assert data["gateway_key"]
    source_db = manager.room_dir(a_id) / "data" / "serein.db"
    save_settings(source_db, {
        "features": {"memos": True, "dream_read": True},
        "upstreams": [{
            "name": "provider", "base_url": "https://test.invalid/v1",
            "api_key": "SYNTHETIC_PRIVATE_PROVIDER_KEY", "models": ["model-a"],
        }],
        "dream": {"main_prompt": "PRIVATE_DREAM_PROMPT"},
        "identity": {"ai_name": "PRIVATE_AI"},
    })
    second = client.post("/api/rooms", headers=auth(key),
                         json={"name": "Second"})
    assert second.status_code == 201, second.text
    b_id = second.json()["room"]["room_id"]
    assert second.json()["room"]["source_room_id"] == a_id
    dest_db = manager.room_dir(b_id) / "data" / "serein.db"
    state = read_settings(dest_db)
    assert state["features"]["memos"] and state["features"]["dream_read"]
    assert state["upstreams"][0]["models"] == ["model-a"]
    assert not state["upstreams"][0].get("api_key")
    assert state["dream"]["main_prompt"] == ""
    assert state["identity"]["ai_name"] == "AI"

    listing = client.get("/api/rooms", headers=auth(key))
    assert listing.status_code == 200
    assert len(listing.json()["rooms"]) == 2
    serialized = listing.text
    for sensitive in (data["gateway_key"], second.json()["gateway_key"],
                      "SYNTHETIC_PRIVATE_PROVIDER_KEY", "PRIVATE_DREAM_PROMPT", key):
        assert sensitive not in serialized
    assert client.get("/api/rooms/" + a_id, headers=auth(key)).status_code == 200


def test_explicit_provider_credential_clone_and_blank_room(console):
    client, manager, key, _, _ = console
    first = manager.create("A", source="blank")["room"]["room_id"]
    db = manager.room_dir(first) / "data" / "serein.db"
    save_settings(db, {"upstreams": [{
        "name": "provider", "base_url": "https://test.invalid/v1",
        "api_key": "OPT_IN_PRIVATE_PROVIDER_KEY", "models": ["model-a"],
    }]})
    cloned = client.post("/api/rooms", headers=auth(key), json={
        "name": "Explicit",
        "source": first,
        "reuse_provider_keys": True,
    })
    assert cloned.status_code == 201, cloned.text
    state = read_settings(manager.room_dir(cloned.json()["room"]["room_id"]) /
                          "data" / "serein.db")
    assert state["upstreams"][0]["api_key"] == "OPT_IN_PRIVATE_PROVIDER_KEY"
    blank = client.post("/api/rooms", headers=auth(key), json={
        "name": "Clean", "source": "blank",
    })
    state_blank = read_settings(manager.room_dir(blank.json()["room"]["room_id"]) /
                                "data" / "serein.db")
    assert state_blank["upstreams"] == []


def test_web_start_and_stop_change_only_target_room_routes(console):
    client, manager, key, runtime, _ = console
    a = manager.create("A", source="blank")
    b = manager.create("B", source="blank")
    a_id = a["room"]["room_id"]
    b_id = b["room"]["room_id"]
    response = client.post("/api/rooms/" + a_id + "/start", headers=auth(key))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ready"
    assert manager.resolve_gateway_key(a["gateway_key"]) == a_id
    assert manager.resolve_gateway_key(b["gateway_key"]) is None
    assert ("start", a_id) in runtime.calls
    response = client.post("/api/rooms/" + a_id + "/stop", headers=auth(key))
    assert response.status_code == 200
    assert manager.resolve_gateway_key(a["gateway_key"]) is None
    assert ("stop", a_id) in runtime.calls
    assert manager.get(b_id)["state"] == "stopped"


def test_invalid_source_and_unknown_admin_route_fail_without_mutation(console):
    client, manager, key, runtime, _ = console
    assert client.post("/api/rooms", headers=auth(key), json={
        "name": "Invalid", "source": "../../other",
    }).status_code == 400
    assert client.post("/api/rooms", headers=auth(key), json={
        "name": "Invalid", "source": "blank", "rogue": "unexpected",
    }).status_code == 422
    assert client.get("/api/rooms/not-a-valid-room", headers=auth(key)).status_code == 404
    assert client.post("/api/rooms/not-a-valid-room/start",
                       headers=auth(key)).status_code == 409
    assert manager.list_rooms() == []
    assert runtime.calls == []


def test_dashboard_has_no_secret_or_private_room_data_in_html(console):
    client, manager, key, runtime, _ = console
    manager.create("PRIVATE_ROOM_NAME", source="blank")
    page = client.get("/")
    script = client.get("/rooms-ui.js")
    assert page.status_code == script.status_code == 200
    assert "Multi-Room" in page.text
    assert "梦境" in page.text and "心绪" in page.text and "备忘" in page.text
    assert "PRIVATE_ROOM_NAME" not in page.text
    assert key not in page.text and key not in script.text
    assert "localStorage" not in script.text.replace("no cookies or localStorage", "")
    assert "textContent" in script.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert script.headers["x-content-type-options"] == "nosniff"


def test_cli_admin_init_never_prints_admin_secret(tmp_path):
    root = tmp_path / "installation"
    script = Path(__file__).resolve().parents[1] / "scripts" / "rooms.py"
    cmd = [sys.executable, str(script), "--root", str(root), "admin-init"]
    one = subprocess.run(cmd, capture_output=True, text=True, check=True)
    two = subprocess.run(cmd, capture_output=True, text=True, check=True)
    expected = root / "control" / "secrets" / "admin-key"
    key = expected.read_text().strip()
    assert key and key not in one.stdout + two.stdout
    assert json.loads(one.stdout)["admin_key_file"] == str(expected)
    assert json.loads(two.stdout)["admin_key_file"] == str(expected)
    assert "room Gateway Key" in one.stdout


def test_room_id_creation_uses_existing_appearance_ai_name(console):
    client, manager, key, _, _ = console
    response = client.post("/api/rooms", headers=auth(key), json={
        "name": "xiaogi-main", "room_slug": "xiaogi-main", "source": "blank",
    })
    assert response.status_code == 201, response.text
    room = response.json()["room"]
    assert room["room_slug"] == "xiaogi-main"
    assert response.json()["gateway_key"]
    uuid = room["room_id"]
    listings = client.get("/api/rooms", headers=auth(key)).json()["rooms"]
    assert listings[0]["display_name"] == "xiaogi-main"
    db = manager.room_dir(uuid) / "data" / "serein.db"
    save_settings(db, {"identity": {"ai_name": "小玑"}})
    listings = client.get("/api/rooms", headers=auth(key)).json()["rooms"]
    assert listings[0]["display_name"] == "小玑"
    assert listings[0]["room_slug"] == "xiaogi-main"
    assert "gateway_key" not in listings[0]
    duplicate = client.post("/api/rooms", headers=auth(key), json={
        "name": "Another AI", "room_slug": "xiaogi-main", "source": "blank",
    })
    assert duplicate.status_code == 400
    assert len(manager.list_rooms()) == 1


def test_mcp_permission_editor_is_room_scoped_and_requires_admin(console):
    client, manager, key, runtime, _ = console
    a = manager.create("A", source="blank")["room"]["room_id"]
    b = manager.create("B", source="blank")["room"]["room_id"]
    path = f"/api/rooms/{a}/mcp-tools"
    assert client.get(path).status_code == 401
    assert client.post(path, json={"mode": "custom", "tools": []}).status_code == 401
    assert client.get(path, headers=auth(key)).json()["mode"] == "original"
    original_b = client.get(f"/api/rooms/{b}/mcp-tools", headers=auth(key)).json()
    assert original_b["mode"] == "original"
    assert "read_memory" in original_b["available"]

    denied = client.post(path, headers=auth(key),
                         json={"mode": "custom", "tools": ["unknown_tool"]})
    assert denied.status_code == 409
    assert not runtime.calls
    original = client.post(path, headers={**auth(key), "Origin": "https://not-ours.invalid"},
                           json={"mode": "custom", "tools": ["read_memory"]})
    assert original.status_code == 403

    # Saving a stopped room must not start it or alter other rooms.
    result = client.post(path, headers=auth(key),
                         json={"mode": "custom", "tools": ["read_memory"]})
    assert result.status_code == 200, result.text
    assert result.json()["permissions"]["tools"] == ["read_memory"]
    assert ("recreate", a) in runtime.calls
    assert manager.get(a)["state"] == "stopped"
    assert manager.mcp_permissions(b)["mode"] == "original"

    manager.start(a, runtime)
    result = client.post(path, headers=auth(key),
                         json={"mode": "custom", "tools": []})
    assert result.status_code == 200, result.text
    assert result.json()["restarted"] is True
    assert manager.get(a)["state"] == "ready"
    assert manager.get(b)["state"] == "stopped"
    assert manager.mcp_permissions(a)["tools"] == []
    assert manager.mcp_permissions(b)["mode"] == "original"

    restored = client.post(path, headers=auth(key),
                           json={"mode": "original", "tools": []})
    assert restored.status_code == 200
    assert manager.mcp_permissions(a)["mode"] == "original"
    assert manager.mcp_permissions(b)["mode"] == "original"


def test_full_room_backup_web_admin_api_is_room_scoped_and_does_not_show_keys(console):
    client, manager, key, runtime, _ = console
    a = manager.create("A", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    b = manager.create("B", source="blank", room_slug="other-main")["room"]["room_id"]
    with __import__("sqlite3").connect(manager.room_dir(a) / "data" / "serein.db") as db:
        db.execute("INSERT INTO background_state(name,value_json) VALUES (?,?)",
                   ("private-memo", '{"memo":"SECRET_FOR_A_ONLY"}'))
        db.commit()

    endpoint = f"/api/rooms/{a}/backups"
    assert client.get(endpoint).status_code == 401
    assert client.post(endpoint).status_code == 401
    assert client.get(endpoint, headers=auth(key)).json()["items"] == []

    manager.start(a, runtime)
    response = client.post(endpoint, headers=auth(key))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["room_id"] == a
    assert data["restarted"] is True
    assert "gateway_key" not in str(data)
    assert ("stop", a) in runtime.calls
    assert ("start", a) in runtime.calls
    assert manager.get(a)["state"] == "ready"
    assert manager.get(b)["state"] == "stopped"

    items = client.get(endpoint, headers=auth(key)).json()["items"]
    assert len(items) == 1
    filename = items[0]["name"]
    assert filename == data["name"]
    preview = client.get(endpoint + "/" + filename + "/preview", headers=auth(key))
    assert preview.status_code == 200, preview.text
    assert preview.json()["verified"] is True
    assert preview.json()["restore_available"] is False
    downloaded = client.get(endpoint + "/" + filename + "/download", headers=auth(key))
    assert downloaded.status_code == 200, downloaded.text[:160]
    assert downloaded.content[:2] == b"\x1f\x8b"
    assert filename in downloaded.headers["content-disposition"]
    assert "gateway_key" not in downloaded.headers["content-disposition"]

    assert client.get(f"/api/rooms/{b}/backups/{filename}/download",
                      headers=auth(key)).status_code == 404
    assert client.get(f"/api/rooms/{b}/backups/{filename}/preview",
                      headers=auth(key)).status_code == 409
    assert client.post(endpoint,headers={**auth(key),"Origin":"https://foreign.invalid"}).status_code == 403



def test_restore_requires_room_id_and_sha_with_same_dashboard_admin(console):
    import json
    from serein.core.store import Store
    client, manager, key, runtime, _ = console
    a = manager.create("A", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    b = manager.create("B", source="blank", room_slug="other-main")["room"]["room_id"]
    db_a = manager.room_dir(a) / "data" / "serein.db"
    db_b = manager.room_dir(b) / "data" / "serein.db"
    with Store(db_a) as store:
        store.create("old-scene", "scene", "Before", "BEFORE RESTORE")
    with Store(db_b) as store:
        store.create("other-scene", "scene", "Other", "B PRIVATE")
    manager.start(a, runtime)
    base = f"/api/rooms/{a}/backups"
    created = client.post(base, headers=auth(key))
    assert created.status_code == 201
    name = created.json()["name"]
    with Store(db_a) as store:
        store.create("new-scene", "scene", "After", "AFTER BACKUP")
    target = base + "/" + name
    assert client.get(target + "/restore-plan").status_code == 401
    plan = client.get(target + "/restore-plan", headers=auth(key))
    assert plan.status_code == 200
    assert plan.json()["restore_available"] is True
    sha = plan.json()["archive_sha256"]
    assert client.post(target + "/restore", json={
        "confirm_room_id": "xiaogi-main", "expected_sha256": sha,
    }).status_code == 401
    assert client.post(target + "/restore", headers=auth(key), json={
        "confirm_room_id": "other-main", "expected_sha256": sha,
    }).status_code == 409
    assert client.post(target + "/restore", headers=auth(key), json={
        "confirm_room_id": "xiaogi-main", "expected_sha256": "f" * 64,
    }).status_code == 409
    result = client.post(target + "/restore", headers=auth(key), json={
        "confirm_room_id": "xiaogi-main", "expected_sha256": sha,
    })
    assert result.status_code == 200, result.text
    assert result.json()["gateway_key_unchanged"] is True
    assert result.json()["rollback_backup"] != name
    with Store(db_a) as store:
        ids = {r[0] for r in store.conn.execute("SELECT id FROM documents")}
        assert "old-scene" in ids and "new-scene" not in ids
    with Store(db_b) as store:
        assert store.conn.execute(
            "SELECT id FROM documents WHERE id='other-scene'"
        ).fetchone()
    assert manager.get(a)["state"] == "ready"
    assert manager.get(b)["state"] == "stopped"


def test_streamed_room_backup_upload_is_non_destructive_and_rejects_foreign_room(console):
    from serein.room_archive import backup_file
    from serein.core.store import Store
    client, manager, key, runtime, _ = console
    a = manager.create("A", source="blank", room_slug="xiaogi-main")["room"]["room_id"]
    b = manager.create("B", source="blank", room_slug="other-main")["room"]["room_id"]
    with Store(manager.room_dir(a) / "data" / "serein.db") as store:
        store.create("scene-a", "scene", "Synthetic", "A_PRIVATE")
    created = client.post(f"/api/rooms/{a}/backups", headers=auth(key))
    assert created.status_code == 201
    archive = backup_file(manager, a, created.json()["name"]).read_bytes()
    url = f"/api/rooms/{a}/backups/upload"
    content_type = {"Content-Type": "application/gzip"}
    assert client.post(url, headers=content_type, content=archive).status_code == 401
    response = client.post(url, headers={**auth(key), **content_type}, content=archive)
    assert response.status_code == 201, response.text
    assert response.json()["room_slug"] == "xiaogi-main"
    assert response.json()["verified"] is True
    foreign = client.post(f"/api/rooms/{b}/backups/upload",
                          headers={**auth(key), **content_type}, content=archive)
    assert foreign.status_code == 409
    assert manager.get(a)["state"] == "stopped"
    assert manager.get(b)["state"] == "stopped"
    with Store(manager.room_dir(a) / "data" / "serein.db") as store:
        assert store.conn.execute("SELECT id FROM documents WHERE id='scene-a'").fetchone()
