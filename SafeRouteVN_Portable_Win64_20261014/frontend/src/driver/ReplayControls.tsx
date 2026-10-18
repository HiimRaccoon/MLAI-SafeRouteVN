import type { DispatchSnapshot } from "../shared/types/dispatch";
import type { DispatchApi } from "../services/api/DispatchApi";
import { canControlPlayback } from "../integrations/member3/capabilities";
import { playbackComplete } from "../integrations/member3/playback";
import { REPLAY_SPEEDS, type ReplaySpeed } from '../shared/replaySpeeds';
export interface ReplayControlsProps { api: DispatchApi; snapshot: DispatchSnapshot; pending: boolean; invoke(operation: () => Promise<DispatchSnapshot>): Promise<void> }
export function ReplayControls({ api, snapshot, pending, invoke }: ReplayControlsProps) {
  const backend = snapshot.backend;
  if (!backend) return <section className="drv-stop-section" aria-label="Demo motion controls"><div className="drv-empty-card">
    <h2>Vehicle simulation</h2><p>1× = 1 simulated minute per second. Stops wait for Pickup / Delivered.</p>
    <div style={{display:'flex',gap:8,flexWrap:'wrap'}}>
      <button disabled={pending||!snapshot.planState.activeAcceptedPlanId||snapshot.demo.playback?.playing||!api.replayStart} onClick={()=>void invoke(()=>api.replayStart!(snapshot.demo.playback?.speed??1))}>Play</button>
      <button disabled={pending||!snapshot.demo.playback?.playing||!api.replayPause} onClick={()=>void invoke(()=>api.replayPause!())}>Pause</button>
      <button disabled={pending||!snapshot.planState.activeAcceptedPlanId||snapshot.demo.playback?.playing||!api.replayStep} onClick={()=>void invoke(()=>api.replayStep!())}>Step 1 minute</button>
      <label>Speed <select value={snapshot.demo.playback?.speed??1} disabled={pending||!api.replaySpeed} onChange={e=>void invoke(()=>api.replaySpeed!(Number(e.target.value) as ReplaySpeed))}>{REPLAY_SPEEDS.map(s=><option key={s} value={s}>{s}×</option>)}</select></label>
    </div></div></section>;
  const controller = backend.playback;
  const blocked = pending || backend.stale || backend.mutationPending || !backend.capabilities?.replay || !controller;
  const barrier = backend.pendingEvents?.events.some(e => e.apply_allowed);
  const awaiting=backend.executionView.vehicles.some(v=>Boolean(v.awaiting_delivery));
  const canAdvance = !blocked && controller?.fully_paused && !playbackComplete(controller) && Boolean(backend.executionView.active_job_id) && !barrier && (!awaiting || controller.auto_deliver);
  const canControl = !pending && canControlPlayback(snapshot);
  return <section className="drv-stop-section" aria-label="Simulated replay controls">
    <div className="drv-empty-card">
      <h2 className="drv-empty-title">Simulated replay</h2>
      <p>1× = {controller?.step_seconds ?? 60} simulation seconds per tick; processing may delay updates.</p>
      <p role="status">{controller?.in_flight ? "Settling reserved tick — server progress may still change." : controller?.paused ? `Paused · ${controller.reason}` : controller ? "Playing on server" : "Controller unavailable"}</p>
      <p>Bản đồ phát lại có đệm để chuyển động mượt; vị trí hiển thị có thể chậm hơn giờ server.</p>
      {backend.playbackConverging && <p>Updating server execution. Last confirmed state is shown.</p>}
      {!backend.executionView.active_job_id && <p>Chờ admin Accept tuyến để bật Play. Optimize tạo phương án; Accept mới giao tuyến cho tài xế.</p>}
      {backend.stale && <p>Dữ liệu tuyến chưa cập nhật. Play tạm khóa cho đến khi nhận lại trạng thái server.</p>}
      {barrier && <p>Event barrier: Apply in Admin before continuing.</p>}
      {awaiting && <p>Chờ Deliver: {backend.executionView.vehicles.filter(v=>v.awaiting_delivery).map(v=>`${v.vehicle_id} · ${v.awaiting_delivery}`).join(', ')}. Chọn tài xế tương ứng để xác nhận.</p>}
      {controller?.auto_deliver && <p>Auto Deliver đang bật: server xác nhận khi xe đến điểm giao trong lúc Play.</p>}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 12 }}>
        <button className="drv-btn drv-btn--outline" type="button" disabled={!canAdvance || !api.replayStep} onClick={() => void invoke(() => api.replayStep!())}>Step</button>
        <button className="drv-btn drv-btn--primary" type="button" disabled={!canAdvance || !api.replayStart} onClick={() => void invoke(() => api.replayStart!(controller!.speed))}>Play</button>
        <button className="drv-btn drv-btn--outline" type="button" disabled={!canControl || !api.replayPause} onClick={() => void invoke(() => api.replayPause!())}>Pause</button>
        <label>Replay speed <select aria-label="Replay speed" value={controller?.speed ?? 1} disabled={!canControl || !api.replaySpeed} onChange={event => {
          const speed = Number(event.target.value) as ReplaySpeed;
          if (REPLAY_SPEEDS.includes(speed)) void invoke(() => api.replaySpeed!(speed));
        }}>{REPLAY_SPEEDS.map(speed => <option key={speed} value={speed}>{speed}×</option>)}</select></label>
      </div>
    </div>
  </section>;
}
