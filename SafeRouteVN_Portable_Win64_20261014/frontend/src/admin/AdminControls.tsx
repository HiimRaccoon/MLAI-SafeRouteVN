import {ChevronDown} from 'lucide-react';
import {useDispatch} from '../app/DispatchContext';
import './deliveryDay.css';

export function AdminControls(){
  const {api,snapshot,pending,invoke}=useDispatch();
  const backend=snapshot?.backend;
  const locked=pending||backend?.mutationPending;
  return <details className="admin-controls">
    <summary className="admin-user-pill" aria-label="Admin controls">
      <span className="avatar">AD</span><span className="admin-name"><b>Admin</b><small>Dispatcher</small></span><ChevronDown size={12}/>
    </summary>
    <div className="admin-controls-menu">
      <button disabled={locked} onClick={()=>void invoke(()=>api.getSnapshot())}>Refresh server</button>
      <button disabled={locked||!api.resetSession||!backend||backend.stale||!backend.playback?.fully_paused} onClick={()=>void invoke(()=>api.resetSession!())}>Reset session</button>
      {backend?.playback&&!backend.playback.fully_paused&&<><p>Pause replay trước khi reset session.</p><button disabled={locked||!api.replayPause} onClick={()=>void invoke(()=>api.replayPause!())}>Pause replay</button></>}
      <label><input type="checkbox" checked={backend?.playback?.auto_deliver??false} disabled={locked||backend?.stale||!backend?.executionView.delivery_day||!api.setAutoDeliver} onChange={e=>void invoke(()=>api.setAutoDeliver!(e.target.checked))}/> Auto Deliver</label>
      <p>{backend?.executionView.delivery_day?'Khi Play, tự xác nhận giao hàng lúc xe đến điểm giao. Tắt để tài xế nhấn Deliver.':'Bắt đầu ngày giao hàng để dùng Auto Deliver.'}</p>
    </div>
  </details>;
}
