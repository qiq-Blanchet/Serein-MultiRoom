"""Host-owned delivery acknowledgements and archive-backed evidence selection."""
import json
from fastapi import APIRouter, HTTPException, Query
from ..core.store import Store, encode, digest, Conflict
from ..compat.raw_archive import raw_archive
from ..chat_archive import archive_host_turn


def routes(settings, auth):
    router=APIRouter(dependencies=auth)
    with Store(settings.database) as store:
        store.conn.execute('CREATE TABLE IF NOT EXISTS host_deliveries '
            '(id INTEGER PRIMARY KEY, receipt_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
        store.conn.execute('CREATE TABLE IF NOT EXISTS host_turn_receipts '
            '(receipt_id TEXT PRIMARY KEY, window_id TEXT NOT NULL, payload_hash TEXT NOT NULL)')

    def record_delivery(body):
        payload=encode({k:body[k] for k in ('receipt_id','window_id','delivered_ids')})
        with Store(settings.database) as store,store.transaction():
            old=store.conn.execute('SELECT * FROM host_deliveries WHERE receipt_id=?',(body['receipt_id'],)).fetchone()
            if old and old['payload']!=payload:
                raise Conflict('Delivery receipt already records a different acknowledgement')
            store.conn.execute('INSERT OR IGNORE INTO host_deliveries(receipt_id,payload) VALUES (?,?)',(body['receipt_id'],payload))

    @router.post('/v1/host/deliveries')
    def delivered(body:dict):
        if not body.get('receipt_id') or not body.get('window_id') or not isinstance(body.get('delivered_ids'),list):
            raise HTTPException(400,'receipt_id, window_id and actually delivered_ids are required')
        if len(body['delivered_ids'])>500 or any(not isinstance(k,str) for k in body['delivered_ids']):
            raise HTTPException(400,'Invalid delivery IDs')
        record_delivery(body)
        return {'status':'recorded','reported_by':'host','delivered_ids':body['delivered_ids']}

    @router.post('/v1/host/turns')
    def completed_turn(body:dict):
        receipt_id=str(body.get('receipt_id') or '').strip()
        window_id=str(body.get('window_id') or '').strip()
        user_message=body.get('user_message')
        assistant_message=body.get('assistant_message')
        delivered_ids=body.get('delivered_ids',[])
        client=str(body.get('client') or 'chatgpt_work').strip()
        if not receipt_id or len(receipt_id)>300 or not window_id or len(window_id)>200:
            raise HTTPException(400,'receipt_id and window_id are required')
        if not isinstance(user_message,str) or not isinstance(assistant_message,str):
            raise HTTPException(400,'user_message and assistant_message must be text')
        if not user_message.strip() or not assistant_message.strip() or len(user_message)>120000 or len(assistant_message)>120000:
            raise HTTPException(400,'Completed host turn text is empty or too large')
        if not isinstance(delivered_ids,list) or len(delivered_ids)>500 or any(not isinstance(k,str) for k in delivered_ids):
            raise HTTPException(400,'delivered_ids must contain at most 500 IDs')
        if not client or len(client)>100 or any(ord(ch)<32 for ch in client):
            raise HTTPException(400,'Invalid host client name')
        delivered_ids=list(dict.fromkeys(delivered_ids))
        payload_hash=digest(encode([window_id,user_message,assistant_message,delivered_ids,client]))
        with Store(settings.database) as store,store.transaction():
            old=store.conn.execute('SELECT * FROM host_turn_receipts WHERE receipt_id=?',(receipt_id,)).fetchone()
            if old and (old['window_id']!=window_id or old['payload_hash']!=payload_hash):
                raise Conflict('Host turn receipt already records different content')
            store.conn.execute('INSERT OR IGNORE INTO host_turn_receipts VALUES (?,?,?)',
                (receipt_id,window_id,payload_hash))
        archived=archive_host_turn(settings,receipt_id=receipt_id,window_id=window_id,
            user_message=user_message,assistant_message=assistant_message,client=client)
        # Never acknowledge a completed turn that raw ingestion rejected.
        # The receipt remains idempotent: callers can retry the same payload.
        if archived.get('status') == 'rejected' or archived.get('rejected'):
            raise HTTPException(503, 'Host turn archive rejected; retry this receipt')
        record_delivery({'receipt_id':receipt_id,'window_id':window_id,'delivered_ids':delivered_ids})
        return {'status':'recorded','reported_by':'host','delivered_ids':delivered_ids,
                'raw_archive':archived,'pipeline_eligible':True}

    @router.get('/v1/host/deliveries')
    def history(limit:int=Query(80,ge=1,le=200),before_id:int=0,
                after_id:int | None=Query(None,ge=0),review_ids:str=''):
        if after_id is not None and before_id:
            raise HTTPException(400, 'Use either before_id or after_id')
        ids=list(dict.fromkeys(int(value) for value in review_ids.split(',') if value.isdigit() and int(value)>0))[:500]
        with Store(settings.database,read_only=True) as store:
            if after_id is not None:
                rows=store.conn.execute('SELECT * FROM host_deliveries WHERE id>? ORDER BY id ASC LIMIT ?',
                    (after_id,limit+1)).fetchall()
            else:
                rows=store.conn.execute('SELECT * FROM host_deliveries WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT ?',
                    (before_id,before_id,limit+1)).fetchall()
            reviewed=store.conn.execute('SELECT * FROM host_deliveries WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id DESC',ids).fetchall() if ids else []
        def serialize(row):
            payload=json.loads(row['payload'])
            return {'id':row['id'],**payload,'reported_by':payload.get('reported_by','host'),
                'gateway_memory_injected_ids':payload['delivered_ids'],
                'hook_memory_outcome':'injected' if payload['delivered_ids'] else 'no_match',
                'gateway_memory_trigger':'host_acknowledgement','session_id':payload['window_id']}
        items=[serialize(row) for row in rows[:limit]]
        next_id=items[-1]['id'] if items else 0
        return {'status':'ok','items':items,'has_more':len(rows)>limit,'next_before_id':next_id,
            **({'reviewed_items':[serialize(row) for row in reviewed if row['id'] not in {item['id'] for item in items}]} if ids else {}),
            **({'next_after_id':next_id or after_id} if after_id is not None else {})}

    @router.post('/v1/host/messages/search')
    def search(body:dict):
        archive=raw_archive(settings)
        ids=body.get('messageIds') or body.get('message_ids')
        limit=max(1,min(100,int(body.get('limit') or 30)))
        before=int(body.get('beforeId') or 0)
        context=int(body.get('contextMessageId') or 0)
        query=str(body.get('query') or '').strip()
        more=False
        mode='search' if query else 'browse'
        if ids:
            if not isinstance(ids,list) or not 1<=len(ids)<=100:
                raise HTTPException(400,'Select at most 100 messages')
            with Store(settings.database,read_only=True) as store:
                rows=store.conn.execute('SELECT * FROM raw_events WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id',ids).fetchall()
                items=[archive._row_to_event(r) for r in rows]
        else:
            with Store(settings.database,read_only=True) as store:
                if context:
                    target=store.conn.execute('SELECT * FROM raw_events WHERE id=?',(context,)).fetchone()
                    if target is None:
                        raise HTTPException(404,'Original context message not found')
                    radius=max(1,min(20,int(body.get('contextRadius') or 6)))
                    scope=(target['source'],target['session_id'])
                    earlier=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id<? ORDER BY id DESC LIMIT ?',(*scope,context,radius)).fetchall()
                    later=store.conn.execute('SELECT * FROM raw_events WHERE source=? AND session_id=? AND id>=? ORDER BY id LIMIT ?',(*scope,context,radius+1)).fetchall()
                    rows=list(reversed(earlier))+later
                    mode='context'
                else:
                    session=str(body.get('sessionId') or body.get('session_id') or '')
                    conditions=['(?=0 OR id<?)','(?=\'\' OR session_id=?)']
                    params=[before,before,session,session]
                    if query.startswith('#'):
                        conditions.append('(source_event_id=? OR CAST(id AS TEXT)=?)');params.extend([query[1:]]*2)
                    elif query:
                        conditions.append('instr(text,?)>0');params.append(query)
                    rows=store.conn.execute('SELECT * FROM raw_events WHERE '+' AND '.join(conditions)+' ORDER BY id DESC LIMIT ?',(*params,limit+1)).fetchall()
                    more=len(rows)>limit;rows=rows[:limit]
                items=[archive._row_to_event(r) for r in rows]
        messages=[{**r,'content':r['text'],'source_system':r['source'],
                   'source_message_id':r['source_event_id'] or str(r['id'])} for r in items]
        return {'status':'ok','messages':messages,'items':messages,'count':len(messages),'mode':mode,
                'target_message_id':context or None,'has_more':more,
                'next_before_id':min((r['id'] for r in messages),default=0)}
    return router
