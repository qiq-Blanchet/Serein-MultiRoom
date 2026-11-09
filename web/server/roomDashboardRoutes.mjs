// Same-origin, opt-in management endpoints for the existing Serein Dashboard.
// The browser authenticates only once with the administrator's HTTP Basic key.
// The administrator token is NEVER returned in a room listing or embedded JS.
import { readyRooms, effectiveRoom, roomCookie } from './roomSession.mjs';

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

function fields(room) {
  return {
    room_id:room.room_id, name:room.name, state:room.state,
    created_at:room.created_at, source_room_id:room.source_room_id,
    docker:room.docker && {container:room.docker.container, health:room.docker.health},
  };
}

export async function dashboardRoomEndpoint(req, res, {
    path, publicOrigin, adminKey, routesFile, adminURL, fetchImpl = fetch,
}) {
  const action = /^\/__serein\/rooms\/([a-f0-9]{32})\/(start|stop)$/.exec(path);
  if (!action && !['/__serein/rooms', '/__serein/rooms/select', '/__serein/rooms/current'].includes(path)) return false;
  const origin = publicOrigin || new URL('http://' + req.headers.host);
  if (!sameOrigin(req, origin)) { send(res, 403, {detail:'Same-origin request required'}); return true; }
  if ((action || path === '/__serein/rooms/select') && req.method !== 'POST'
      || path === '/__serein/rooms/current' && req.method !== 'GET'
      || path === '/__serein/rooms' && !['GET','POST'].includes(req.method)) {
    send(res, 405, {detail:'Method not allowed'});return true;
  }
  const ready = readyRooms(routesFile);
  const selected = effectiveRoom(req.headers.cookie, adminKey, ready);
  if (path === '/__serein/rooms/current') {
    send(res, 200, {room_id:selected?.room_id || null, enabled:true}); return true;
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
    if (typeof body.name !== 'string' || !body.name.trim() || body.name.length > 80
        || !(body.source === undefined || ['first','blank'].includes(body.source) || /^[a-f0-9]{32}$/.test(body.source))
        || (body.reuse_provider_keys !== undefined && typeof body.reuse_provider_keys !== 'boolean')
        || Object.keys(body).some(k => !['name','source','reuse_provider_keys'].includes(k))) {
      send(res,400,{detail:'Invalid room parameters'});return true;
    }
  }
  // Only the server-side gateway sends the admin token to the control plane.
  try {
    const base = new URL(adminURL);
    if (base.username || base.password || base.pathname !== '/' || base.search || base.hash
        || base.protocol !== 'http:'
        || !['localhost','127.0.0.1','[::1]','serein-room-admin','host.docker.internal'].includes(base.hostname)) {
      throw Error('Invalid private administrator service URL');
    }
    const response = await fetchImpl(new URL('/api/rooms', base), {
      method:req.method, redirect:'manual',
      headers:{'Authorization':'Bearer '+adminKey, 'Content-Type':'application/json',
               'Accept':'application/json'},
      body:body ? JSON.stringify(body) : undefined,
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
