import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { roomContextPlugin, requireRoomMatch, roomBackupBasename } from '../server/roomContext.mjs';
import { signedPreviewRoom } from '../server/roomSession.mjs';

const KEY = 'synthetic-administrator-secret-'.padEnd(64, 'k');
const A='a'.repeat(32), B='b'.repeat(32);

function response() {
  return {statusCode:0,body:'',headers:{},
    writeHead(code,headers={}){this.statusCode=code;this.headers=headers;},
    end(body=''){this.body=body;}};
}

test('a stale browser tab cannot import or export into a different selected room', async()=>{
  const root=mkdtempSync(join(tmpdir(),'serein-import-room-'));
  const routePath=join(root,'routes.json'), keyPath=join(root,'admin-key');
  writeFileSync(routePath,JSON.stringify({version:1,rooms:[
    {room_id:A,room_slug:'xiaogi-main',internal_token:'synthetic-a'.padEnd(48,'a'),
      gateway_key_sha256:'a'.repeat(64)},
    {room_id:B,room_slug:'other-main',internal_token:'synthetic-b'.padEnd(48,'b'),
      gateway_key_sha256:'b'.repeat(64)},
  ]}));
  writeFileSync(keyPath,KEY);
  const env={SEREIN_MULTIROOM_ROUTES_FILE:routePath,
             SEREIN_MULTIROOM_ADMIN_KEY_FILE:keyPath};
  let middleware;
  roomContextPlugin({env}).configurePreviewServer({middlewares:{use(fn){middleware=fn;}}});
  function attempt(selected, submitted) {
    return new Promise((resolve,reject)=>{
      const request={headers:{
        'x-serein-verified-room':selected,
        'x-serein-verified-proof':signedPreviewRoom(selected,KEY),
        ...(submitted===null?{}:{'x-serein-target-room':submitted}),
      }};
      const res=response();
      middleware(request,res,()=>{
        try {resolve({ok:requireRoomMatch(request,res,env),
                     basename:roomBackupBasename(),res});}
        catch(error){reject(error);}
      });
    });
  }
  try {
    const validA=await attempt(A,A);
    assert.equal(validA.ok,true);
    assert.equal(validA.basename,'serein-xiaogi-main');
    const validB=await attempt(B,B);
    assert.equal(validB.ok,true);
    assert.equal(validB.basename,'serein-other-main');
    const stale=await attempt(B,A);
    assert.equal(stale.ok,false);
    assert.equal(stale.res.statusCode,409);
    assert.match(stale.res.body,/当前房间已切换/);
    const missing=await attempt(B,null);
    assert.equal(missing.res.statusCode,409);
    const random=await attempt(A,'not-a-real-room');
    assert.equal(random.res.statusCode,409);
    assert.equal(requireRoomMatch({headers:{}},response(),{}),true);
  } finally {rmSync(root,{recursive:true,force:true});}
});
