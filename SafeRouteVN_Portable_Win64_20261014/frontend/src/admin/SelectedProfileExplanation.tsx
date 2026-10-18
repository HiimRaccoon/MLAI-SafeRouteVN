import {useEffect, useRef, useState} from 'react';
import type {DispatchApi} from '../services/api/DispatchApi';
import type {DispatchSnapshot} from '../shared/types/dispatch';
import type {M3Explanations} from '../integrations/member3/types';
import {DecisionAnalysis} from './DecisionAnalysisPanel';
import {sameBasis} from '../integrations/member3/revision';

export default function ProfileExplanation({snapshot,api}:{snapshot:DispatchSnapshot;api:DispatchApi}) {
  const group=snapshot.backend?.comparison;
  const sid=group?.session_id, cid=group?.comparison_id, status=group?.status;
  const [result,setResult]=useState<M3Explanations>();
  const [failed,setFailed]=useState(false);
  const [receivedAt,setReceivedAt]=useState<string>();
  const ready=useRef(false);
  const [mode,setMode]=useState<'quick'|'decision'>(()=>{
    try{return localStorage.getItem('saferoute.explanationMode')==='decision'?'decision':'quick';}catch{return 'quick';}
  });
  const changeMode=(next:'quick'|'decision')=>{
    setMode(next);try{localStorage.setItem('saferoute.explanationMode',next);}catch{/* Display still works without storage. */}
  };
  useEffect(()=>{
    ready.current=false;
    setResult(undefined);setFailed(false);setReceivedAt(undefined);
  },[api,sid,cid,status]);
  useEffect(()=>{
    if(mode!=='quick' || ready.current || !sid || !cid || status!=='COMPLETED' || !api.profileExplanations) return;
    setFailed(false);
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try {
        const value=await api.profileExplanations!(sid,cid,controller.signal);
        if(controller.signal.aborted)return;
        setResult(value);
        if(value.status==='READY'){ready.current=true;setReceivedAt(new Date().toISOString());}
        if(value.status==='PENDING')timer=setTimeout(poll,1500);
      } catch {if(!controller.signal.aborted)setFailed(true);}
    };
    void poll();
    return()=>{controller.abort();clearTimeout(timer);};
  },[api,sid,cid,status,mode]);
  const selected=snapshot.planState.proposedAlternatives.find(p=>p.id===snapshot.planState.selectedAlternativeId);
  if(!selected || selected.origin?.kind!=='MEMBER3')return null;
  const profile=selected.origin.profile;
  const matching=result && result.session_id===sid && result.comparison_id===cid && group && sameBasis(result.basis,group.input_basis) ? result : undefined;
  const lines=matching?.status==='READY' ? matching.profiles[profile]?.sentences ?? [] : [];
  const stale=Boolean(group && snapshot.backend && (!sameBasis(group.input_basis,snapshot.backend.basis) || snapshot.backend.stale));
  const aiContent=<>
    <p className="di-hint-text">{matching?.status==='READY' ? `${matching.model} · Đánh giá do AI tạo` : 'Đánh giá Qwen'}
      {stale && ' · Kết quả của lần tính trước; cần tối ưu lại để đánh giá trạng thái hiện tại.'}</p>
    <ul>{lines.map((line,i)=><li key={`${profile}-${i}`}>{line}</li>)}</ul>
    {!matching && status==='COMPLETED' && !failed && <p role="status">Đang chuẩn bị giải thích bằng Qwen3…</p>}
    {matching?.status==='PENDING' && <p role="status">Đang chuẩn bị giải thích bằng Qwen3…</p>}
    {matching?.status==='WAITING' && <p role="status">{matching.message}</p>}
    {(failed || matching?.status==='FALLBACK') && <p className="di-hint-text">{matching?.message ?? 'AI local chưa sẵn sàng; bạn vẫn có thể xem và chọn phương án.'}</p>}
  </>;
  return <section className="selected-explanation" aria-label={`Giải thích ${profile}`} aria-live="polite">
    <div className="explanation-mode" role="group" aria-label="Chế độ giải thích">
      <button type="button" aria-pressed={mode==='quick'} onClick={()=>changeMode('quick')}>Giải thích nhanh</button>
      <button type="button" aria-pressed={mode==='decision'} onClick={()=>changeMode('decision')}>Phân tích quyết định</button>
    </div>
    <h3>{mode==='quick'?`Vì sao chọn ${profile}?`:`Phân tích quyết định · ${profile}`}</h3>
    {stale && mode==='decision' && <p role="status">Dữ liệu thuộc lần tính trước. Các đánh đổi dưới đây không phải đánh giá trạng thái hiện tại.</p>}
    {mode==='quick'?aiContent:<DecisionAnalysis snapshot={snapshot} selected={selected}/>}
    <details className="explanation-transparency"><summary>Thông tin minh bạch</summary>
      {mode==='quick' && matching?.limitations && <p>{matching.limitations}</p>}
      <p>Số liệu do bộ tối ưu tính và kiểm tra tính khả thi. {mode==='quick'?'AI hỗ trợ diễn giải; người điều phối chọn và xác nhận áp dụng kế hoạch.':'Người điều phối chọn và xác nhận áp dụng kế hoạch.'}</p>
      <p>Nguồn số liệu: dự báo từ bộ tối ưu, không phải kết quả giao hàng thực tế. Bảng và các chênh lệch được ứng dụng tính trực tiếp từ số liệu; không do AI tự tạo.</p>
      {mode==='quick' && <p>{matching?.status==='READY'?`Model diễn giải: ${matching.model}.`:'Chưa có diễn giải AI sẵn sàng. Phần phân tích bằng số vẫn dùng dữ liệu đã có, không giả làm nội dung AI.'}</p>}
      {mode==='quick' && matching?.status==='READY' && receivedAt && <p>Nhận phản hồi AI lúc: {new Intl.DateTimeFormat('vi-VN',{dateStyle:'short',timeStyle:'medium',timeZone:'Asia/Ho_Chi_Minh'}).format(new Date(receivedAt))} (UTC+7). Backend chưa cung cấp thời điểm tạo nội dung.</p>}
      <p>Chi phí tuyến không phải riêng chi phí nhiên liệu; exposure proxy không phải xác suất tai nạn. Chỉ số thiếu được khai báo thay vì điền số 0.</p>
    </details>
  </section>;
}
