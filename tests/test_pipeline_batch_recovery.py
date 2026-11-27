"""Explicit failed-batch recovery against synthetic archives only."""
import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from test_public_features import settings, synthetic_runner, output_for
from serein.api.http import create_app
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store, encode, Conflict
from serein.deployment import save_settings, read_settings
from serein.extensions import pipeline as p, pipeline_recovery as recovery


def short_output(role, request):
    if role == 'event_writer':
        request = {**request, 'messages': [{**m, 'content': m['content'][:60]} for m in request['messages']]}
    return output_for(role, request)


async def short_runner(role, request):
    return short_output(role, request)


def seed(settings, start=0, count=4, size=2000, session='failed', tail=False):
    rows = [{'source_event_id': f'{session}-{i}', 'session_id': session,
             'role': 'user' if i % 2 == 0 else 'assistant', 'text': str(i % 10)*size,
             'created_at': f'2025-01-01T00:{i:02}:00Z', 'metadata': {'timestamp_source': 'import_time', 'runtime': session}}
            for i in range(start, start+count*2+int(tail))]
    raw_archive(settings).ingest(rows, source='synthetic')


def failed(settings, *, tail=False):
    save_settings(settings.database, {'pipeline': {'max_input_chars': 100000}})
    seed(settings, tail=tail)
    async def broken(role, request):
        if role == 'event_curator':
            raise OSError('Synthetic failed batch')
        return output_for(role, request)
    for _ in range(2):
        with pytest.raises(OSError):
            asyncio.run(p.advance(settings.database, include_recent=True, runner=broken))
    result = asyncio.run(p.advance(settings.database, include_recent=True, runner=broken))
    assert result['status'] == 'retained'
    return result['batch_id']


def rows(settings, table):
    with Store(settings.database, read_only=True) as store:
        return [tuple(row) for row in store.conn.execute('SELECT * FROM '+table+' ORDER BY rowid')]


def test_skip_preserves_sources_jobs_events_and_allows_later_same_chat(settings):
    seed(settings, count=1, session='published')
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))['events'] == 1
    existing = rows(settings, 'fact_events')
    batch_id = failed(settings, tail=True)
    jobs = rows(settings, 'pipeline_jobs')
    attempts = rows(settings, 'pipeline_attempts')
    originals = rows(settings, 'raw_events')
    preview = recovery.recovery_preview(settings.database, batch_id)
    assert preview['originals'] == 8 and preview['original_chars'] == 16000
    assert rows(settings, 'raw_events') == originals
    result = asyncio.run(recovery.recover_batch(settings.database, batch_id, 'skip', 'SKIP_PIPELINE_BATCH'))
    assert result['skipped_originals'] == 8
    assert rows(settings, 'raw_events') == originals
    assert rows(settings, 'pipeline_jobs') == jobs
    assert rows(settings, 'pipeline_attempts') == attempts
    assert rows(settings, 'fact_events') == existing
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM raw_processing WHERE outcome='manual_skip'").fetchone()[0] == 8
        assert store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=11').fetchone() is None
    # The pending user tail was excluded from skipping and still pairs with its reply.
    raw_archive(settings).ingest([{'source_event_id': 'failed-9', 'session_id': 'failed', 'role': 'assistant',
        'text': 'A new complete answer', 'created_at': '2025-01-01T00:09:00Z', 'metadata': {'runtime': 'failed'}}], source='synthetic')
    later = asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))
    assert later['status'] == 'processed' and later['processed_originals'] == 2
    with pytest.raises(Conflict):
        asyncio.run(recovery.recover_batch(settings.database, batch_id, 'skip', 'SKIP_PIPELINE_BATCH'))


