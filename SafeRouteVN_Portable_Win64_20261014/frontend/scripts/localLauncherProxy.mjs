import http from 'node:http';
import { readFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';

// Dev-server only. The bearer remains in Node memory, never in browser assets.
export function localLauncherProxy({ accessFile, authFile, origin, backend }) {
  const publicUrl = new URL(origin), target = new URL(backend);
  for (const url of [publicUrl,target]) {
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('Local launcher requires exact loopback origins');
  }
  const access=JSON.parse(readFileSync(accessFile,'utf8'));
  const auth=JSON.parse(readFileSync(authFile,'utf8'));
  const credential=access.credentials?.find(c=>c.role==='dispatcher');
  if (!credential || typeof credential.token!=='string' || !auth.tokens?.some(t=>t.actor_id===credential.actor_id && t.role==='dispatcher' && t.token_sha256===createHash('sha256').update(credential.token).digest('hex'))) throw new Error('Local launcher credentials do not match backend auth');
  function failure(res,status,code,message) {
    if(res.headersSent){res.destroy();return;}
    res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});
    res.end(JSON.stringify({schema_version:'saferoute-m3-http-response/1',request_id:randomUUID(),status:'ERROR',data:null,diagnostics:[{code,message}]}));
  }
  const middleware=(req,res,next)=>{
    const path=req.url??'';
    if (!(path.startsWith('/api/') || path==='/ready' || path==='/health')) return next();
    const remote=req.socket.remoteAddress;
    if(!['127.0.0.1','::ffff:127.0.0.1'].includes(remote) || req.headers.host!==publicUrl.host ||
      req.headers['x-saferoute-local']!=='1' || (req.headers.origin && req.headers.origin!==publicUrl.origin) ||
      (req.headers['sec-fetch-site'] && req.headers['sec-fetch-site']!=='same-origin') ||
      !['GET','POST'].includes(req.method) || (req.method==='POST' && req.headers.origin!==publicUrl.origin)) {
      return failure(res,403,'LOCAL_ORIGIN_REQUIRED','Use the app opened by the local launcher.');
    }
    // Do not forward browser cookies, Origin, stale bearer or redirect credentials.
    const headers={Accept:'application/json',Authorization:'Bearer '+credential.token};
    for(const key of ['content-type','content-length'])if(req.headers[key])headers[key]=req.headers[key];
    const upstream=http.request({hostname:'127.0.0.1',port:target.port||80,path,method:req.method,headers},response=>{
      if(response.statusCode>=300 && response.statusCode<400){response.resume();failure(res,502,'LOCAL_REDIRECT_REJECTED','Backend redirect rejected.');return;}
      res.writeHead(response.statusCode??502,{'Content-Type':response.headers['content-type']??'application/json','Cache-Control':'no-store'});
      response.pipe(res);
    });
    upstream.setTimeout(130000,()=>upstream.destroy(new Error('timeout')));
    upstream.on('error',()=>failure(res,502,'LOCAL_BACKEND_UNAVAILABLE','Backend is unavailable. Check the launcher log.'));
    req.on('aborted',()=>upstream.destroy());
    res.on('close',()=>{if(!res.writableEnded)upstream.destroy();});
    req.pipe(upstream);
  };
  return {name:'saferoute-local-launcher',configureServer(server){server.middlewares.use(middleware);},middleware};
}
