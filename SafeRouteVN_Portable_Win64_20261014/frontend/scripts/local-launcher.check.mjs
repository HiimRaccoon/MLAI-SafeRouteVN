import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtempSync,writeFileSync,rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { localLauncherProxy } from './localLauncherProxy.mjs';
let backend, frontend, origin, plugin, dir, seen=[];
const token='test-only-dispatcher-secret-123456';
const listen=s=>new Promise(resolve=>s.listen(0,'127.0.0.1',resolve));
function call(path='/api/scenarios',headers={},method='GET') {
  return new Promise((resolve,reject)=>{
    const req=http.request(origin+path,{method,headers:{'X-SafeRoute-Local':'1',...headers}},res=>{
      let text='';res.on('data',b=>text+=b);res.on('end',()=>resolve({status:res.statusCode,text,headers:res.headers}));
    });req.on('error',reject);req.end(method==='POST'?'{}':undefined);
  });
}
before(async()=>{
  dir=mkdtempSync(join(tmpdir(),'saferoute-proxy-'));
  writeFileSync(join(dir,'access.json'),JSON.stringify({credentials:[{actor_id:'local',role:'dispatcher',token}]}));
  writeFileSync(join(dir,'auth.json'),JSON.stringify({tokens:[{actor_id:'local',role:'dispatcher',token_sha256:createHash('sha256').update(token).digest('hex')}]}));
  backend=http.createServer((req,res)=>{seen.push(req.headers);req.resume();res.setHeader('Content-Type','application/json');res.end('{"ok":true}');});
  await listen(backend);
  frontend=http.createServer((req,res)=>plugin.middleware(req,res,()=>{res.writeHead(404);res.end();}));
  await listen(frontend);origin=`http://127.0.0.1:${frontend.address().port}`;
  plugin=localLauncherProxy({accessFile:join(dir,'access.json'),authFile:join(dir,'auth.json'),origin,backend:`http://127.0.0.1:${backend.address().port}`});
});
after(async()=>{
  await Promise.all([backend,frontend].filter(Boolean).map(s=>new Promise(r=>s.close(r))));
  // Only the unique directory created by this test is removed.
  if(dir)rmSync(dir,{recursive:true,force:true});
});
test('injects server bearer without exposing it in the browser response',async()=>{
  const response=await call('/api/scenarios',{Authorization:'Bearer wrong',Cookie:'browser=secret'});
  assert.equal(response.status,200);assert.equal(seen.at(-1).authorization,'Bearer '+token);
  assert.equal(seen.at(-1).cookie,undefined);assert.equal(response.text.includes(token),false);
  assert.equal(response.headers['cache-control'],'no-store');
});
test('allows a same-origin app POST',async()=>{
  assert.equal((await call('/api/scenarios/S0/load',{Origin:origin,'Sec-Fetch-Site':'same-origin','Content-Type':'application/json'},'POST')).status,200);
});
test('blocks foreign origin, forged host, cross-site and preflight before forwarding',async()=>{
  const before=seen.length;
  for(const headers of [{Origin:'https://attacker.invalid'},{Host:'attacker.invalid'},{'Sec-Fetch-Site':'cross-site'},{'X-SafeRoute-Local':'0'}]) {
    assert.equal((await call('/api/scenarios',headers)).status,403);
  }
  assert.equal((await call('/api/scenarios',{Origin:origin},'OPTIONS')).status,403);
  assert.equal((await call('/api/scenarios',{},'POST')).status,403);
  assert.equal(seen.length,before);
});
test('does not proxy filesystem or arbitrary non-API paths',async()=>{
  assert.equal((await call('/dev_access.json')).status,404);
});
test('rejects non-loopback backend and mismatched credentials',()=>{
  const options={accessFile:join(dir,'access.json'),authFile:join(dir,'auth.json'),origin,backend:'http://example.com'};
  assert.throws(()=>localLauncherProxy(options),/loopback/);
  writeFileSync(join(dir,'wrong.json'),'{"tokens":[]}');
  assert.throws(()=>localLauncherProxy({...options,backend:'http://127.0.0.1:8000',authFile:join(dir,'wrong.json')}),/credentials/);
});
