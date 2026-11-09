// Opt-in dashboard selection. A signed room hint is NOT an authentication
// credential: every browser request still needs the original configurable web login.
import { createHmac, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';

export const ROOM_COOKIE = 'serein_dashboard_room';

function signature(key, roomId) {
  return createHmac('sha256', key).update('serein-dashboard-room-v1:' + roomId).digest('base64url');
}

export function roomCookie(key, roomId, secure = true) {
  if (!/^[a-f0-9]{32}$/.test(roomId)) throw Error('invalid_room_id');
  const value = roomId + '.' + signature(key, roomId);
  return ROOM_COOKIE + '=' + value + '; Path=/; HttpOnly; SameSite=Strict' + (secure ? '; Secure' : '');
}

export function selectedRoom(cookie, key) {
  if (!key) return null;
  const value = String(cookie || '').split(';').map(entry => entry.trim())
    .find(entry => entry.startsWith(ROOM_COOKIE + '='))?.slice(ROOM_COOKIE.length + 1);
  const match = /^([a-f0-9]{32})\.([A-Za-z0-9_-]{43})$/.exec(value || '');
  if (!match) return null;
  const expected = Buffer.from(signature(key, match[1]));
  const actual = Buffer.from(match[2]);
  return expected.length === actual.length && timingSafeEqual(actual, expected) ? match[1] : null;
}

export function readyRooms(filename) {
  try {
    const doc = JSON.parse(readFileSync(filename, 'utf8'));
    if (doc.version !== 1 || !Array.isArray(doc.rooms)) return [];
    return doc.rooms.filter(row => /^[a-f0-9]{32}$/.test(row.room_id)
      && /^[a-f0-9]{64}$/.test(row.gateway_key_sha256)
      && typeof row.internal_token === 'string' && row.internal_token.length >= 32);
  } catch { return []; }
}

export function effectiveRoom(cookie, key, routes) {
  const chosen = selectedRoom(cookie, key);
  return routes.find(row => row.room_id === chosen) || routes[0] || null;
}

export function signedPreviewRoom(roomId, key) {
  return signature(key, roomId);
}

export function verifyPreviewRoom(roomId, signed, key) {
  if (!/^[a-f0-9]{32}$/.test(roomId) || !key || typeof signed !== 'string') return false;
  const expected = Buffer.from(signature(key, roomId));
  const actual = Buffer.from(signed);
  return expected.length === actual.length && timingSafeEqual(actual, expected);
}
