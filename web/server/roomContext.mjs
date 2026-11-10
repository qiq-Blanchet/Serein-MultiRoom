// Request-local backend routing for existing Vite dashboard bridges.
// Legacy single-room mode stays unchanged unless multi-room variables are set.
import { AsyncLocalStorage } from 'node:async_hooks';
import { readFileSync } from 'node:fs';
import { readyRooms, verifyPreviewRoom } from './roomSession.mjs';

const context = new AsyncLocalStorage();

export function backendFor(env = process.env) {
  const room = context.getStore();
  if (room) return { base: room.base, token: room.token, roomId: room.roomId };
  // Never fall back to the original room when a multi-room request loses its
  // authenticated async context: that would be a cross-room data leak.
  if (env.SEREIN_MULTIROOM_ROUTES_FILE) {
    return { base: '', token: '', roomId: null };
  }
  return { base: String(env.SEREIN_MEMORY_URL || '').trim(),
           token: String(env.SEREIN_MEMORY_TOKEN || '').trim(),
           roomId: null };
}

function privateHeader(request, name) {
  const value = request.headers[name];
  return typeof value === 'string' ? value : '';
}

export function roomContextPlugin({env = process.env} = {}) {
  function install(server) {
    if (!env.SEREIN_MULTIROOM_ROUTES_FILE) return;
    server.middlewares.use((request, response, next) => {
      const roomId = privateHeader(request, 'x-serein-verified-room');
      const proof = privateHeader(request, 'x-serein-verified-proof');
      let key;
      try { key = readFileSync(env.SEREIN_MULTIROOM_ADMIN_KEY_FILE, 'utf8').trim(); }
      catch { response.writeHead(503); response.end('Room manager unavailable'); return; }
      const route = readyRooms(env.SEREIN_MULTIROOM_ROUTES_FILE)
        .find(item => item.room_id === roomId);
      if (!route || !verifyPreviewRoom(roomId, proof, key)) {
        response.writeHead(401, {'Cache-Control':'no-store'});
        response.end('Verified room context required');
        return;
      }
      const room = { base: 'http://room-' + roomId + ':8011',
                     token: route.internal_token, roomId };
      context.run(room, () => next());
    });
  }
  return { name:'serein-room-context',
           configurePreviewServer:install,
           configureServer:install };
}

export function currentRoomId() {
  return context.getStore()?.roomId ?? null;
}

/**
 * An old browser tab must not submit an import to a newly selected room.
 * The x-serein-target-room header is only a consistency check, never
 * authentication or routing. The signed server room context stays authoritative.
 */
export function requireRoomMatch(request, response, env = process.env) {
  if (!env.SEREIN_MULTIROOM_ROUTES_FILE) return true;
  const expected = currentRoomId();
  const requested = request.headers['x-serein-target-room'];
  if (expected && typeof requested === 'string' && requested === expected) return true;
  response.writeHead(409, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(JSON.stringify({detail: '当前房间已切换，请重新打开页面后再导入或导出。'}));
  return false;
}

export function currentRoomSlug(env = process.env) {
  const id = currentRoomId();
  if (!id || !env.SEREIN_MULTIROOM_ROUTES_FILE) return null;
  // Always derive metadata from the server's ready manifest, never a
  // client-supplied filename or query parameter.
  return readyRooms(env.SEREIN_MULTIROOM_ROUTES_FILE)
    .find(room => room.room_id === id)?.room_slug || id;
}

export function roomBackupBasename() {
  const slug = currentRoomSlug();
  if (!slug) return 'serein';
  const safe = slug.replace(/[^a-z0-9-]/g, '').slice(0,48);
  return 'serein-' + (safe || currentRoomId());
}
