import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import {it,expect,vi} from 'vitest';
import {AdminControls} from './AdminControls';
const ctx=vi.hoisted(()=>({value:{} as any}));
vi.mock('../app/DispatchContext',()=>({useDispatch:()=>ctx.value}));
it('expands admin controls and sends auto-deliver configuration to the server',async()=>{
  const api={getSnapshot:vi.fn(),resetSession:vi.fn(),setAutoDeliver:vi.fn().mockResolvedValue({})};
  ctx.value={api,pending:false,invoke:(fn:any)=>fn(),snapshot:{backend:{executionView:{delivery_day:{}},playback:{fully_paused:true,auto_deliver:false}}}};
  render(<AdminControls/>);
  expect(screen.getByText('Admin').closest('details')).not.toHaveAttribute('open');
  fireEvent.click(screen.getByText('Admin'));
  expect(screen.getByRole('button',{name:'Reset session'})).toBeEnabled();
  fireEvent.click(screen.getByRole('checkbox',{name:'Auto Deliver'}));
  await waitFor(()=>expect(api.setAutoDeliver).toHaveBeenCalledWith(true));
  fireEvent.click(screen.getByRole('button',{name:'Refresh server'}));
  expect(api.getSnapshot).toHaveBeenCalledOnce();
});
