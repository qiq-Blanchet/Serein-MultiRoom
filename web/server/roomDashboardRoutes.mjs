// Same-origin, opt-in management endpoints for the existing Serein Dashboard.
// The browser authenticates only once with the administrator's HTTP Basic key.
// The administrator token is NEVER returned in a room listing or embedded JS.
import { readyRooms, effectiveRoom, roomCookie } from './roomSession.mjs';
import { Readable } from 'node:stream';

function send(res, code, data, extra = {}) {
  const payload = JSON.stringify(data);
  res.writeHead(code, {'Content-Type':'application/json; charset=utf-8',
                       'Cache-Control':'no-store',
                       'X-Content-Type-Options':'nosniff', ...extra});
  res.end(payload);
}

function sameOrigin(req, expected) {
  if (req.headers['sec-fetch-site'] && !['same-origin', 'none'].includes(req.headers['sec-fetch-site'])) return false;
  const origin = req.headers.origin;
  if (!origin) return true; // non-browser authenticated API clients
  try {
    const received = new URL(origin);
    return received.origin === expected.origin;
  } catch { return false; }
}

async function readBody(req) {
  const chunks = []; let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 8192) throw Error('request_too_large');
    chunks.push(chunk);
  }
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw Error('invalid_body');
  return body;
}

function privateDockerAdminHost(hostname) {
  // The dashboard may run in the installation's private Docker network.
  // Never allow arbitrary public or link-local hosts as control-plane targets.
  const parts = String(hostname || "").split(".");
  if (parts.length !== 4 || parts.some(part => !/^\d{1,3}$/.test(part) ||
      Number(part) > 255)) return false;
  const [first, second] = parts.map(Number);
  return first === 10 || first === 172 && second >= 16 && second <= 31
    || first === 192 && second === 168;
}

function fields(room) {
  return {
    room_id:room.room_id, room_slug:room.room_slug, name:room.name,
    display_name:room.display_name, state:room.state,
    created_at:room.created_at, source_room_id:room.source_room_id,
    docker:room.docker && {container:room.docker.container, health:room.docker.health},
  };
}

