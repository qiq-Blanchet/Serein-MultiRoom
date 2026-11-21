import json

from serein.core.store import Store
from test_public_settings import deployment


def test_completed_host_turn_archives_pair_and_delivery_for_pipeline(deployment):
    settings, client = deployment
    body = {
        'receipt_id': 'work:session-1:turn-1',
        'window_id': 'work:session-1',
        'user_message': 'Remember the synthetic blue notebook.',
        'assistant_message': 'I will remember the blue notebook.',
        'delivered_ids': ['scene:synthetic'],
        'client': 'chatgpt_work',
    }
    response = client.post('/v1/host/turns', json=body)
    response.raise_for_status()
    assert response.json()['pipeline_eligible'] is True
    assert response.json()['delivered_ids'] == ['scene:synthetic']
    from serein.extensions.pipeline import initialize
    initialize(settings.database)
    with Store(settings.database, read_only=True) as store:
        rows = store.conn.execute('SELECT * FROM raw_events ORDER BY id').fetchall()
        assert [(row['role'], row['text']) for row in rows] == [
            ('user', body['user_message']), ('assistant', body['assistant_message'])]
        assert all(row['source'] == 'serein_host' and row['session_id'] == body['window_id'] for row in rows)
        assert all(json.loads(row['metadata_json'])['host_receipt_id'] == body['receipt_id'] for row in rows)
        assert store.conn.execute('SELECT count(*) FROM raw_processing').fetchone()[0] == 0
        delivery = json.loads(store.conn.execute('SELECT payload FROM host_deliveries').fetchone()[0])
        assert delivery['delivered_ids'] == ['scene:synthetic']

    # Hook retries are idempotent and do not duplicate source material.
    retry = client.post('/v1/host/turns', json=body)
    retry.raise_for_status()
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
        assert store.conn.execute('SELECT count(*) FROM host_deliveries').fetchone()[0] == 1


def test_completed_host_turn_rejects_receipt_reuse_and_incomplete_turn(deployment):
    _, client = deployment
    body = {'receipt_id':'same','window_id':'work:one','user_message':'one',
            'assistant_message':'answer','delivered_ids':[]}
    client.post('/v1/host/turns', json=body).raise_for_status()
    conflict = client.post('/v1/host/turns', json={**body, 'assistant_message':'different'})
    assert conflict.status_code == 400
    assert client.post('/v1/host/turns', json={**body, 'receipt_id':'empty', 'assistant_message':''}).status_code == 400
