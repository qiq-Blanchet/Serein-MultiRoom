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
  if (!action && !mcpTools && !['/__serein/rooms', '/__serein/rooms/select', '/__serein/rooms/current'].includes(path)) return false;
  const origin = publicOrigin || new URL('http://' + req.headers.host);
  if (!sameOrigin(req, origin)) { send(res, 403, {detail:'Same-origin request required'}); return true; }
  if ((action || path === '/__serein/rooms/select') && req.method !== 'POST'
      || mcpTools && !['GET','POST'].includes(req.method)
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
