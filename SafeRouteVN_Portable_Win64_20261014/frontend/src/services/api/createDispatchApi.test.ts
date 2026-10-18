import { describe, expect, it } from 'vitest';
import { createDispatchApi } from './createDispatchApi';
import { BackendDispatchApi } from './BackendDispatchApi';
import { MockDispatchApi } from './MockDispatchApi';
describe('production dispatch selection',()=>{
 it('defaults to async backend and keeps explicit mock/injection available',async()=>{
  const initialized=createDispatchApi({});expect(typeof (initialized as unknown as {then?:unknown}).then).toBe("function");
  expect(await initialized).toBeInstanceOf(BackendDispatchApi);
  expect(await createDispatchApi({VITE_DISPATCH_MODE:'backend',VITE_M3_BASE_URL:'http://127.0.0.1:8000'})).toBeInstanceOf(BackendDispatchApi);
  const mock=await createDispatchApi({VITE_DISPATCH_MODE:'mock'});expect(mock).toBeInstanceOf(MockDispatchApi);(mock as MockDispatchApi).dispose();
 });
 it('rejects invalid mode without a mock fallback',async()=>{await expect(Promise.resolve().then(()=>createDispatchApi({VITE_DISPATCH_MODE:'backedn'}))).rejects.toThrow(/VITE_DISPATCH_MODE/)});
});
