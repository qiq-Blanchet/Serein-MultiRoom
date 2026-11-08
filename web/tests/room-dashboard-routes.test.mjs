import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { dashboardRoomEndpoint } from '../server/roomDashboardRoutes.mjs';
import { roomCookie } from '../server/roomSession.mjs';

const A='a'.repeat(32),B='b'.repeat(32);
const KEY='synthetic-admin-key-'.padEnd(64,'z');
function req(method='GET',body=undefined,headers={}) {
  const json=body===undefined?undefined:JSON.stringify(body);
  return Object.assign(Readable.from(json===undefined?[]:[Buffer.from(json)]), {
    method, headers:{host:'127.0.0.1:18221',
      ...(json===undefined?{}:{'content-type':'application/json'}),...headers},
  });
}
function capture() {
  return {statusCode:0,headers:{},body:'',writeHead(status,headers){this.statusCode=status;this.headers=headers||{};},
    end(data=''){this.body+=data;}};
}
function manifest(path, ids=[A]) {
  writeFileSync(path,JSON.stringify({version:1,rooms:ids.map(id=>({
    room_id:id,gateway_key_sha256:id.slice(0,1).repeat(64),internal_token:'s'.repeat(40),
  }))}));
}
function options(path, fetchImpl) {
  return {routesFile:path, adminKey:KEY, adminURL:'http://127.0.0.1:18220',
    publicOrigin:new URL('http://127.0.0.1:18221'),fetchImpl};
}
test('dashboard list uses server-held admin secret, returns metadata only', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-route-')),path=join(root,'routes.json');manifest(path);
  let header;
  const mocked=async (url, opts)=>{
    header=opts.headers.Authorization;
    assert.match(String(url),/\/api\/rooms$/);
    return new Response(JSON.stringify({rooms:[{room_id:A,name:'Synthetic A',state:'ready',
      docker:{container:'running',health:'healthy'},api_key:'PRIVATE_PROVIDER_KEY'}]}),{
      status:200,headers:{'content-type':'application/json'},
    });
  };
  try {
    const res=capture(), handled=await dashboardRoomEndpoint(req(),res,{
      ...options(path,mocked),path:'/__serein/rooms',
    });
    assert.equal(handled,true);assert.equal(res.statusCode,200);
    const payload=JSON.parse(res.body);
    assert.equal(payload.current,A);
    assert.equal(payload.rooms[0].name,'Synthetic A');
    assert.equal(JSON.stringify(payload).includes('PRIVATE_PROVIDER_KEY'),false);
    assert.equal(JSON.stringify(payload).includes(KEY),false);
    assert.equal(header,'Bearer '+KEY);
  } finally {rmSync(root,{recursive:true,force:true});}
});
test('selection is signed, origin-protected, and disallows stopped room', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-select-')),path=join(root,'routes.json');manifest(path);
  const noFetch=()=>{throw Error('Selection must not call room admin');};
  try {
    let res=capture();
    await dashboardRoomEndpoint(req('POST',{room_id:B}),res,{
      ...options(path,noFetch),path:'/__serein/rooms/select',
    });
    assert.equal(res.statusCode,409);assert.equal(res.headers['Set-Cookie'],undefined);
    res=capture();
    await dashboardRoomEndpoint(req('POST',{room_id:A},{
      origin:'https://malicious.invalid',
    }),res,{...options(path,noFetch),path:'/__serein/rooms/select'});
    assert.equal(res.statusCode,403);
    res=capture();
    await dashboardRoomEndpoint(req('POST',{room_id:A}),res,{
      ...options(path,noFetch),path:'/__serein/rooms/select',
    });
    assert.equal(res.statusCode,200);
    assert.match(res.headers['Set-Cookie'],/HttpOnly; SameSite=Strict/);
    assert.equal(res.headers['Set-Cookie'].includes('; Secure'),false);
    assert.equal(JSON.parse(res.body).room_id,A);
    res=capture();
    await dashboardRoomEndpoint(req('GET'),res,{
      ...options(path,noFetch),path:'/__serein/rooms/current',
    });
    assert.equal(JSON.parse(res.body).room_id,A);
  } finally {rmSync(root,{recursive:true,force:true});}
});
test('room creation and start use same admin credential without another login', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-create-')),path=join(root,'routes.json');manifest(path);
  const sent=[];
  const mocked=async (url,params)=>{
    sent.push({url:String(url),method:params.method,body:params.body,auth:params.headers.Authorization});
    if(String(url).endsWith('/start')) {
      manifest(path,[A,B]);
      return new Response(JSON.stringify({room_id:B,name:'Second',state:'ready'}),{status:200});
    }
    return new Response(JSON.stringify({room:{room_id:B,name:'Second',state:'stopped'},gateway_key:'ROOM_B_KEY'}),{status:201});
  };
  try {
    let res=capture();
    await dashboardRoomEndpoint(req('POST',{name:'Second',source:'first',
      reuse_provider_keys:false}),res,{...options(path,mocked),path:'/__serein/rooms'});
    assert.equal(res.statusCode,201);
    assert.equal(JSON.parse(res.body).gateway_key,'ROOM_B_KEY');
    res=capture();
    await dashboardRoomEndpoint(req('POST',{}),res,{
      ...options(path,mocked),path:'/__serein/rooms/'+B+'/start',
    });
    assert.equal(res.statusCode,200);
    res=capture();
    await dashboardRoomEndpoint(req('POST',{room_id:B}),res,{
      ...options(path,mocked),path:'/__serein/rooms/select',
    });
    assert.equal(res.statusCode,200);
    assert.equal(sent.length,2);
    assert.equal(sent[0].auth,'Bearer '+KEY);
    assert.equal(sent[1].auth,'Bearer '+KEY);
  } finally {rmSync(root,{recursive:true,force:true});}
});
