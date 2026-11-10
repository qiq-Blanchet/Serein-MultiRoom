import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable, Writable } from 'node:stream';
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
    await dashboardRoomEndpoint(req('POST',{room_slug:'xiaogi-main',source:'first',
      reuse_provider_keys:false}),res,{...options(path,mocked),path:'/__serein/rooms'});
    assert.equal(res.statusCode,201);
    assert.equal(JSON.parse(res.body).gateway_key,'ROOM_B_KEY');
    assert.equal(JSON.parse(sent[0].body).room_slug,'xiaogi-main');
    assert.equal(JSON.parse(sent[0].body).name,'xiaogi-main');
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

test('Dashboard reaches only the selected RFC1918 bridge control service', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-bridge-'));
  const routes=join(root,'routes.json');manifest(routes);
  const seen=[];
  const handler=async (url, params)=>{
    seen.push(String(url));
    assert.equal(params.headers.Authorization,'Bearer '+KEY);
    return new Response(JSON.stringify({rooms:[]}),{status:200});
  };
  try {
    let response=capture();
    await dashboardRoomEndpoint(req(),response,{
      ...options(routes,handler),adminURL:'http://172.28.0.1:18220',
      path:'/__serein/rooms',
    });
    assert.equal(response.statusCode,200);
    assert.deepEqual(seen,['http://172.28.0.1:18220/api/rooms']);
    response=capture();
    await dashboardRoomEndpoint(req(),response,{
      ...options(routes,handler),adminURL:'http://8.8.8.8:18220',
      path:'/__serein/rooms',
    });
    assert.equal(response.statusCode,502);
    assert.equal(seen.length,1);
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('room creation requires one creation-time ID but no duplicate room-name setting', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-room-id-')),path=join(root,'routes.json');
  manifest(path);
  const sent=[];
  const handler=async (url,params)=>{
    sent.push(JSON.parse(params.body));
    return new Response(JSON.stringify({
      room:{room_id:B,room_slug:'xiaogi-main',display_name:'xiaogi-main',state:'stopped'},
      gateway_key:'SYNTHETIC_ROOM_GATEWAY_KEY',
    }),{status:201});
  };
  try {
    let res=capture();
    await dashboardRoomEndpoint(req('POST',{source:'first'}),res,{
      ...options(path,handler),path:'/__serein/rooms',
    });
    assert.equal(res.statusCode,400);
    for(const id of ['bad space','XiaoGi','../../../etc','x','room--two']) {
      res=capture();
      await dashboardRoomEndpoint(req('POST',{room_slug:id}),res,{
        ...options(path,handler),path:'/__serein/rooms',
      });
      assert.equal(res.statusCode,400);
    }
    assert.equal(sent.length,0);
    res=capture();
    await dashboardRoomEndpoint(req('POST',{room_slug:'xiaogi-main'}),res,{
      ...options(path,handler),path:'/__serein/rooms',
    });
    assert.equal(res.statusCode,201);
    assert.deepEqual(sent,[{room_slug:'xiaogi-main',name:'xiaogi-main'}]);
    assert.equal(JSON.parse(res.body).gateway_key,'SYNTHETIC_ROOM_GATEWAY_KEY');
    assert.equal(JSON.parse(res.body).room.room_slug,'xiaogi-main');
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('MCP editing is limited to the currently selected room and one web login', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-mcp-')),path=join(root,'routes.json');
  manifest(path,[A,B]);
  const forwarded=[];
  const handler=async (url,params)=>{
    forwarded.push({url:String(url),method:params.method,body:params.body,
                    bearer:params.headers.Authorization});
    return new Response(JSON.stringify(params.method === 'GET'
      ? {mode:'original',tools:[],available:['read_memory','recall_memory']}
      : {permissions:{mode:'custom',tools:['read_memory']},restarted:true}),{status:200});
  };
  try {
    const current=req('GET',undefined,{cookie:roomCookie(KEY,B)});
    let response=capture();
    await dashboardRoomEndpoint(current,response,{
      ...options(path,handler),path:'/__serein/rooms/'+B+'/mcp-tools',
    });
    assert.equal(response.statusCode,200);
    assert.equal(JSON.parse(response.body).mode,'original');

    response=capture();
    await dashboardRoomEndpoint(req('POST',{
      mode:'custom',tools:['read_memory'],
    },{cookie:roomCookie(KEY,B)}),response,{
      ...options(path,handler),path:'/__serein/rooms/'+B+'/mcp-tools',
    });
    assert.equal(response.statusCode,200);
    assert.equal(JSON.parse(response.body).restarted,true);

    response=capture();
    await dashboardRoomEndpoint(req('POST',{
      mode:'custom',tools:['read_memory'],
    },{cookie:roomCookie(KEY,B)}),response,{
      ...options(path,handler),path:'/__serein/rooms/'+A+'/mcp-tools',
    });
    assert.equal(response.statusCode,403);
    assert.equal(forwarded.length,2);
    assert.ok(forwarded.every(row=>row.bearer==='Bearer '+KEY));
    assert.ok(forwarded.every(row=>row.url.endsWith('/api/rooms/'+B+'/mcp-tools')));
    assert.deepEqual(JSON.parse(forwarded[1].body),
                     {mode:'custom',tools:['read_memory']});
  } finally {rmSync(root,{recursive:true,force:true});}
});

test('paused room selection is explicitly reset and never silently falls into another AI', async()=>{
  const root=mkdtempSync(join(tmpdir(),'dashboard-reset-')),path=join(root,'routes.json');
  manifest(path,[A]);
  try {
    let res=capture();
    await dashboardRoomEndpoint(req('GET',undefined,{cookie:roomCookie(KEY,B)}),res,{
      ...options(path,()=>{throw Error('Reset must stay local');}),
      path:'/__serein/rooms/current',
    });
    assert.equal(res.statusCode,200);
    assert.equal(JSON.parse(res.body).room_id,null);
    res=capture();
    await dashboardRoomEndpoint(req('POST',{}, {cookie:roomCookie(KEY,B)}),res,{
      ...options(path,()=>{throw Error('Reset must stay local');}),
      path:'/__serein/rooms/reset',
    });
    assert.equal(res.statusCode,200);
    assert.equal(JSON.parse(res.body).cleared,true);
    assert.match(res.headers['Set-Cookie'],/Max-Age=0; HttpOnly; SameSite=Strict/);
  }finally{rmSync(root,{recursive:true,force:true});}
});

test('full-room archive operations require same selected AI and stale-tab marker', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-archive-')),path=join(root,'routes.json');
  manifest(path,[A,B]);
  const name='20261110T140000Z-aabbccddeeff.tar.gz';
  const calls=[];
  const handler=async (url,params)=>{
    calls.push({url:String(url),method:params.method,headers:params.headers,
                body:params.body});
    if(String(url).endsWith('/preview'))
      return new Response(JSON.stringify({verified:true,room_id:A,files:7}),{status:200});
    if(params.method==='POST')
      return new Response(JSON.stringify({room_id:A,name,restarted:true}),{status:201});
    return new Response(JSON.stringify({room_id:A,items:[{name,bytes:1234}]}),{status:200});
  };
  const base=options(path,handler);
  const headers={cookie:roomCookie(KEY,A),'x-serein-target-room':A};
  try{
    let res=capture();
    await dashboardRoomEndpoint(req('GET',undefined,headers),res,{
      ...base,path:'/__serein/rooms/'+A+'/backups'});
    assert.equal(res.statusCode,200);
    assert.equal(JSON.parse(res.body).items[0].name,name);

    res=capture();
    await dashboardRoomEndpoint(req('POST',{},headers),res,{
      ...base,path:'/__serein/rooms/'+A+'/backups'});
    assert.equal(res.statusCode,201);
    assert.equal(JSON.parse(res.body).restarted,true);

    res=capture();
    await dashboardRoomEndpoint(req('GET',undefined,headers),res,{
      ...base,path:'/__serein/rooms/'+A+'/backups/'+name+'/preview'});
    assert.equal(res.statusCode,200);
    assert.equal(JSON.parse(res.body).verified,true);

    for(const bad of [
      {cookie:roomCookie(KEY,B),'x-serein-target-room':A},
      {cookie:roomCookie(KEY,A),'x-serein-target-room':B},
      {cookie:roomCookie(KEY,A)},
    ]){
      res=capture();
      await dashboardRoomEndpoint(req('POST',{},bad),res,{
        ...base,path:'/__serein/rooms/'+A+'/backups'});
      assert.equal(res.statusCode,409);
    }
    assert.equal(calls.length,3);
    assert.ok(calls.every(item=>item.headers.Authorization==='Bearer '+KEY));
    assert.ok(calls.every(item=>item.url.includes('/api/rooms/'+A+'/backups')));
    assert.equal(calls[1].body,'{}');
  }finally{rmSync(root,{recursive:true,force:true});}
});

test('browser-streamed full archive is named for the signed current room', async () => {
  const root=mkdtempSync(join(tmpdir(),'dashboard-archive-stream-')),path=join(root,'routes.json');
  manifest(path,[A,B]);
  const name='20261110T140000Z-aabbccddeeff.tar.gz';
  const bytes=Buffer.from('SYNTHETIC_BINARY_ROOM_A');
  let selectedFetchCount=0;
  const handler=async (url,params)=>{
    selectedFetchCount++;
    assert.equal(params.headers.Authorization,'Bearer '+KEY);
    assert.ok(String(url).includes('/api/rooms/'+A+'/backups/'+name+'/download'));
    return new Response(bytes,{status:200,headers:{'Content-Length':String(bytes.length)}});
  };
  class CapturedWritable extends Writable {
    constructor(){super();this.statusCode=0;this.headers={};this.data=[];}
    writeHead(code,headers){this.statusCode=code;this.headers=headers;this.headersSent=true;}
    _write(chunk,enc,done){this.data.push(Buffer.from(chunk));done();}
  }
  try{
    const out=new CapturedWritable();
    await dashboardRoomEndpoint(req('GET',undefined,{cookie:roomCookie(KEY,A)}),out,{
      ...options(path,handler),
      path:'/__serein/rooms/'+A+'/backups/'+name+'/download'});
    assert.equal(out.statusCode,200);
    assert.match(out.headers['Content-Disposition'],new RegExp('serein-a{32}-'+name.replaceAll('.','\\.')));
    assert.deepEqual(Buffer.concat(out.data),bytes);
    const res=capture();
    await dashboardRoomEndpoint(req('GET',undefined,{cookie:roomCookie(KEY,B)}),res,{
      ...options(path,handler),
      path:'/__serein/rooms/'+A+'/backups/'+name+'/download'});
    assert.equal(res.statusCode,409);
    assert.equal(selectedFetchCount,1);
  }finally{rmSync(root,{recursive:true,force:true});}
});
