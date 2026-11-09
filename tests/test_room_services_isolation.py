"""Per-room original Serein task and MCP isolation tests.

No Docker/VPS credentials, no model calls; actual Serein application objects,
MCP tool registration and lifespan scheduling are used with synthetic stores.
"""
import asyncio
import json

import pytest

from serein.application import Application
from serein.api.mcp import create_server
from serein.config import Settings
from serein.core.store import Store
from serein.lifecycle import lifespan
from serein.room_manager import RoomManager


def test_mcp_tool_allowlist_is_per_room_and_denies_unlisted_calls(tmp_path):
    manager = RoomManager(tmp_path / "root")
    a_id = manager.create("xiaogi-main", source="blank",
                          room_slug="xiaogi-main")["room"]["room_id"]
    b_id = manager.create("other-main", source="blank",
                          room_slug="other-main")["room"]["room_id"]
    a_db = manager.room_dir(a_id) / "data" / "serein.db"
    b_db = manager.room_dir(b_id) / "data" / "serein.db"
    with Store(a_db) as store:
        store.create("scene-only-in-a", "scene", "A private scene", "ONLY_A_CAN_READ_THIS")
    with Store(b_db) as store:
        store.create("scene-only-in-b", "scene", "B private scene", "ONLY_B_CAN_READ_THIS")

    manager.set_mcp_permissions(a_id, ["read_memory"])
    manager.set_mcp_permissions(b_id, ["recall_memory"])
    def server(room_id, allowed):
        home = manager.room_dir(room_id)
        cfg = Settings(home / "data" / "serein.db",
                       home / "data" / "recall-index.sqlite",
                       writable=True, mcp_tools=allowed)
        return create_server(Application(cfg), http=True)
    mcp_a = server(a_id, ["read_memory"])
    mcp_b = server(b_id, ["recall_memory"])

    async def exercise():
        a_names = {tool.name for tool in await mcp_a.list_tools()}
        b_names = {tool.name for tool in await mcp_b.list_tools()}
        assert a_names == {"read_memory"}
        assert b_names == {"recall_memory"}
        with pytest.raises(Exception, match="Unknown tool"):
            await mcp_a.call_tool("recall_memory", {"query": "A"})
        with pytest.raises(Exception, match="Unknown tool"):
            await mcp_b.call_tool("read_memory", {"identifier": "scene-only-in-a"})
        read = await mcp_a.call_tool("read_memory", {"identifier": "scene-only-in-a"})
        assert "ONLY_A_CAN_READ_THIS" in str(read)
        unknown = await mcp_a.call_tool("read_memory", {"identifier": "scene-only-in-b"})
        assert "ONLY_B_CAN_READ_THIS" not in str(unknown)
    asyncio.run(exercise())
    assert manager.mcp_permissions(a_id)["tools"] == ["read_memory"]
    assert manager.mcp_permissions(b_id)["tools"] == ["recall_memory"]


def test_per_room_background_lifespans_are_distinct_and_cancel_independently(
        tmp_path, monkeypatch):
    """Auto-summary/model-task worker and requested-work receive per-room DB."""
    manager = RoomManager(tmp_path / "root")
    a_id = manager.create("A", source="blank")["room"]["room_id"]
    b_id = manager.create("B", source="blank")["room"]["room_id"]
    paths = {
        a_id: manager.room_dir(a_id) / "data" / "serein.db",
        b_id: manager.room_dir(b_id) / "data" / "serein.db",
    }
    a = Application(Settings(paths[a_id], writable=True))
    b = Application(Settings(paths[b_id], writable=True))
    started = set()
    cancelled = set()

    async def fake_worker(kind, database):
        identity = (kind, str(database))
        started.add(identity)
        try:
            await asyncio.Future()
        finally:
            cancelled.add(identity)

    async def fake_model_jobs(app):
        await fake_worker("configured-model-tasks", app.settings.database)

    async def fake_requested_work(settings):
        await fake_worker("requested-work", settings.database)

    monkeypatch.setattr("serein.model_jobs.run", fake_model_jobs)
    monkeypatch.setattr("serein.work_tasks.run", fake_requested_work)

    async def exercise():
        async with lifespan(a):
            await asyncio.sleep(0)
            assert ("configured-model-tasks", str(paths[a_id])) in started
            assert ("configured-model-tasks", str(paths[b_id])) not in started
            async with lifespan(b):
                await asyncio.sleep(0)
                for name in ("configured-model-tasks", "requested-work"):
                    assert (name, str(paths[a_id])) in started
                    assert (name, str(paths[b_id])) in started
                assert cancelled == set()
            for name in ("configured-model-tasks", "requested-work"):
                assert (name, str(paths[b_id])) in cancelled
                assert (name, str(paths[a_id])) not in cancelled
        assert cancelled == started

    asyncio.run(exercise())
    # No original room tables or background control state are shared.
    with Store(paths[a_id]) as store:
        store.conn.execute(
            "INSERT INTO background_state(name,value_json) VALUES (?,?)",
            ("memo-for-room-a", json.dumps({"content":"PRIVATE_A_MEMO"})),
        )
    with Store(paths[b_id]) as store:
        assert store.conn.execute(
            "SELECT 1 FROM background_state WHERE name=?",
            ("memo-for-room-a",),
        ).fetchone() is None
