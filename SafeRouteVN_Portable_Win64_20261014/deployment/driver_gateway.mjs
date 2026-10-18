// Production driver entry point. Only the loopback listener is exposed to Tailscale Funnel.
import http from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { gzip } from 'node:zlib';
import { promisify } from 'node:util';

const compress = promisify(gzip);
async function replySnapshot(req, res, status, data) {
  let bytes = Buffer.from(JSON.stringify(data));
  const encodings = String(req.headers['accept-encoding'] || '').toLowerCase().split(',').map(value => value.trim().split(';'));
  const encoding = encodings.find(([name]) => name === 'gzip') ?? encodings.find(([name]) => name === '*');
  const quality = encoding?.slice(1).find(value => value.trim().startsWith('q='));
  const accepted = encoding && (quality === undefined || Number(quality.trim().slice(2)) > 0);
  const zipped = accepted && bytes.length >= 1024;
  if (zipped) bytes = await compress(bytes, { level: 6 });
  res.writeHead(status, { ...headers, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
    Vary: 'Accept-Encoding', 'Content-Length': bytes.length, ...(zipped ? { 'Content-Encoding': 'gzip' } : {}) });
  res.end(bytes);
}

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };
const headers = {
  'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'strict-origin-when-cross-origin',
  'X-Frame-Options': 'DENY', 'Permissions-Policy': 'geolocation=(), camera=(), microphone=()',
  'Content-Security-Policy': "default-src 'self'; script-src 'self'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self' https://tiles.openfreemap.org; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'",
};
function envelope(code, message) {
  return { schema_version: 'saferoute-m3-http-response/1', request_id: randomUUID(), status: 'ERROR', data: null,
    diagnostics: [{ severity: 'ERROR', code, path: 'gateway', message }] };
}
function reply(res, status, data) {
  res.writeHead(status, { ...headers, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(data));
}
function fail(res, status, code, message) { reply(res, status, envelope(code, message)); }

export async function createGateway(config) {
  const origin = new URL(config.public_origin);
  const upstream = new URL(config.backend);
  if (origin.origin !== config.public_origin || origin.username || origin.password ||
      !(origin.protocol === 'https:' || origin.protocol === 'http:' && origin.hostname === '127.0.0.1')) throw new Error('Exact HTTPS public origin required (loopback HTTP only for staging)');
  if (upstream.protocol !== 'http:' || upstream.hostname !== '127.0.0.1' || upstream.pathname !== '/' || upstream.search || upstream.hash || upstream.username || upstream.password) throw new Error('Backend must be a loopback HTTP origin');
  const root = await realpath(config.dist);
  await stat(path.join(root, 'index.html'));
  const windows = new Map();
  const active = new Map();
  let activeTotal = 0;
  function limit(key, max) {
    const now = Date.now();
    if (windows.size > 5000) for (const [k, v] of windows) if (v.end <= now) windows.delete(k);
    let entry = windows.get(key);
    if (!entry || entry.end <= now) {
      if (windows.size >= 10000) return false;
      entry = { end: now + 60000, count: 0 }; windows.set(key, entry);
    }
    return ++entry.count <= max;
  }
  const server = http.createServer({ maxHeaderSize: 16384, requestTimeout: 150000, headersTimeout: 15000 }, async (req, res) => {
    const started = Date.now();
    // Host is forwarded by Tailscale Funnel; forwarding headers never select an upstream.
    if (req.headers.host?.toLowerCase() !== origin.host.toLowerCase()) return fail(res, 421, 'INVALID_HOST', 'Unrecognized application host');
    if (!['GET', 'HEAD', 'POST'].includes(req.method)) return fail(res, 405, 'METHOD_NOT_ALLOWED', 'Method not allowed');
    if (req.headers.origin && req.headers.origin !== origin.origin) return fail(res, 403, 'INVALID_ORIGIN', 'Request origin denied');
    if (req.method === 'POST' && req.headers.origin !== origin.origin) return fail(res, 403, 'INVALID_ORIGIN', 'Same-origin request required');
    let pathname;
    try {
      const raw = req.url.split('?')[0];
      pathname = decodeURIComponent(raw);
      if (!raw.startsWith('/') || raw.startsWith('//') || pathname.includes('\\') || pathname.includes('\0') || pathname.includes('%') || pathname.split('/').some(p => p === '.' || p === '..')) throw new Error();
    } catch { return fail(res, 400, 'INVALID_PATH', 'Invalid request path'); }
    // Funnel is a loopback proxy; client-supplied forwarding headers are not trusted.
    const ip = 'gateway';
    if (!limit(`all:${ip}`, 12000)) return fail(res, 429, 'RATE_LIMITED', 'Too many requests; retry later');
    let activeKey;
    try {
      if (pathname === '/ready') {
        if (req.method !== 'GET') return fail(res, 405, 'METHOD_NOT_ALLOWED', 'GET required');
        if (!limit(`ready:${ip}`, 600)) return fail(res, 429, 'RATE_LIMITED', 'Too many readiness requests');
        const check = await fetch(upstream.origin + '/ready', { redirect: 'error', signal: AbortSignal.timeout(60000) });
        const body = await check.json();
        const ready = check.ok && body?.data?.ready === true;
        return reply(res, ready ? 200 : 503, { schema_version: 'saferoute-m3-http-response/1', request_id: randomUUID(), status: ready ? 'OK' : 'ERROR', data: { ready, execution_mode: 'SIMULATED_REPLAY' }, diagnostics: ready ? [] : envelope('BACKEND_NOT_READY', 'Server is not ready').diagnostics });
      }
      if (pathname.startsWith('/api/')) {
        if (req.method === 'HEAD' || req.url.includes('?')) return fail(res, 400, 'INVALID_REQUEST', 'Unsupported API request');
        const auth = req.headers.authorization;
        if (typeof auth !== 'string' || !/^Bearer [A-Za-z0-9_-]{20,256}$/.test(auth)) return fail(res, 401, 'UNAUTHORIZED', 'Driver access code required');
        const hash = createHash('sha256').update(auth).digest('hex');
        if (activeTotal >= 32 || (active.get(hash) || 0) >= 8) return fail(res, 429, 'RATE_LIMITED', 'Too many concurrent requests');
        activeTotal++; active.set(hash, (active.get(hash) || 0) + 1); activeKey = hash;
        if (Number(req.headers['content-length'] || 0) > 65536) return fail(res, 413, 'BODY_TOO_LARGE', 'Request exceeds 64 KiB');
        if (!limit(`identity:${ip}`, 6000)) return fail(res, 429, 'RATE_LIMITED', 'Too many API requests');
        const whoResponse = await fetch(upstream.origin + '/api/auth/me', { headers: { Authorization: auth }, redirect: 'error', signal: AbortSignal.timeout(15000) });
        const who = await whoResponse.json();
        if (!whoResponse.ok) return reply(res, whoResponse.status, who);
        if (who.data?.role !== 'driver' || !who.data?.session?.session_id) return fail(res, 403, 'FORBIDDEN', 'Assigned driver account required');
        const sid = who.data.session.session_id;
        const allowedGlobal = ['/api/auth/me', '/api/scenarios', '/api/runtime/capabilities'].includes(pathname);
        const sessionPrefix = `/api/sessions/${sid}`;
        const suffix = pathname.slice(sessionPrefix.length);
        const sessionMatch = pathname === sessionPrefix || pathname.startsWith(sessionPrefix + '/');
        const readAllowed = ['', '/world', '/state', '/orders', '/vehicles', '/locations', '/events', '/replay/history', '/replay/playback', '/run', '/acceptances'].includes(suffix);
        const writeAllowed = ['/replay/step', '/replay/start', '/replay/speed', '/replay/playback/pause', '/replay/auto-deliver', '/delivery-day'].includes(suffix);
        if (!(req.method === 'GET' && (allowedGlobal || sessionMatch && readAllowed) || req.method === 'POST' && sessionMatch && writeAllowed)) return fail(res, 403, 'FORBIDDEN', 'Operation is outside the assigned driver session');
        if (!limit(`${req.method}:${hash}`, req.method === 'POST' ? 60 : 600)) return fail(res, 429, 'RATE_LIMITED', 'Account request limit exceeded');
        if (pathname === '/api/auth/me') return reply(res, 200, who);
        let body;
        if (req.method === 'POST') {
          if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json') return fail(res, 415, 'UNSUPPORTED_MEDIA_TYPE', 'JSON required');
          const chunks = []; let size = 0;
          for await (const chunk of req) { size += chunk.length; if (size > 65536) { fail(res, 413, 'BODY_TOO_LARGE', 'Request exceeds 64 KiB'); return; } chunks.push(chunk); }
          body = Buffer.concat(chunks);
        }
        const upstreamResponse = await fetch(upstream.origin + pathname, { method: req.method, headers: { Authorization: auth, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) }, body, redirect: 'error', signal: AbortSignal.timeout(125000) });
        const result = await upstreamResponse.json();
        return await replySnapshot(req, res, upstreamResponse.status, result);
      }
      if (req.method === 'POST') return fail(res, 405, 'METHOD_NOT_ALLOWED', 'Read-only resource');
      const relative = pathname === '/' || pathname === '/driver' ? 'index.html' : pathname.startsWith('/assets/') ? pathname.slice(1) : null;
      if (!relative) return fail(res, 404, 'NOT_FOUND', 'Resource not found');
      const file = await realpath(path.join(root, relative));
      if (!file.startsWith(root + path.sep)) return fail(res, 403, 'FORBIDDEN', 'Resource denied');
      const type = mime[path.extname(file)];
      if (!type) return fail(res, 404, 'NOT_FOUND', 'Resource not found');
      const bytes = await readFile(file);
      res.writeHead(200, { ...headers, 'Content-Type': type, 'Content-Length': bytes.length, 'Cache-Control': relative === 'index.html' ? 'no-store' : 'public, max-age=31536000, immutable' });
      res.end(req.method === 'HEAD' ? undefined : bytes);
    } catch (error) {
      if (!res.headersSent) fail(res, error.code === 'ENOENT' ? 404 : 503, 'SERVICE_UNAVAILABLE', 'Service temporarily unavailable');
      else res.destroy();
    } finally {
      if (activeKey) { activeTotal--; const remaining = active.get(activeKey) - 1; if (remaining) active.set(activeKey, remaining); else active.delete(activeKey); }
      // No request URL, credential, private path, query or response body in logs.
      console.log(JSON.stringify({ event: 'driver_request', method: req.method, status: res.statusCode, duration_ms: Date.now() - started }));
    }
  });
  server.maxConnections = 128;
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
  const server = await createGateway(config);
  server.listen(config.port, '127.0.0.1', () => console.log(JSON.stringify({ status: 'DRIVER_GATEWAY_READY', port: config.port })));
  process.on('SIGTERM', () => server.close(() => process.exit(0)));
}
