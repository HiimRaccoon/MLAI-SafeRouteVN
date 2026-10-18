import { afterEach, expect, it, vi } from 'vitest';
import { Member3Client } from './client';

afterEach(()=>vi.unstubAllGlobals());

it('uses launcher proxy without consulting or sending a browser bearer',async()=>{
  vi.stubGlobal('window',{location:{origin:'http://127.0.0.1:5174'}});
  const token=vi.fn(()=> 'old-browser-token');
  const fetcher=vi.fn(async()=>new Response(JSON.stringify({schema_version:'saferoute-m3-http-response/1',request_id:'test',status:'OK',diagnostics:[],data:{scenarios:[]}}),{status:200}));
  const client=new Member3Client({baseUrl:'http://127.0.0.1:5174',localProxy:true,token,fetch:fetcher});
  await client.scenarios();
  expect(token).not.toHaveBeenCalled();
  expect(fetcher.mock.calls[0]).toEqual(['http://127.0.0.1:5174/api/scenarios',expect.objectContaining({headers:expect.objectContaining({'X-SafeRoute-Local':'1'})})]);
  const headers=(fetcher.mock.calls[0] as unknown as [string,RequestInit])[1].headers as Record<string,string>;
  expect(headers.Authorization).toBeUndefined();
});

it('rejects launcher mode for another origin',()=>{
  vi.stubGlobal('window',{location:{origin:'http://127.0.0.1:5174'}});
  expect(()=>new Member3Client({baseUrl:'http://127.0.0.1:8000',localProxy:true})).toThrow(/page origin/);
});