def test_rebatch_freezes_smaller_scoped_inputs_and_loads_prefix_state_in_order(settings):
    batch_id = failed(settings)
    jobs = rows(settings, 'pipeline_jobs')
    originals = rows(settings, 'raw_events')
    seed(settings, start=8, count=1, size=30)
    preview = recovery.recovery_preview(settings.database, batch_id, 5000)
    assert preview['batches'] == 4 and preview['batch_chars'] == [4000]*4
    result = asyncio.run(recovery.recover_batch(settings.database, batch_id, 'rebatch', 'REBATCH_PIPELINE_BATCH', 5000))
    assert result['batches'] == 4
    assert rows(settings, 'pipeline_jobs') == jobs
    assert rows(settings, 'raw_events')[:8] == originals
    assert read_settings(settings.database)['pipeline']['max_input_chars'] == 100000
    calls = []
    async def run(role, request):
        if role == 'track_router':
            calls.append(request)
            assert max((key for card in request['active_tracks'] for key in card.get('recent_source_message_ids', [])), default=0) < request['messages'][0]['id']
        return short_output(role, request)
    for index, key in enumerate(result['batch_ids']):
        selected = p.new_batch(settings.database, True)
        assert selected['id'] == key
        data = json.loads(selected['input_json'])
        assert data['input_policy']['max_input_chars'] == 5000
        assert data['input_policy']['curator_summary_first'] is True
        assert [m['id'] for m in data['messages']] == [index*2+1, index*2+2]
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=run))['status'] == 'processed'
    assert len(calls) == 4 and calls[1]['active_tracks']
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id<=8').fetchone()[0] == 8
        assert store.conn.execute('SELECT count(*) FROM raw_processing WHERE raw_id>8').fetchone()[0] == 0
    assert asyncio.run(p.advance(settings.database, include_recent=True, runner=run))['processed_originals'] == 2


def test_single_oversized_exchange_cannot_pretend_to_be_rebatched(settings):
    save_settings(settings.database, {'pipeline': {'max_input_chars': 100000}})
    seed(settings, count=1, size=28630)
    p.initialize(settings.database)
    batch = p.new_batch(settings.database, True)
    preview = recovery.recovery_preview(settings.database, batch['id'], 4000)
    assert preview['batches'] == 1 and preview['oversized_batches'] == 1 and not preview['can_rebatch']
    before = rows(settings, 'pipeline_batches')
    with pytest.raises(ValueError, match='无法把本批拆成'):
        asyncio.run(recovery.recover_batch(settings.database, batch['id'], 'rebatch', 'REBATCH_PIPELINE_BATCH', 4000))
    assert rows(settings, 'pipeline_batches') == before
    assert rows(settings, 'raw_processing') == []


def test_rebatching_a_child_keeps_its_replacements_before_existing_later_children(settings):
    batch_id = failed(settings)
    first = asyncio.run(recovery.recover_batch(settings.database, batch_id, 'rebatch', 'REBATCH_PIPELINE_BATCH', 9000))
    assert first['batches'] == 2
    second = asyncio.run(recovery.recover_batch(settings.database, first['batch_ids'][0], 'rebatch', 'REBATCH_PIPELINE_BATCH', 5000))
    for key in [*second['batch_ids'], first['batch_ids'][1]]:
        assert p.new_batch(settings.database, True)['id'] == key
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))['status'] == 'processed'


def test_recovery_auth_confirmation_limits_and_running_lease(settings):
    batch_id = failed(settings)
    client = TestClient(create_app(settings, token='synthetic', live=True))
    body = {'batch_id': batch_id, 'confirm': 'SKIP_PIPELINE_BATCH'}
    assert client.post('/v1/pipeline/skip-batch', json=body).status_code == 401
    client.headers['Authorization'] = 'Bearer synthetic'
    assert client.post('/v1/pipeline/recovery-preview', json={'batch_id': batch_id}).json()['originals'] == 8
    assert client.post('/v1/pipeline/skip-batch', json={'batch_id': batch_id}).status_code == 400
    for limit in (True, 0, 100001, '5000'):
        assert client.post('/v1/pipeline/rebatch', json={'batch_id': batch_id, 'confirm': 'REBATCH_PIPELINE_BATCH', 'max_input_chars': limit}).status_code == 400
    with Store(settings.database) as store:
        store.conn.execute("UPDATE background_state SET value_json=? WHERE name='work:pipeline'",
                           (encode({'id': 'pipeline', 'status': 'running', 'run_id': 'other', 'lease_until': time.time()+60}),))
    assert client.post('/v1/pipeline/skip-batch', json=body).json()['status'] == 'busy'
    assert rows(settings, 'raw_processing') == []


