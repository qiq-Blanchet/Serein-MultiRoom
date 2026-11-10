"""Download readable Scene/Event bodies as a portable Markdown directory tree."""
import io
import json
import re
import zipfile
from ..core.store import digest
from typing import Literal
from fastapi import APIRouter
from fastapi.responses import Response
from ..core.reader import Reader


def database_snapshot(database):
    import sqlite3
    import tempfile
    from contextlib import closing
    from pathlib import Path
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse
    temporary=tempfile.TemporaryDirectory(prefix='serein-backup-')
    target=Path(temporary.name)/'serein.db'
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('备份未通过完整性检查')
        return FileResponse(target,filename='serein-backup.db',media_type='application/octet-stream',
            headers={'Cache-Control':'no-store'},background=BackgroundTask(temporary.cleanup))
    except BaseException:
        temporary.cleanup();raise


def markdown_archive(database, kind: Literal['all', 'scene', 'event'] = 'all'):
    if kind not in ('all', 'scene', 'event'):
        raise ValueError('Invalid export category')
    output=io.BytesIO()
    with Reader(database) as reader, zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        reader.store.conn.execute('BEGIN')
        for selected_kind in (('scene','event') if kind == 'all' else (kind,)):
            archive.writestr(selected_kind+'/',b'')
            rows=reader.store.conn.execute('SELECT id FROM documents WHERE kind=? ORDER BY created_at,id',(selected_kind,)).fetchall()
            for row in rows:
                result=reader.read(row['id'],with_evidence=False)
                if not result['readable'] or not result.get('document'):continue
                doc=result['document']
                slug=re.sub(r'[^\w\-]+','_',doc['title'],flags=re.UNICODE).strip('._')[:70] or selected_kind
                safe_id=re.sub(r'[^A-Za-z0-9_-]','_',doc['id'])[:100]+'-'+digest(doc['id'])[:12]
                # Original body stays intact below the portable metadata.
                metadata={key:doc.get(key) for key in ('id','kind','title','revision','lifecycle','created_at','updated_at')}
                front='\n'.join(key+': '+json.dumps(value,ensure_ascii=False) for key,value in metadata.items())
                archive.writestr(f'{selected_kind}/{slug}--{safe_id}.md','---\n'+front+'\n---\n\n'+doc['body_md'])
    return output.getvalue()


def routes(settings,auth):
    router=APIRouter(dependencies=auth)
    @router.get('/v1/export/backup')
    def backup():return database_snapshot(settings.database)
    @router.get('/v1/export/markdown')
    def download(kind: Literal['all', 'scene', 'event'] = 'all'):
        return Response(markdown_archive(settings.database, kind),media_type='application/zip',
            headers={'Content-Disposition':'attachment; filename="serein-memories.zip"','Cache-Control':'no-store'})
    return router
