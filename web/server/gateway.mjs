import http from 'node:http';
import https from 'node:https';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { preview } from 'vite';
import { requireWebAuth } from './webAuth.mjs';
import { effectiveRoom, readyRooms, signedPreviewRoom } from './roomSession.mjs';
import { dashboardRoomEndpoint } from './roomDashboardRoutes.mjs';

// Legacy deployments continue to use their original single-room settings.
const multiRoom = Boolean(process.env.SEREIN_MULTIROOM_ROUTES_FILE);
const filename = process.env.SEREIN_WEB_AUTH_FILE;
// Reuse the original dashboard login with user-chosen username and password.
// The separate administrator key is internal, never a browser password.
if (!filename) throw new Error('SEREIN_WEB_AUTH_FILE is required');
const adminKey = multiRoom ? readFileSync(process.env.SEREIN_MULTIROOM_ADMIN_KEY_FILE, 'utf8').trim() : null;
if (multiRoom && (!adminKey || adminKey.length < 48)) throw new Error('Multi-room admin key must be initialized');
const core = new URL(multiRoom
  ? (process.env.SEREIN_MULTIROOM_CHAT_URL || 'http://127.0.0.1:18219')
  : process.env.SEREIN_MEMORY_URL);
let publicOrigin = null;
if (process.env.SEREIN_PUBLIC_ORIGIN) {
  publicOrigin = new URL(process.env.SEREIN_PUBLIC_ORIGIN);
  const loopback = ['localhost','127.0.0.1','[::1]'].includes(publicOrigin.hostname);
  if ((publicOrigin.protocol !== 'https:' && !(publicOrigin.protocol === 'http:' && loopback))
      || publicOrigin.username || publicOrigin.password || publicOrigin.pathname !== '/'
      || publicOrigin.search || publicOrigin.hash) throw new Error('SEREIN_PUBLIC_ORIGIN must be an HTTPS origin or loopback HTTP origin');
}
if (!multiRoom) {
  process.env.SEREIN_MEMORY_TOKEN = readFileSync(process.env.SEREIN_MEMORY_TOKEN_FILE, 'utf8').trim();
  if (!process.env.SEREIN_MEMORY_TOKEN) throw new Error('Memory token is empty');
}
if (multiRoom && !publicOrigin && !['127.0.0.1','localhost','::1'].includes(
    process.env.SEREIN_GATEWAY_BIND || '0.0.0.0')) {
  throw new Error('Multi-room administrator UI requires configured HTTPS public origin or loopback binding');
}
const web = await preview({ root: fileURLToPath(new URL('../', import.meta.url)),
  preview: { host: '127.0.0.1', port: Number(process.env.SEREIN_PREVIEW_PORT || 4173), strictPort: true, allowedHosts: true } });
const bearerPaths = new Set(['/v1/models', '/v1/chat/completions',
  '/api/hook/recall', '/v1/host/deliveries']);
const hopHeaders = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'];