@pytest.mark.parametrize('damage', ['changed', 'processed', 'later'])
def test_recovery_refuses_changed_processed_or_non_head_batch(settings, damage):
    batch_id = failed(settings)
    with Store(settings.database) as store:
        if damage == 'changed':
            store.conn.execute("UPDATE raw_events SET text='changed' WHERE id=1")
        elif damage == 'processed':
            store.conn.execute("INSERT INTO raw_processing VALUES (1,'other','settled')")
        else:
            store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) SELECT 'later',scope,input_json,'pending' FROM pipeline_batches WHERE id=?", (batch_id,))
            batch_id = 'later'
    before = rows(settings, 'pipeline_batches')
    with pytest.raises(Conflict):
        asyncio.run(recovery.recover_batch(settings.database, batch_id, 'skip', 'SKIP_PIPELINE_BATCH'))
    assert rows(settings, 'pipeline_batches') == before


@pytest.mark.parametrize('action', ['skip', 'rebatch'])
def test_daytime_router_failure_can_skip_or_start_smaller_event_batches(settings, monkeypatch, action):
    from test_route_scope_holds import api, Clock
    Clock.value = datetime(2026, 10, 10, 12, 0, tzinfo=p.TZ)
    seed(settings, count=6)
    def broken(request):
        raise OSError('Synthetic routing outage')
    api(settings, monkeypatch, broken)
    for _ in range(3):
        asyncio.run(p.flush_routes(settings.database))
        Clock.value += timedelta(minutes=11)
    with Store(settings.database, read_only=True) as store:
        batch_id = store.conn.execute("SELECT id FROM pipeline_batches WHERE status='waiting_materials'").fetchone()[0]
    assert batch_id.startswith('route:')
    assert recovery.recovery_preview(settings.database, batch_id, 5000)['originals'] == 12
    if action == 'skip':
        result = asyncio.run(recovery.recover_batch(settings.database, batch_id, action, 'SKIP_PIPELINE_BATCH'))
        assert result['skipped_originals'] == 12
        seed(settings, start=12, count=1, size=30)
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))['processed_originals'] == 2
    else:
        result = asyncio.run(recovery.recover_batch(settings.database, batch_id, action, 'REBATCH_PIPELINE_BATCH', 5000))
        assert result['batches'] == 6 and all(key.startswith('pipeline:') for key in result['batch_ids'])
        assert asyncio.run(p.advance(settings.database, include_recent=True, runner=short_runner))['processed_originals'] == 2


def test_skip_a_completed_batch_with_retained_material(settings):
    from test_pipeline_pending import retained
    item = retained(settings)
    client = TestClient(create_app(settings, token='synthetic', live=True), headers={'Authorization':'Bearer synthetic'})
    before = {table: rows(settings, table) for table in ('raw_events','fact_events','pipeline_jobs','pipeline_attempts','pipeline_routes','pipeline_route_provenance')}
    assert client.get('/v1/pipeline/status').json()['pending_materials'][0]['can_skip']
    preview = client.post('/v1/pipeline/recovery-preview',json={'batch_id':item['batch_id'],'action':'skip'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['originals'] == 2 and preview.json()['already_processed'] == 0
    result = client.post('/v1/pipeline/skip-batch',json={'batch_id':item['batch_id'],'confirm':'SKIP_PIPELINE_BATCH'})
    assert result.status_code == 200, result.text
    assert result.json()['skipped_originals'] == 2
    assert client.get('/v1/pipeline/status').json()['pending_materials'][0]['status'] == 'resolved_skipped'
    assert all(rows(settings, table) == previous for table,previous in before.items())
    assert len(rows(settings,'raw_processing')) == 2
    assert all(row[2]=='manual_skip' for row in rows(settings,'raw_processing'))
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))['status']=='current'


