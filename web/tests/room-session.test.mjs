import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  adminBasicMatches, roomCookie, selectedRoom, readyRooms,
  effectiveRoom, signedPreviewRoom, verifyPreviewRoom,
} from '../server/roomSession.mjs';
import { roomContextPlugin, backendFor } from '../server/roomContext.mjs';
import { initializeRoomScope, roomLocalStorage, roomSessionStorage }
  from '../src/storage/roomScopedStorage.js';

const KEY = 'synthetic-admin-secret-'.padEnd(64, 'k');
const A = 'a'.repeat(32), B = 'b'.repeat(32);
const mkRoutes = (filename) => writeFileSync(filename, JSON.stringify({
  version:1, rooms:[
    {room_id:A, gateway_key_sha256:'a'.repeat(64), internal_token:'internal-a'.padEnd(48,'a')},
    {room_id:B, gateway_key_sha256:'b'.repeat(64), internal_token:'internal-b'.padEnd(48,'b')},
  ],
}), {mode:0o600});

test('one administrator Basic identity authenticates all rooms, no room key can authenticate', () => {
  const header = 'Basic ' + Buffer.from('admin:' + KEY).toString('base64');
  assert.equal(adminBasicMatches(header, KEY), true);
  assert.equal(adminBasicMatches('Basic ' + Buffer.from('admin:other').toString('base64'), KEY), false);
  assert.equal(adminBasicMatches('Bearer synthetic-room-key', KEY), false);
  assert.equal(adminBasicMatches('Basic ' + Buffer.from('wrong:' + KEY).toString('base64'), KEY), false);
});

test('HttpOnly signed cookie selects one room, forgery cannot change room', () => {
  const cookie = roomCookie(KEY, B, true);
  assert.match(cookie, /HttpOnly; SameSite=Strict; Secure/);
  assert.equal(selectedRoom(cookie, KEY), B);
  assert.equal(selectedRoom(cookie.replace(B, A), KEY), null);
  assert.equal(selectedRoom(cookie, KEY+'x'), null);
  assert.equal(roomCookie(KEY,A,false).includes('; Secure'), false);
});

test('only a ready room is selected, forged or stopped selection fails closed', () => {
  const root = mkdtempSync(join(tmpdir(), 'serein-room-session-'));
  const path = join(root, 'routes.json');
  try {
    mkRoutes(path);
    const routes = readyRooms(path);
    assert.equal(effectiveRoom(roomCookie(KEY, B),KEY,routes).room_id,B);
    assert.equal(effectiveRoom(roomCookie(KEY, B),KEY,[routes[0]]).room_id,A);
    assert.equal(effectiveRoom('',KEY,[]),null);
    assert.equal(routes[0].room_id,A);
    assert.equal(readyRooms(path+'.missing').length,0);
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('Vite backend contexts use verified per-room internal token; never user room IDs', async () => {
  const root = mkdtempSync(join(tmpdir(), 'serein-room-context-'));
  const routesFile=join(root,'routes.json');
  const adminFile=join(root,'admin-key');
  mkRoutes(routesFile);
  writeFileSync(adminFile,KEY, {mode:0o600});
  const env={
    SEREIN_MULTIROOM_ROUTES_FILE:routesFile,
    SEREIN_MULTIROOM_ADMIN_KEY_FILE:adminFile,
    SEREIN_MEMORY_URL:'http://legacy-db:8011',
    SEREIN_MEMORY_TOKEN:'legacy-key',
  };
  let middleware;
  roomContextPlugin({env}).configurePreviewServer({middlewares:{use(fn){middleware=fn;}}});
  try {
    assert.equal(backendFor(env).base,''); // missing context cannot fall back to legacy
    for(const id of [A,B]) {
      const result=await new Promise((resolve)=>{
        const req={headers:{
          'x-serein-verified-room':id,
          'x-serein-verified-proof':signedPreviewRoom(id,KEY),
        }};
        const res={writeHead(code){resolve({code});},end(){}};
        middleware(req,res,()=>resolve(backendFor(env)));
      });
      assert.equal(result.roomId,id);
      assert.match(result.base,new RegExp(id));
      assert.equal(result.token.includes('internal-'+(id===A?'a':'b')),true);
    }
    assert.equal(verifyPreviewRoom(A,signedPreviewRoom(B,KEY),KEY),false);
    const denied=await new Promise((resolve)=>{
      const res={writeHead(code){resolve(code);},end(){}};
      middleware({headers:{
        'x-serein-verified-room':B,
        'x-serein-verified-proof':signedPreviewRoom(A,KEY),
      }},res,()=>resolve('unverified accepted'));
    });
    assert.equal(denied,401);
  } finally {rmSync(root,{recursive:true,force:true});}
});

class MemoryStorage {
  constructor(){this.state=new Map();}
  get length(){return this.state.size;}
  key(i){return [...this.state.keys()][i]??null;}
  getItem(k){return this.state.has(k)?this.state.get(k):null;}
  setItem(k,v){this.state.set(String(k),String(v));}
  removeItem(k){this.state.delete(k);}
  clear(){this.state.clear();}
}
test('browser local/session state is room-namespaced but legacy keys are retained', async () => {
  const storage=new MemoryStorage(), session=new MemoryStorage();
  globalThis.window={localStorage:storage, sessionStorage:session};
  storage.setItem('serein.awake.name.user','OLD_LEGACY_NAME');
  const response={status:200,ok:true,headers:new Headers({'content-type':'application/json'}),
    json:async()=>({enabled:true,room_id:A})};
  assert.equal(await initializeRoomScope(async()=>response),A);
  roomLocalStorage().setItem('serein.awake.name.user','ROOM_A_NAME');
  roomSessionStorage().setItem('serein:narrative-open-intent','room-a-only');
  assert.equal(storage.getItem('serein.awake.name.user'),'OLD_LEGACY_NAME');
  assert.equal(storage.getItem('serein.multiroom.'+A+':serein.awake.name.user'),'ROOM_A_NAME');
  assert.equal(session.getItem('serein:narrative-open-intent'),null);
  assert.equal(roomSessionStorage().getItem('serein:narrative-open-intent'),'room-a-only');
  roomLocalStorage().clear();
  assert.equal(storage.getItem('serein.awake.name.user'),'OLD_LEGACY_NAME');
});
