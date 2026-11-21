"""Read a provider catalog without saving credentials or changing task assignments."""
import json
from urllib.parse import urlsplit

import httpx

import os
from .deployment import read_settings, grouped_upstreams


async def discover(settings, request, *, client=None):
    cfg = read_settings(settings.database)
    base = request['base_url'].rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请填写不含密钥或查询参数的 HTTP(S) 上游地址。')
    previous = next((u for u in grouped_upstreams(cfg) if u['id']==request['upstream_id']), None)
    key = request.get('api_key') or ''
    if request.get('clear_key'):
        key = ''
    elif not key and previous:
        if base != previous['base_url'].rstrip('/'):
            raise ValueError('上游地址已改变，请填写新地址对应的密钥后再拉取。')
        key = previous.get('api_key') or os.environ.get(previous.get('api_key_env', ''), '')
    headers = {'Authorization': 'Bearer '+key} if key else {}
    if request.get('protocol') == 'anthropic':
        headers = {'anthropic-version': '2023-06-01', **({'x-api-key': key} if key else {})}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False,
                                         trust_env=request.get('protocol') != 'codex')
    try:
        async with client.stream('GET',base+'/models',headers=headers) as response:
            if response.status_code != 200:
                if response.status_code in (404,405):raise ValueError('这个上游不支持拉取模型，仍可手动填写模型名。')
                raise ValueError(f'拉取模型失败（HTTP {response.status_code}），请检查地址和密钥；也可以手动填写。')
            data=bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data)>2_000_000:raise ValueError('上游模型列表过大，请手动填写。')
        result=json.loads(data)
        rows=result.get('data') if isinstance(result,dict) else None
        if not isinstance(rows,list):raise ValueError('上游返回的模型列表格式不支持，仍可手动填写。')
        models=set()
        for row in rows:
            identifier=row.get('id') if isinstance(row,dict) else None
            if not isinstance(identifier,str):continue
            identifier=identifier.strip()
            if 1<=len(identifier)<=200 and not any(ord(c)<32 for c in identifier) and not (key and key in identifier):
                models.add(identifier)
        return {'models':sorted(models)[:2000], 'truncated':len(models)>2000 or bool(result.get('has_more'))}
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error,httpx.HTTPError):raise ValueError('无法连接上游，请检查地址或稍后重试；仍可手动填写。') from None
        if isinstance(error,(json.JSONDecodeError,UnicodeError)):raise ValueError('上游没有返回有效的模型列表，仍可手动填写。') from None
        raise
    finally:
        if owned:await client.aclose()