@pytest.mark.parametrize(('stale_status','pending_status'),[('done','open'),('needs_repair','open'),('needs_repair','running')])
def test_skip_already_settled_pending_closes_only_the_stale_record(settings,stale_status,pending_status):
    from test_pipeline_pending import retained
    from serein.extensions import pipeline_pending as pending
    item=retained(settings)
    pending.resume(settings.database,item['id'])
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=synthetic_runner))['events']==1
    with Store(settings.database) as store:
        batch_id=store.conn.execute('SELECT batch_id FROM pipeline_pending_materials WHERE id=?',(item['id'],)).fetchone()[0]
        store.conn.execute('UPDATE pipeline_pending_materials SET status=? WHERE id=?',(pending_status,item['id']))
        store.conn.execute('UPDATE pipeline_batches SET status=? WHERE id=?',(stale_status,batch_id))
    before={table:rows(settings,table) for table in ('raw_events','raw_processing','fact_events','pipeline_jobs','pipeline_attempts','pipeline_routes','pipeline_route_provenance','fact_event_settlement_operations')}
    client=TestClient(create_app(settings,token='synthetic',live=True),headers={'Authorization':'Bearer synthetic'})
    preview=client.post('/v1/pipeline/recovery-preview',json={'batch_id':batch_id,'action':'skip'})
    assert preview.status_code==200,preview.text
    assert preview.json()['already_processed']==2 and preview.json()['originals']==2
    assert client.post('/v1/pipeline/skip-batch',json={'batch_id':batch_id}).status_code==400
    result=client.post('/v1/pipeline/skip-batch',json={'batch_id':batch_id,'confirm':'SKIP_PIPELINE_BATCH'})
    assert result.status_code==200,result.text
    assert result.json()['skipped_originals']==0 and result.json()['already_processed']==2
    assert client.get('/v1/pipeline/status').json()['pending_materials'][0]['status']=='resolved_skipped'
    assert all(rows(settings,table)==previous for table,previous in before.items())
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute('SELECT status FROM pipeline_batches WHERE id=?',(batch_id,)).fetchone()[0]=='done'
    assert asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))['status']=='current'


def test_skip_partially_settled_batch_preserves_receipts_and_skips_only_complete_remaining_units(settings):
    batch_id=failed(settings,tail=True)
    with Store(settings.database) as store:
        store.conn.executemany("INSERT INTO raw_processing VALUES (?,'other','settled')",[(1,),(2,)])
    originals=rows(settings,'raw_events');existing=rows(settings,'raw_processing')
    with pytest.raises(Conflict):
        recovery.recovery_preview(settings.database,batch_id,5000)
    preview=recovery.recovery_preview(settings.database,batch_id,action='skip')
    assert preview['already_processed']==2 and preview['originals']==8
    result=asyncio.run(recovery.recover_batch(settings.database,batch_id,'skip','SKIP_PIPELINE_BATCH'))
    assert result['already_processed']==2 and result['skipped_originals']==6
    assert rows(settings,'raw_processing')[:2]==existing
    assert len(rows(settings,'raw_processing'))==8
    assert rows(settings,'raw_events')==originals
    with Store(settings.database,read_only=True) as store:
        assert store.conn.execute("SELECT status FROM pipeline_pending_materials WHERE batch_id=?",(batch_id,)).fetchone()[0]=='resolved_skipped'
        assert store.conn.execute('SELECT 1 FROM raw_processing WHERE raw_id=9').fetchone() is None

    raw_archive(settings).ingest([{'source_event_id':'failed-9','session_id':'failed','role':'assistant',
        'text':'New answer after skipping','created_at':'2025-01-01T00:09:00Z','metadata':{'runtime':'failed'}}],source='synthetic')
    later=asyncio.run(p.advance(settings.database,include_recent=True,runner=short_runner))
    assert later['status']=='processed' and later['processed_originals']==2
