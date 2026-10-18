import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtemp, writeFile, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createGateway } from './driver_gateway.mjs';
import { gunzipSync } from 'node:zlib';

// Use raw HTTP because browser-style fetch does not allow overriding Host.
function fetch(url, options = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, options, res => {
      const chunks = [];
      res.on('data', b => chunks.push(b));
      res.on('end', () => resolve({ status: res.statusCode, headers: {get: key => res.headers[key.toLowerCase()]}, bytes: () => Buffer.concat(chunks), text: () => Buffer.concat(chunks).toString() }));
    });
    req.on('error', reject); req.end(options.body);
  });
}

test('public gateway enforces host, token, session, operation and static boundaries', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'saferoute-driver-'));
  await writeFile(path.join(root, 'index.html'), '<h1>Driver</h1>');
  await mkdir(path.join(root, 'assets'));
  await writeFile(path.join(root, 'assets/app.js'), 'window.driver=true;');
  await writeFile(path.join(root, 'private.json'), '{"secret":"never-public"}');
  let valid = true;
  const calls = [];
  const upstream = http.createServer((req, res) => {
    calls.push(req.url);
    const status = req.url === '/ready' ? 200 : valid ? 200 : 401;
    res.writeHead(status, {'Content-Type': 'application/json'});
    const data = req.url === '/api/auth/me' ? {role: 'driver', session: {session_id: 'assigned'}} : {ready: true, route: 'coordinates'.repeat(4096)};
    res.end(JSON.stringify({schema_version:'saferoute-m3-http-response/1', status:status===200?'OK':'ERROR',request_id:'test',diagnostics:[],data}));
  });
  await new Promise(r => upstream.listen(0, '127.0.0.1', r));
  const server = await createGateway({ public_origin: 'https://driver.example.vn', backend: `http://127.0.0.1:${upstream.address().port}`, dist: root });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const auth = { Host:'driver.example.vn', Authorization:'Bearer '+'x'.repeat(36) };
  try {
    assert.equal((await fetch(base+'/driver')).status,421);
    assert.equal((await fetch(base+'/driver',{headers:{Host:'driver.example.vn'}})).status,200);
    assert.equal((await fetch(base+'/private.json',{headers:auth})).status,404);
    assert.equal((await fetch(base+'/build-graph.json',{headers:auth})).status,404);
    assert.equal((await fetch(base+'/docs',{headers:auth})).status,404);
    assert.equal((await fetch(base+'/api/auth/me',{headers:{Host:'driver.example.vn'}})).status,401);
    assert.equal((await fetch(base+'/api/sessions/other/world',{headers:auth})).status,403);
    assert.equal(calls.includes('/api/sessions/other/world'),false);
    assert.equal((await fetch(base+'/api/sessions/assigned/world',{headers:auth})).status,200);
    const compressed=await fetch(base+'/api/sessions/assigned/world',{headers:{...auth,'Accept-Encoding':'gzip, deflate'}});
    assert.equal(compressed.headers.get('content-encoding'),'gzip');
    assert.equal(compressed.headers.get('cache-control'),'no-store');
    assert.equal(compressed.headers.get('vary'),'Accept-Encoding');
    assert.ok(compressed.bytes().length<1000);
    assert.equal(JSON.parse(gunzipSync(compressed.bytes()).toString()).data.route,'coordinates'.repeat(4096));
    const plain=await fetch(base+'/api/sessions/assigned/world',{headers:{...auth,'Accept-Encoding':'gzip;q=0, identity'}});
    assert.equal(plain.headers.get('content-encoding'),undefined);
    assert.equal(JSON.parse(plain.text()).data.route,'coordinates'.repeat(4096));
    const post={method:'POST',headers:{...auth,Origin:'https://driver.example.vn','Content-Type':'application/json'},body:'{}'};
    assert.equal((await fetch(base+'/api/sessions/assigned/optimize',post)).status,403);
    assert.equal((await fetch(base+'/api/sessions/assigned/replay/start',post)).status,200);
    assert.equal((await fetch(base+'/api/sessions/assigned/replay/start',{...post,headers:{...post.headers,Origin:'https://evil.example'}})).status,403);
    assert.equal((await fetch(base+'/api/auth/me?token=bad',{headers:auth})).status,400);
    assert.equal((await fetch(base+'/api/sessions/assigned/replay/start',{...post, headers:{...post.headers,'Content-Length':'70000'},body:'x'.repeat(70000)})).status,413);
    valid=false;
    assert.equal((await fetch(base+'/api/sessions/assigned/world',{headers:auth})).status,401);
    const page=await fetch(base+'/driver',{headers:auth});
    assert.equal(page.headers.get('cache-control'),'no-store');
    assert.ok(page.headers.get('content-security-policy').includes("frame-ancestors 'none'"));
  } finally {
    await Promise.all([new Promise(r=>server.close(r)),new Promise(r=>upstream.close(r))]);
    await rm(root,{recursive:true,force:true});
  }
});
