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


def test_dream_mood_memo_and_summary_feature_state_stays_per_room(tmp_path):
    """Feature switches and generated task state remain in their own stores."""
    from serein.deployment import read_settings, save_settings

    manager = RoomManager(tmp_path / "rooms")
    a_id = manager.create("A", source="blank")["room"]["room_id"]
    b_id = manager.create("B", source="blank")["room"]["room_id"]
    a_db = manager.room_dir(a_id) / "data" / "serein.db"
    b_db = manager.room_dir(b_id) / "data" / "serein.db"
    save_settings(a_db, {
        "features": {
            "memos": True, "persona": True,
            "dream_read": True, "dream_morning": True,
        },
        "pipeline": {"auto_enabled": False},
    })
    a_settings = read_settings(a_db)
    b_settings = read_settings(b_db)
    for feature in ("memos", "persona", "dream_read", "dream_morning"):
        assert a_settings["features"][feature] is True
        assert b_settings["features"][feature] is False
    assert a_settings["pipeline"]["auto_enabled"] is False
    assert b_settings["pipeline"]["auto_enabled"] is True

    names = ("dream-generated", "mood-generated", "memo-generated",
             "summary-task-progress")
    with Store(a_db) as store:
        for name in names:
            store.conn.execute(
                "INSERT INTO background_state(name,value_json) VALUES (?,?)",
                (name, json.dumps({"content": "PRIVATE_A_" + name})),
            )
    with Store(b_db) as store:
        for name in names:
            assert store.conn.execute(
                "SELECT 1 FROM background_state WHERE name=?", (name,)
            ).fetchone() is None
        store.conn.execute(
            "INSERT INTO background_state(name,value_json) VALUES (?,?)",
            ("memo-generated", json.dumps({"content": "PRIVATE_B"})),
        )
    with Store(a_db) as store:
        value = store.conn.execute(
            "SELECT value_json FROM background_state WHERE name=?",
            ("memo-generated",),
        ).fetchone()[0]
        assert "PRIVATE_B" not in value and "PRIVATE_A_" in value


def test_real_summary_pipeline_keeps_auto_toggle_and_settled_events_in_their_room(tmp_path):
    """Exercise the real Event summarizer with fake model outputs, no HTTP/API key.

    This is stronger than merely checking that different worker objects receive
    different database paths: the persistent raw/events/processed tables are
    actually updated, and a paused room must not inherit the other room's work.
    """
    from serein.compat.raw_archive import raw_archive
    from serein.deployment import save_settings
    from serein.extensions import pipeline
    from test_public_features import synthetic_runner

    manager = RoomManager(tmp_path / "isolated-background")
    first = manager.create("First AI", source="blank")["room"]["room_id"]
    second = manager.create("Second AI", source="blank")["room"]["room_id"]
    db_a = manager.room_dir(first) / "data" / "serein.db"
    db_b = manager.room_dir(second) / "data" / "serein.db"
    save_settings(db_a, {"pipeline": {"auto_enabled": False}})
    save_settings(db_b, {"pipeline": {"auto_enabled": True}})

    for db, label in ((db_a, "ALPHA"), (db_b, "BETA")):
        instance = Settings(db, writable=True)
        result = raw_archive(instance).ingest([
            {"source_event_id": "m6-user", "session_id": "m6-summary",
             "role": "user", "text": f"M6_PRIVATE_{label}_BOOKCLUB",
             "created_at": "2025-01-02T10:00:00Z"},
            {"source_event_id": "m6-assistant", "session_id": "m6-summary",
             "role": "assistant", "text": f"M6_PRIVATE_{label}_AGREEMENT",
             "created_at": "2025-01-02T10:01:00Z"},
        ], source="m6-synthetic")
        assert result["inserted"] == 2

    assert asyncio.run(pipeline.scheduled_advance(db_a))["status"] == "auto_paused"
    with Store(db_a, read_only=True) as store:
        # A paused fresh room may not have initialized the pipeline schema at
        # all; both absent tables and empty initialized tables are safe.
        for table in ("raw_processing", "pipeline_batches"):
            present = store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if present:
                assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    # Running the actual summarization contract in B creates B-only Event
    # evidence and marks B's original messages processed. No model API called.
    completed_b = asyncio.run(pipeline.advance(
        db_b, include_recent=True, runner=synthetic_runner))
    assert completed_b["events"] == 1, completed_b

    def snapshot(db):
        with Store(db, read_only=True) as store:
            rows = store.conn.execute(
                "SELECT id FROM documents WHERE kind='event' AND lifecycle='active'"
            ).fetchall()
            processing_exists = store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_processing'"
            ).fetchone()
            originals = (store.conn.execute(
                "SELECT count(*) FROM raw_processing"
            ).fetchone()[0] if processing_exists else 0)
            summaries = [str(store.read(row["id"])) for row in rows]
            return summaries, originals

    b_summaries, b_processed = snapshot(db_b)
    a_summaries, a_processed = snapshot(db_a)
    assert b_processed == 2 and len(b_summaries) == 1
    assert "M6_PRIVATE_BETA" in b_summaries[0]
    assert "M6_PRIVATE_ALPHA" not in b_summaries[0]
    assert a_summaries == [] and a_processed == 0

    # Enable the first room, then actually settle its own original pair.
    save_settings(db_a, {"pipeline": {"auto_enabled": True}})
    with Store(db_a, read_only=True) as store:
        original_rows = store.conn.execute(
            "SELECT * FROM raw_events ORDER BY id"
        ).fetchall()
        original_units = pipeline.dialogue_units(
            [pipeline.task_message(row) for row in original_rows]
        )
    assert len(original_rows) == 2, [dict(row) for row in original_rows]
    assert len(original_units) == 1 and pipeline.dialogue_unit_is_complete(
        original_units[0]
    ), original_units
    pipeline.initialize(db_a)
    first_batch = pipeline.new_batch(db_a, include_recent=True)
    assert first_batch is not None, {
        "raw_rows": [(row["source"], row["session_id"], row["role"],
                      row["created_at"]) for row in original_rows],
        "unit_count": len(original_units),
        "pipeline_policy": pipeline.read_settings(db_a)["pipeline"],
    }
    completed_a = asyncio.run(pipeline.advance(
        db_a, include_recent=True, runner=synthetic_runner))
    assert completed_a.get("events") == 1, completed_a
    a_summaries, a_processed = snapshot(db_a)
    assert a_processed == 2 and len(a_summaries) == 1
    assert "M6_PRIVATE_ALPHA" in a_summaries[0]
    assert "M6_PRIVATE_BETA" not in a_summaries[0]
    assert snapshot(db_b) == (b_summaries, b_processed)

    # Restarting an already-completed pipeline pass must not summarize again.
    assert asyncio.run(pipeline.advance(
        db_b, include_recent=True, runner=synthetic_runner))["status"] == "current"
    assert snapshot(db_a) == (a_summaries, a_processed)
    assert snapshot(db_b) == (b_summaries, b_processed)