export async function dashboardRoomEndpoint(req, res, {
    path, publicOrigin, adminKey, routesFile, adminURL, fetchImpl = fetch,
}) {
  const action = /^\/__serein\/rooms\/([a-f0-9]{32})\/(start|stop)$/.exec(path);
  const mcpTools = /^\/__serein\/rooms\/([a-f0-9]{32})\/mcp-tools$/.exec(path);
  const backups = /^\/__serein\/rooms\/([a-f0-9]{32})\/backups(?:\/(\d{8}T\d{6}Z-[a-f0-9]{12}\.tar\.gz)\/(preview|download|restore-plan|restore))?$/.exec(path);
  const backupUpload = /^\/__serein\/rooms\/([a-f0-9]{32})\/backups\/upload$/.exec(path);
  if (!action && !mcpTools && !backups && !backupUpload && !['/__serein/rooms', '/__serein/rooms/select', '/__serein/rooms/reset', '/__serein/rooms/current'].includes(path)) return false;
  const origin = publicOrigin || new URL('http://' + req.headers.host);
  if (!sameOrigin(req, origin)) { send(res, 403, {detail:'Same-origin request required'}); return true; }
  if ((action || path === '/__serein/rooms/select' || path === '/__serein/rooms/reset') && req.method !== 'POST'
      || mcpTools && !['GET','POST'].includes(req.method)
      || backups && !(backups[2] ?
           req.method === (backups[3] === 'restore' ? 'POST' : 'GET') :
           ['GET','POST'].includes(req.method))
      || backupUpload && req.method !== 'POST'
      || path === '/__serein/rooms/current' && req.method !== 'GET'
      || path === '/__serein/rooms' && !['GET','POST'].includes(req.method)) {
    send(res, 405, {detail:'Method not allowed'});return true;
  }
  const ready = readyRooms(routesFile);
  const selected = effectiveRoom(req.headers.cookie, adminKey, ready);
  if (path === '/__serein/rooms/reset') {
    // Explicit user action to leave a stopped room. No implicit switch.
    const secure = origin.protocol === 'https:';
    send(res, 200, {cleared:true}, {
      'Set-Cookie':'serein_dashboard_room=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict' +
        (secure ? '; Secure' : ''),
    });
    return true;
  }
  if (path === '/__serein/rooms/current') {
    send(res, 200, {room_id:selected?.room_id || null,
      room_slug:selected?.room_slug || null, enabled:true}); return true;
  }
  if (action) {
    const responseUrl = new URL('/api/rooms/' + action[1] + '/' + action[2], adminURL);
    try {
      const response = await fetchImpl(responseUrl, {
        method:'POST', redirect:'manual',
        headers:{'Authorization':'Bearer ' + adminKey, 'Accept':'application/json'},
        signal:AbortSignal.timeout(60000),
      });
      if (!response.ok) { send(res,response.status === 409 ? 409 : 502,
                             {detail:'Room lifecycle operation failed'}); return true; }
      const room = await response.json();
      send(res,200,{room:fields(room)});
    } catch {send(res,502,{detail:'Room lifecycle administrator unavailable'});}
    return true;
  }
  if (backupUpload) {
    if (!selected || backupUpload[1] !== selected.room_id ||
        req.headers['x-serein-target-room'] !== selected.room_id) {
      send(res,409,{detail:'房间已经切换，请刷新页面后再上传备份。'});return true;
    }
    if (!['application/gzip','application/octet-stream'].includes(
         String(req.headers['content-type']||'').split(';')[0].trim())) {
      send(res,415,{detail:'Only gzip room archives are accepted'});return true;
    }
    const contentLength=Number(req.headers['content-length']||0);
    if (!Number.isFinite(contentLength) || contentLength < 0 ||
        contentLength > 16*1024*1024*1024) {
      send(res,413,{detail:'Room archive exceeds size limit'});return true;
    }
    try {
      const base=new URL(adminURL);
      if (base.username || base.password || base.pathname !== '/' || base.search || base.hash
          || base.protocol !== 'http:' ||
          (!['localhost','127.0.0.1','[::1]','serein-room-admin'].includes(base.hostname)
          && !privateDockerAdminHost(base.hostname))) throw Error('Private admin URL required');
      const reply=await fetchImpl(
        new URL('/api/rooms/'+selected.room_id+'/backups/upload',base),{
          method:'POST',redirect:'manual',
          headers:{Authorization:'Bearer '+adminKey,
                   'Content-Type':'application/gzip'},
          body:req,duplex:'half',signal:AbortSignal.timeout(1200000),
        });
      if (!reply.ok) {
        send(res,reply.status >= 400 && reply.status < 500 ? reply.status : 502,
             {detail:'备份上传未通过当前房间身份或完整性校验'});return true;
      }
      send(res,201,await reply.json());
    } catch {
      send(res,502,{detail:'备份上传中断，请检查连接和服务器空间'});
    }
    return true;
  }
  if (backups) {
    // Downloads and snapshots are explicitly scoped to the signed current
    // room, AND to the room identity obtained when the browser tab opened.
    // This header is only a stale-tab guard; it cannot select a room.
    if (!selected || backups[1] !== selected.room_id ||
        (backups[3] !== 'download' &&
          req.headers['x-serein-target-room'] !== selected.room_id)) {
      send(res,409,{detail:'房间已经切换，请刷新页面后再备份。'});return true;
    }
    let payload;
    if (req.method === 'POST') {
      if (!String(req.headers['content-type'] || '').startsWith('application/json')) {
        send(res,415,{detail:'JSON required'});return true;
      }
      try { payload = await readBody(req); }
      catch { send(res,400,{detail:'Invalid room backup request'});return true; }
      const isRestore = backups[3] === 'restore';
      if (isRestore) {
        const slug=payload.confirm_room_id, hash=payload.expected_sha256;
        if (typeof slug !== 'string' || !/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(slug)
            || slug.length < 3 || slug.length > 48
            || typeof hash !== 'string' || !/^[a-f0-9]{64}$/.test(hash)
            || Object.keys(payload).some(key=>!['confirm_room_id','expected_sha256'].includes(key))) {
          send(res,400,{detail:'Restore requires exact room ID and archive checksum'});return true;
        }
        if (slug !== selected.room_slug) {
          send(res,409,{detail:'Restore room ID does not match current AI'});return true;
        }
      } else if (Object.keys(payload).length) {
        send(res,400,{detail:'Unexpected room backup options'});return true;
      }
    }
    try {
      const base = new URL(adminURL);
      if (base.username || base.password || base.pathname !== '/' || base.search || base.hash
          || base.protocol !== 'http:' ||
          (!['localhost','127.0.0.1','[::1]','serein-room-admin'].includes(base.hostname)
            && !privateDockerAdminHost(base.hostname))) throw Error('Private admin URL required');
      const endpoint = '/api/rooms/' + selected.room_id + '/backups' +
        (backups[2] ? '/' + backups[2] + '/' + backups[3] : '');
      const reply = await fetchImpl(new URL(endpoint,base),{
        method:req.method,redirect:'manual',
        headers:{Authorization:'Bearer ' + adminKey,Accept:backups[3]==='download'
            ? 'application/gzip':'application/json',
          ...(payload ? {'Content-Type':'application/json'} : {})},
        body:payload ? JSON.stringify(payload) : undefined,
        signal:AbortSignal.timeout(1200000),
      });
      if (!reply.ok || reply.status >= 300) {
        send(res,reply.status >= 400 && reply.status < 500 ? reply.status : 502,
             {detail:'房间备份未完成或备份文件不可用'});return true;
      }
      if (backups[3] === 'download') {
        res.writeHead(200, {
          'Content-Type':'application/gzip',
          'Cache-Control':'no-store',
          'X-Content-Type-Options':'nosniff',
          'Content-Disposition':'attachment; filename="serein-' +
             (selected.room_slug || selected.room_id) + '-' + backups[2] + '"',
          ...(reply.headers.get('Content-Length') ?
             {'Content-Length':reply.headers.get('Content-Length')} : {}),
        });
        if (reply.body) await new Promise((resolve,reject) =>
          Readable.fromWeb(reply.body).on('error',reject).on('end',resolve).pipe(res));
        else res.end();
        return true;
      }
      const data = await reply.json();
      send(res,reply.status === 201 ? 201 : 200,data);
    } catch {
      if (res.headersSent && typeof res.destroy === 'function') res.destroy();
      else send(res,502,{detail:'整房备份服务暂不可用，请检查当前房间状态'});
    }
    return true;
  }
  if (mcpTools) {
    // The Settings page may only modify the currently authenticated room;
    // an arbitrary UUID from browser input cannot update another room.
    if (!selected || mcpTools[1] !== selected.room_id) {
      send(res, 403, {detail:'Select the room before editing its MCP tools'}); return true;
    }
    let body;
    if (req.method === 'POST') {
      if (!String(req.headers['content-type'] || '').startsWith('application/json')) {
        send(res,415,{detail:'JSON required'});return true;
      }
      try {body = await readBody(req);} catch {
        send(res,400,{detail:'Invalid MCP tool selection'});return true;
      }
      if (!['original','custom'].includes(body.mode) || !Array.isArray(body.tools)
          || body.tools.length > 128
          || body.tools.some(item => typeof item !== 'string' || !item || item.length > 100)
          || Object.keys(body).some(key => !['mode','tools'].includes(key))) {
        send(res,400,{detail:'Invalid MCP tool selection'});return true;
      }
    }
    try {
      const base = new URL(adminURL);
      if (base.username || base.password || base.pathname !== '/' || base.search || base.hash
          || base.protocol !== 'http:' ||
          (!['localhost','127.0.0.1','[::1]','serein-room-admin'].includes(base.hostname)
            && !privateDockerAdminHost(base.hostname))) throw Error('Private admin URL required');
      const target = new URL('/api/rooms/' + mcpTools[1] + '/mcp-tools', base);
      const response = await fetchImpl(target, {
        method:req.method, redirect:'manual',
        headers:{'Authorization':'Bearer '+adminKey, 'Content-Type':'application/json'},
        body:body ? JSON.stringify(body) : undefined,
        signal:AbortSignal.timeout(90000),
      });
      if (!response.ok) {
        send(res, response.status >= 400 && response.status < 500 ? response.status : 502,
             {detail:'Unable to read or update MCP tools'});return true;
      }
      const data = await response.json();
      send(res,200,data);
    } catch {
      send(res,502,{detail:'Room MCP administrator unavailable'});
    }
    return true;
  }
  if (path === '/__serein/rooms/select') {
    if (!String(req.headers['content-type']||'').startsWith('application/json')) {
      send(res, 415, {detail:'JSON required'}); return true;
    }
    let body;
    try {body = await readBody(req);} catch {send(res,400,{detail:'Invalid room selection'});return true;}
    const chosen = ready.find(room => room.room_id === body.room_id);
    if (!chosen || Object.keys(body).some(key => key !== 'room_id')) {
      send(res, 409, {detail:'Room is not ready or does not exist'}); return true;
    }
    const secure = origin.protocol === 'https:';
    send(res, 200, {room_id:chosen.room_id}, {'Set-Cookie':roomCookie(adminKey, chosen.room_id, secure)});
    return true;
  }
  if (req.method === 'POST' && !String(req.headers['content-type']||'').startsWith('application/json')) {
    send(res,415,{detail:'JSON required'});return true;
  }
  let body;
  if (req.method === 'POST') {
    try {body = await readBody(req);} catch {send(res,400,{detail:'Invalid room creation request'});return true;}
    if (typeof body.room_slug !== 'string'
        || !/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(body.room_slug)
        || body.room_slug.length < 3 || body.room_slug.length > 48
        || !(body.source === undefined || ['first','blank'].includes(body.source) || /^[a-f0-9]{32}$/.test(body.source))
        || (body.reuse_provider_keys !== undefined && typeof body.reuse_provider_keys !== 'boolean')
        || Object.keys(body).some(k => !['room_slug','source','reuse_provider_keys'].includes(k))) {
      send(res,400,{detail:'Invalid room parameters'});return true;
    }
  }
  // Only the server-side gateway sends the admin token to the control plane.
  try {
    const base = new URL(adminURL);
    if (base.username || base.password || base.pathname !== '/' || base.search || base.hash
        || base.protocol !== 'http:'
        || !['localhost','127.0.0.1','[::1]','serein-room-admin'].includes(base.hostname)
        && !privateDockerAdminHost(base.hostname)) {
      throw Error('Invalid private administrator service URL');
    }
    const response = await fetchImpl(new URL('/api/rooms', base), {
      method:req.method, redirect:'manual',
      headers:{'Authorization':'Bearer '+adminKey, 'Content-Type':'application/json',
               'Accept':'application/json'},
      body:body ? JSON.stringify({...body, name:body.room_slug}) : undefined,
      signal:AbortSignal.timeout(15000),
    });
    if (response.status >= 300 && response.status < 400) throw Error('Admin service redirect forbidden');
    if (!response.ok) {
      send(res, response.status >= 400 && response.status < 500 ? response.status : 502,
           {detail:'Room administration request failed'});return true;
    }
    const data = await response.json();
    if (req.method === 'GET') {
      const all = Array.isArray(data.rooms) ? data.rooms.map(fields) : [];
      send(res, 200, {rooms:all, current:selected?.room_id || null});
    } else {
      send(res, 201, {room:fields(data.room || {}), gateway_key:data.gateway_key});
    }
  } catch {
    send(res, 502, {detail:'Private room administrator unavailable'});
  }
  return true;
}