const server = http.createServer((req, res) => {
  let path;
  try { path = new URL(req.url, 'http://localhost').pathname; }
  catch {
    res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff' });
    res.end('Invalid request URL'); return;
  }
  if (path === '/ready' && req.method === 'GET') {
    const chosen = multiRoom ? readyRooms(process.env.SEREIN_MULTIROOM_ROUTES_FILE)[0] : null;
    if (multiRoom && !chosen) { res.writeHead(503); res.end(); return; }
    const previewHeaders = multiRoom ? {
      'X-Serein-Verified-Room':chosen.room_id,
      'X-Serein-Verified-Proof':signedPreviewRoom(chosen.room_id, adminKey),
    } : {};
    Promise.all([fetch(new URL('/health', core), {signal:AbortSignal.timeout(3000)}),
      fetch(`http://127.0.0.1:${web.httpServer.address().port}/`,
        {headers:previewHeaders, signal:AbortSignal.timeout(3000)})])
      .then(results => { res.writeHead(results.every(r => r.ok) ? 200 : 503); res.end(); })
      .catch(() => { res.writeHead(503); res.end(); });
    return;
  }
  const mcp = ['/serein/mcp', '/serein/mcp/', '/mcp', '/mcp/'].includes(path);
  const oauth = path === '/authorize' || path === '/token' || path === '/register'
    || path.startsWith('/.well-known/oauth-');
  let requestHost;
  try { requestHost = new URL(`http://${req.headers.host}`).hostname; } catch {}
  const loopbackOAuth = ['localhost','127.0.0.1','[::1]'].includes(requestHost);
  if (oauth && !publicOrigin && !loopbackOAuth) {
    res.writeHead(400, {'Content-Type':'application/json'});
    res.end('{"detail":"OAuth requires a configured HTTPS public origin"}');return;
  }
  const chat = bearerPaths.has(path) || mcp;
  if (multiRoom && oauth) {
    res.writeHead(404, {'Cache-Control':'no-store'}); res.end('OAuth is not yet available for rooms'); return;
  }
  if (mcp && req.headers.origin) {
    let originOK = false;
    try { originOK = new URL(req.headers.origin).host === req.headers.host; } catch {}
    if (!originOK) { res.writeHead(403); res.end('Invalid origin'); return; }
  }
  // Both single-room and multi-room dashboards use the original scrypt-hashed
  // web-auth.json. Room changes never ask for a second password.
  if (!chat && !oauth && !requireWebAuth(req, res, filename)) return;
  if (!chat && !oauth && !['GET','HEAD','OPTIONS'].includes(req.method)) {
    let originOK = true;
    try { if (req.headers.origin) originOK = new URL(req.headers.origin).host === req.headers.host; }
    catch { originOK = false; }
    if (!originOK || !String(req.headers['content-type'] || '').startsWith('application/json')) {
      res.writeHead(403); res.end('Same-origin JSON required'); return;
    }
  }
  if (chat && !mcp && !req.headers.authorization?.startsWith('Bearer ')) {
    res.writeHead(401); res.end('Bearer token required'); return;
  }
  if (multiRoom && path.startsWith('/__serein/rooms')
      && (['/__serein/rooms','/__serein/rooms/select','/__serein/rooms/current'].includes(path)
          || /^\/__serein\/rooms\/[a-f0-9]{32}\/(start|stop|mcp-tools)$/.test(path)
          || /^\/__serein\/rooms\/[a-f0-9]{32}\/backups(?:\/\d{8}T\d{6}Z-[a-f0-9]{12}\.tar\.gz\/(preview|download))?$/.test(path))) {
    void dashboardRoomEndpoint(req, res, {
      path, publicOrigin, adminKey,
      routesFile:process.env.SEREIN_MULTIROOM_ROUTES_FILE,
      adminURL:process.env.SEREIN_MULTIROOM_ADMIN_URL || 'http://127.0.0.1:18220',
    }).catch(() => { if (!res.headersSent) res.writeHead(502); res.end('Room manager unavailable'); });
    return;
  }
  const backend = chat || oauth;
  const target = backend ? core : new URL(`http://127.0.0.1:${process.env.SEREIN_PREVIEW_PORT || 4173}`);
  const headers = { ...req.headers, host: backend ? target.host : req.headers.host };
  if (multiRoom && !backend) {
    const current = effectiveRoom(req.headers.cookie, adminKey,
      readyRooms(process.env.SEREIN_MULTIROOM_ROUTES_FILE));
    if (!current) { res.writeHead(503); res.end('No ready room'); return; }
    // Browser cannot supply or override the internal room context.
    for (const name of Object.keys(headers)) {
      if (name.startsWith('x-serein-verified-') || name.startsWith('x-forwarded-'))
        delete headers[name];
    }
    headers['x-serein-verified-room'] = current.room_id;
    headers['x-serein-verified-proof'] = signedPreviewRoom(current.room_id, adminKey);
  }
  if (mcp || oauth) {
    headers['x-forwarded-host'] = publicOrigin?.host || req.headers.host;
    headers['x-forwarded-proto'] = publicOrigin?.protocol.slice(0,-1) || 'http';
  }
  for (const key of hopHeaders) delete headers[key];
  if (!backend) delete headers.authorization;
  const proxy = (target.protocol === 'https:' ? https : http).request(target, {
    method: req.method, path: req.url, headers,
  }, upstream => {
    const responseHeaders = { ...upstream.headers };
    for (const key of hopHeaders) delete responseHeaders[key];
    res.writeHead(upstream.statusCode, responseHeaders);
    upstream.pipe(res);
  });
  proxy.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end('Backend unavailable'); });
  res.on('close', () => { if (!res.writableEnded) proxy.destroy(); });
  req.pipe(proxy);
});
server.listen(Number(process.env.SEREIN_GATEWAY_PORT || 8080), process.env.SEREIN_GATEWAY_BIND || '0.0.0.0');
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
  server.close(); web.httpServer.close();
});
