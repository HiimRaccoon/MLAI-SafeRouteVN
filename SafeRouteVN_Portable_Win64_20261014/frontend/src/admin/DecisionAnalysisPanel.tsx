import {useEffect,useRef,useState,type ReactNode} from 'react';
import type {DispatchSnapshot,PlanProfile,ProposedAlternative} from '../shared/types/dispatch';
import {decisionCandidates,decisionMetrics,decisionNumber,decisionProfiles,decisionSummary,numericTradeoffs,sameDecisionRoute} from './decisionTradeoffs';
const intent: Record<PlanProfile,string>={
 FASTEST:'Phù hợp khi ưu tiên thời gian di chuyển; đối chiếu phần chi phí và phơi nhiễm phải đánh đổi.',
 BALANCED:'Phù hợp khi muốn cân nhắc đồng thời thời gian và phơi nhiễm theo mục tiêu mô hình; không mặc định đây là phương án tốt nhất.',
 SAFER:'Phù hợp khi ưu tiên giảm exposure proxy và có thể chấp nhận thời gian hoặc chi phí tăng nếu số liệu thể hiện điều đó.'
};
export function DecisionAnalysis({snapshot,selected}:{snapshot:DispatchSnapshot;selected:ProposedAlternative}){
 const [expanded,setExpanded]=useState(false);
 const candidates=decisionCandidates(snapshot);
 const comparable=candidates.every(Boolean) && snapshot.backend?.comparison?.outcome?.comparison?.status==='COMPARABLE';
 const selectedIndex=candidates.findIndex(p=>p?.id===selected.id);
 const table=<div className="decision-table-scroll" tabIndex={0} role="region" aria-label="Bảng đánh đổi">
   <table className="decision-table"><caption>Đơn vị ghi ở từng chỉ tiêu; các chỉ tiêu thời gian, chi phí, phơi nhiễm, quãng đường và trễ càng thấp càng tốt.</caption>
    <thead><tr><th scope="col">Chỉ tiêu dự kiến</th>{decisionProfiles.map((profile,i)=><th scope="col" key={profile} className={selectedIndex===i?'decision-selected':''}>{profile}{selectedIndex===i?' · đang chọn':''}</th>)}</tr></thead>
    <tbody><tr><th scope="row">Đơn phục vụ / chưa phục vụ</th>{candidates.map((p,i)=><td key={i}>{p?`${p.nativeForecast!.jobView.served_orders.length} / ${p.nativeForecast!.jobView.unserved_orders.length}`:'Chưa có dữ liệu'}</td>)}</tr>
     {decisionMetrics.map(metric=><tr key={metric.key}><th scope="row">{metric.label} ({metric.unit})</th>{candidates.map((p,i)=>{const n=p?.nativeForecast?.metrics.values[metric.key];return <td key={i}>{n==null?'Chưa có dữ liệu':decisionNumber(n*metric.factor)}</td>;})}</tr>)}
    </tbody></table>
  </div>;
 return <div className="decision-analysis">
  <h4>Bảng đánh đổi giữa các phương án</h4>
  <p className="di-hint-text">Số liệu dự kiến; tổng thời gian đội xe là tổng thời gian di chuyển của các xe, không phải thời gian hoàn thành toàn bộ chuyến giao.</p>
  {!comparable && <p role="status">Chưa đủ ba phương án cùng dữ liệu để so sánh đầy đủ. Chỉ hiển thị số liệu đã có; chưa kết luận phương án nào tốt hơn.</p>}
  <button type="button" className="decision-expand" onClick={()=>setExpanded(true)}>Mở rộng bảng</button>
  {!expanded && table}
  {expanded && <DecisionTableDialog onClose={()=>setExpanded(false)}>{table}</DecisionTableDialog>}
  <h4>Đánh đổi của phương án đang chọn</h4>
  {comparable && selectedIndex>=0 ? candidates.filter(p=>p && p.id!==selected.id).map(peer=><div key={peer!.id} className="decision-choice">
   <strong>So với {peer!.origin?.kind==='MEMBER3'?peer!.origin.profile:''}</strong>
   <ul>{numericTradeoffs(selected,peer!).map(text=><li key={text}>{text}</li>)}</ul>
   {sameDecisionRoute(selected,peer!) && <p>Cùng tuyến đường; khác tên mục tiêu không có nghĩa là một phương án đường đi khác.</p>}
  </div>):<p>Chưa đủ dữ liệu so sánh cho lựa chọn này.</p>}
  <h4>Khi nào cân nhắc từng lựa chọn?</h4>
  {decisionProfiles.map((profile,i)=>{
   const p=candidates[i], baseline=candidates[i===0?1:0];
   return <div key={profile} className="decision-choice"><strong>{profile}</strong><p>{intent[profile]}</p>
    {comparable && p && baseline && <><p>Mốc so sánh: {i===0?'BALANCED':'FASTEST'}.</p><ul>{numericTradeoffs(p,baseline).map(text=><li key={text}>{text}</li>)}</ul></>}
   </div>;
  })}
  <details className="decision-assumptions" open><summary>Giả định và giới hạn</summary><ul>
   <li>Chỉ so sánh các phương án thuộc cùng lượt tối ưu và có cùng dữ liệu đầu vào được hệ thống xác nhận.</li>
   <li>Dự kiến phụ thuộc đơn hàng, tải trọng, cửa sổ giao hàng và bối cảnh đưa vào mô hình; điều kiện thực tế thay đổi có thể làm kết quả khác đi.</li>
   <li>{snapshot.backend?.run?.online_weather_enabled?'Phiên bật lấy thời tiết trực tuyến. Xem nguồn và thời điểm cập nhật ở phần weather; không mặc định giao thông là dữ liệu trực tiếp.':'Chưa có xác nhận về nguồn thời tiết hoặc giao thông trực tiếp cho lượt tính này; không coi kết quả là tình trạng đường ngoài thực tế.'}</li>
   <li>Exposure là chỉ số proxy của mô hình, không phải xác suất tai nạn. Chi phí tuyến không đồng nghĩa với riêng chi phí nhiên liệu.</li>
   <li>Trễ 0 giây không tự chứng minh 100% đơn được giao đúng giờ; cần đối chiếu cả đơn chưa phục vụ và thời điểm hoàn thành.</li>
   <li>Đây là nghiệm khả thi trong tập ứng viên đã tìm được; chưa chứng minh tối ưu toàn cục. Dữ liệu thiếu không được thay bằng 0.</li>
  </ul></details>
  <h4>Diễn giải từ số liệu</h4>
  {selectedIndex>=0?<ul>{decisionSummary(selected,comparable?candidates.filter((p):p is ProposedAlternative=>Boolean(p)):[]).map(text=><li key={text}>{text}</li>)}</ul>:<p>Chưa có dữ liệu dự kiến hợp lệ cho lựa chọn này.</p>}
 </div>;
}


function DecisionTableDialog({children,onClose}:{children:ReactNode;onClose:()=>void}){
 const dialog=useRef<HTMLDialogElement>(null);
 useEffect(()=>{dialog.current?.showModal();},[]);
 return <dialog className="decision-table-dialog" ref={dialog} onClose={onClose} aria-label="Bảng đánh đổi mở rộng">
  <div className="decision-dialog-header"><h3>Bảng đánh đổi giữa các phương án</h3><button autoFocus type="button" onClick={()=>dialog.current?.close()}>Đóng bảng</button></div>
  {children}
 </dialog>;
}
