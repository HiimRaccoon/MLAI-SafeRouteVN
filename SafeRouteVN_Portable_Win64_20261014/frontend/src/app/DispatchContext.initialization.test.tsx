import { StrictMode } from 'react';
import { act, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { DispatchProvider, useDispatch } from './DispatchContext';
import { createDispatchApi } from '../services/api/createDispatchApi';
import { MockDispatchApi } from '../services/api/MockDispatchApi';
import type { DispatchApi } from '../services/api/DispatchApi';
vi.mock('../services/api/createDispatchApi',()=>({createDispatchApi:vi.fn()}));
function Probe(){const {snapshot}=useDispatch();return <div>{snapshot?'initialized world':'loading world'}</div>}
it('shares async initialization through StrictMode and never subscribes after unmount',async()=>{
 let resolve!:(api:DispatchApi)=>void;const pending=new Promise<DispatchApi>(done=>{resolve=done});
 vi.mocked(createDispatchApi).mockReturnValue(pending as unknown as ReturnType<typeof createDispatchApi>);
 const api=new MockDispatchApi({storage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}});const subscribe=vi.spyOn(api,'subscribe'),read=vi.spyOn(api,'getSnapshot');
 const first=render(<StrictMode><DispatchProvider><Probe/></DispatchProvider></StrictMode>);
 expect(createDispatchApi).toHaveBeenCalledTimes(1);expect(subscribe).not.toHaveBeenCalled();
 first.unmount();await act(async()=>{resolve(api);await pending});expect(subscribe).not.toHaveBeenCalled();expect(read).not.toHaveBeenCalled();
 const second=render(<StrictMode><DispatchProvider><Probe/></DispatchProvider></StrictMode>);
 expect(await screen.findByText('initialized world')).toBeInTheDocument();expect(createDispatchApi).toHaveBeenCalledTimes(1);expect(subscribe).toHaveBeenCalledTimes(1);expect(read).toHaveBeenCalledTimes(1);
 second.unmount();api.dispose();
});
