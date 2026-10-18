// Standalone M4 reference, not the frontend ownership area. Prototype views
// must show simulation and supported scope; no claim of full Step7 handoff.
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
export function strictJSON(raw) {
  let i=0;
  const ws=()=>{while (/\s/.test(raw[i]??'') && i<raw.length)i++;};
  const fail=()=>{throw new Error(`INVALID_JSON at ${i}`);};
  function string(){
    const start=i++;while(i<raw.length){if(raw[i]==='\\'){i+=2;continue;}if(raw[i++]==='"')return JSON.parse(raw.slice(start,i));}fail();
  }
  function value(depth=0){
    if(depth>96)fail();ws();let c=raw[i];
    if(c==='"')return string();
    if(c==='{'){
      i++;ws();const out=Object.create(null),seen=new Set();if(raw[i]==='}'){i++;return out;}
      while(i<raw.length){ws();if(raw[i]!=='"')fail();const key=string();if(seen.has(key))throw new Error('DUPLICATE_JSON_KEY '+key);seen.add(key);ws();if(raw[i++]!==':')fail();out[key]=value(depth+1);ws();c=raw[i++];if(c==='}')return out;if(c!==',')fail();}fail();
    }
    if(c==='['){i++;ws();const out=[];if(raw[i]===']'){i++;return out;}while(i<raw.length){out.push(value(depth+1));ws();c=raw[i++];if(c===']')return out;if(c!==',')fail();}fail();}
    for(const [text,v] of [['true',true],['false',false],['null',null]])if(raw.slice(i,i+text.length)===text){i+=text.length;return v;}
    const match=raw.slice(i).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);if(!match)fail();
    i+=match[0].length;const n=Number(match[0]);if(!Number.isFinite(n)||(!/[.eE]/.test(match[0])&&!Number.isSafeInteger(n)))throw new Error('NUMERIC_INTEROP');return n;
  }
  const out=value();ws();if(i!==raw.length)fail();return out;
}
function require(ok,path){if(!ok)throw Object.assign(new Error('CONTRACT_INVALID '+path),{code:'INVALID_DATA',path});}
export function exactInt(text,p='int64'){require(typeof text==='string'&&/^(?:0|-?[1-9]\d*)$/.test(text)&&text.length<=20,p);const n=BigInt(text);require(n>=-(1n<<63n)&&n<(1n<<63n),p);return n;}
function id(v,p){require(typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/.test(v),p);}
function hash(v,p){require(typeof v==='string'&&/^[a-f0-9]{64}$/.test(v),p);}
function finite(v,p){require(typeof v==='number'&&Number.isFinite(v)&&v>=0,p);}
function object(v,p){require(v!==null&&typeof v==='object'&&!Array.isArray(v),p);}
function fields(v,needed,optional,p){object(v,p);require(needed.every(k=>Object.hasOwn(v,k))&&Object.keys(v).every(k=>[...needed,...optional].includes(k)),p);}
function time(v,p){if(typeof v==='string')return exactInt(v,p);require(Number.isSafeInteger(v),p);return BigInt(v);}
function node(v,p){require(Number.isSafeInteger(v)&&v>0,p);}
function xy(v,p){require(Array.isArray(v)&&v.length===2,p);v.forEach((n,i)=>require(typeof n==='number'&&Number.isFinite(n)&&Math.abs(n)<=(i===0?180:90),p+`[${i}]`));}
// Portable shape/time/geometry validation, not raw-road or VRP certification.
function trajectory(t,p='accepted_trajectory'){
 if(t===null)return;
 fields(t,['job_id','profile','forecast','domain_sha256','vehicle_routes'],[],p);id(t.job_id,p+'.job_id');hash(t.domain_sha256,p+'.domain_sha256');
 require(['FASTEST','BALANCED','SAFER'].includes(t.profile)&&t.forecast===true,p+'.profile');require(Array.isArray(t.vehicle_routes),p+'.vehicle_routes');
 const vehicles=new Set(),served=new Set();
 const kinds={EDGE:['edge_id','from_node','to_node','incoming_edge','fraction_start','fraction_start_exact','fraction_end','geometry','feature_payload','distance_m','exposure','overlay_sha256','temporal_policy','temporal_segments'],PICKUP:['order_id','node_id','load_after_kg'],SERVICE:['order_id','node_id','load_after_kg','continuation'],WAIT:['node_id','order_id','reason']};
 t.vehicle_routes.forEach((r,i)=>{
  const q=p+`.vehicle_routes[${i}]`,needed=['vehicle_id','order_sequence','actions','start_us','return_us','start_node','end_node'];
  const optional=['route_column_id','return_load_kg','total_distance_m','total_travel_time_s','total_exposure','total_cost_vnd','total_soft_lateness_s'];fields(r,needed,optional,q);
  id(r.vehicle_id,q+'.vehicle_id');require(!vehicles.has(r.vehicle_id),q+'.vehicle_id');vehicles.add(r.vehicle_id);require(Array.isArray(r.order_sequence),q+'.order_sequence');
  for(const o of r.order_sequence){id(o,q+'.order_sequence');require(!served.has(o),q+'.order_sequence');served.add(o);}
  const start=time(r.start_us,q+'.start_us'),end=time(r.return_us,q+'.return_us');require(end>=start,q+'.return_us');node(r.start_node,q+'.start_node');node(r.end_node,q+'.end_node');
  for(const k of optional)if(Object.hasOwn(r,k)){if(k==='route_column_id')id(r[k],q+'.'+k);else if(k==='return_load_kg')require(typeof r[k]==='number'&&Number.isFinite(r[k])&&r[k]>=-1e-9,q+'.'+k);else finite(r[k],q+'.'+k);}
  require(Array.isArray(r.actions),q+'.actions');let previous=start;
  r.actions.forEach((a,j)=>{
   const z=q+`.actions[${j}]`;object(a,z);require(typeof a.kind==='string'&&Object.hasOwn(kinds,a.kind),z+'.kind');fields(a,['kind','start_us','end_us'],kinds[a.kind],z);
   const b=time(a.start_us,z+'.start_us'),e=time(a.end_us,z+'.end_us');require(b>=previous&&e>=b&&e<=end,z+'.end_us');previous=e;
   if(a.kind==='EDGE'){
    for(const k of ['edge_id','from_node','to_node','incoming_edge','fraction_start','fraction_end','geometry','feature_payload','distance_m','exposure'])require(Object.hasOwn(a,k),z+'.'+k);
    id(a.edge_id,z+'.edge_id');node(a.from_node,z+'.from_node');node(a.to_node,z+'.to_node');if(a.incoming_edge!==null)id(a.incoming_edge,z+'.incoming_edge');
    for(const k of ['distance_m','exposure','fraction_start','fraction_end'])finite(a[k],z+'.'+k);require(a.fraction_start<=a.fraction_end&&a.fraction_end<=1,z+'.fraction_end');
    if(a.fraction_start_exact!==undefined){require(typeof a.fraction_start_exact==='string'&&/^(?:0|[1-9][0-9]{0,18})(?:\/[1-9][0-9]{0,18})?$/.test(a.fraction_start_exact),z+'.fraction_start_exact');const parts=a.fraction_start_exact.split('/');require(BigInt(parts[0])<=BigInt(parts[1]??'1')&&BigInt(parts[1]??'1')<(1n<<63n),z+'.fraction_start_exact');}
    require(Array.isArray(a.geometry)&&a.geometry.length>=2,z+'.geometry');a.geometry.forEach((v,k)=>xy(v,z+`.geometry[${k}]`));object(a.feature_payload,z+'.feature_payload');
    // Optional metadata has the same shape contract as Python/schema; this
    // consumer does not authenticate an overlay or require a policy version.
    if(Object.hasOwn(a,'overlay_sha256'))hash(a.overlay_sha256,z+'.overlay_sha256');
    if(Object.hasOwn(a,'temporal_policy'))require(typeof a.temporal_policy==='string'&&a.temporal_policy.length>0,z+'.temporal_policy');
    if(a.temporal_segments!==undefined){require(Array.isArray(a.temporal_segments),z+'.temporal_segments');let prior=b;a.temporal_segments.forEach((s,k)=>{const f=z+`.temporal_segments[${k}]`;object(s,f);const sb=time(s.start_us,f+'.start_us'),se=time(s.end_us,f+'.end_us');require(sb>=prior&&se>=sb&&se<=e,f+'.end_us');prior=se;require(s.edge_id===a.edge_id&&['BASELINE','WET','BASELINE_AFTER_EXPIRY'].includes(s.layer),f+'.edge_id');});}
   }else if(['PICKUP','SERVICE'].includes(a.kind)){
    for(const k of ['order_id','node_id','load_after_kg'])require(Object.hasOwn(a,k),z+'.'+k);id(a.order_id,z+'.order_id');node(a.node_id,z+'.node_id');require(typeof a.load_after_kg==='number'&&Number.isFinite(a.load_after_kg)&&a.load_after_kg>=-1e-9,z+'.load_after_kg');
    if(a.kind==='PICKUP')require(b===e,z+'.end_us');if(a.continuation!==undefined)require(typeof a.continuation==='boolean',z+'.continuation');
   }else{if(a.node_id!==undefined)node(a.node_id,z+'.node_id');if(a.order_id!==undefined)id(a.order_id,z+'.order_id');if(a.reason!==undefined)require(typeof a.reason==='string'&&a.reason.length>0,z+'.reason');}
  });require(r.actions.length===0||previous===end,q+'.return_us');
 });
}
function timestamp(v,p){
 require(typeof v==='string',p);
 const m=v.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\+07:00$/);require(m!==null,p);
 const [y,mo,d,h,mi,s]=m.slice(1,7).map(Number),leap=y%4===0&&(y%100!==0||y%400===0);
 require(y>=1&&mo>=1&&mo<=12&&d>=1&&d<=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31][mo-1]&&h<24&&mi<60&&s<60,p);
}
export function view(payload){
  require(['task02-m2-execution-view/1','task02-m2-execution-view/2'].includes(payload?.schema_version),'schema_version');
  if(payload.schema_version.endsWith('/2'))fields(payload,['schema_version','basis','execution_mode','real_world_observation','current_time','order_ids','delivered_prefix','planned_served_suffix','unserved','metric_scope','observed_metrics','vehicles','active_job_id','pending_event_ids','planned_suffix_metrics','projected_whole_metrics','accepted_trajectory'],[],'$');
  require(payload.execution_mode==='SIMULATED_REPLAY'&&payload.real_world_observation===false,'simulation');
  require(payload.metric_scope==='OBSERVED_PREFIX_ONLY','metric_scope');
  timestamp(payload.current_time,'current_time');
  const b=payload.basis;fields(b,['session_id','root_sha256','head_sha256','head_version','generation','source_sha256','context_version','overlay_sha256','build_sha256'],[],'basis');id(b.session_id,'basis.session_id');
  for(const k of ['head_version','generation'])require(exactInt(b[k])>=0,'basis.'+k);
  id(b.context_version,'basis.context_version');require(b.overlay_sha256===null||typeof b.overlay_sha256==='string','basis.overlay_sha256');if(b.overlay_sha256!==null)hash(b.overlay_sha256,'basis.overlay_sha256');
  for(const k of ['root_sha256','head_sha256','source_sha256','build_sha256'])hash(b[k],'basis.'+k);
  for(const k of ['order_ids','delivered_prefix','planned_served_suffix','unserved','vehicles','pending_event_ids'])require(Array.isArray(payload[k]),k);
  payload.unserved.forEach((u,i)=>{object(u,`unserved[${i}]`);id(u.order_id,`unserved[${i}].order_id`);require(typeof u.reason==='string'&&u.reason.length>0,`unserved[${i}].reason`);});
  const ids=[...payload.delivered_prefix,...payload.planned_served_suffix,...payload.unserved.map(x=>x.order_id)];
  require(ids.every(x=>typeof x==='string')&&new Set(ids).size===ids.length&&ids.length===payload.order_ids.length&&ids.every(x=>payload.order_ids.includes(x)),'coverage');
  for(const k of ['order_ids','delivered_prefix','planned_served_suffix','pending_event_ids']){require(new Set(payload[k]).size===payload[k].length,k);for(const v of payload[k])id(v,k);}
  for(const u of payload.unserved)require(typeof u.reason==='string'&&u.reason.length>0,'unserved.reason');
  require(payload.active_job_id===null||typeof payload.active_job_id==='string','active_job_id');if(payload.active_job_id!==null)id(payload.active_job_id,'active_job_id');
  const vehicleIds=new Set();
  for(const v of payload.vehicles){
   object(v,'vehicles');
   id(v.vehicle_id,'vehicles.vehicle_id');require(!vehicleIds.has(v.vehicle_id),'vehicles.vehicle_id');vehicleIds.add(v.vehicle_id);
   require(['AVAILABLE','UNAVAILABLE'].includes(v.availability),'vehicles.availability');finite(v.current_load_kg,'vehicles.current_load_kg');
   if(v.capacity_kg!==undefined){finite(v.capacity_kg,'vehicles.capacity_kg');require(v.current_load_kg<=v.capacity_kg+1e-9,'vehicles.current_load_kg');}
   if(v.remaining_range_m!==undefined)finite(v.remaining_range_m,'vehicles.remaining_range_m');
   require(Array.isArray(v.onboard_order_ids)&&new Set(v.onboard_order_ids).size===v.onboard_order_ids.length,'vehicles.onboard_order_ids');for(const o of v.onboard_order_ids)id(o,'vehicles.onboard_order_ids');
   const p=v.position;require(p&&['AT_NODE','ON_EDGE'].includes(p.kind),'position.kind');require(Array.isArray(p.coordinates)&&p.coordinates.length===2&&p.coordinates.every(x=>typeof x==='number'&&Number.isFinite(x))&&Math.abs(p.coordinates[0])<=180&&Math.abs(p.coordinates[1])<=90,'coordinates');
   if(p.kind==='ON_EDGE'){id(p.edge_id,'position.edge_id');finite(p.progress,'position.progress');require(p.progress<=1,'position.progress');}
   if(p.progress_exact!==undefined){fields(p.progress_exact,['numerator','denominator'],[],'position.progress_exact');const n=exactInt(p.progress_exact.numerator),d=exactInt(p.progress_exact.denominator);require(d>0&&n>=0&&n<=d,'position.progress_exact');}
   if(v.position_timestamp!==undefined)timestamp(v.position_timestamp,'vehicles.position_timestamp');
  }
  require(payload.observed_metrics===null||typeof payload.observed_metrics==='object'&&!Array.isArray(payload.observed_metrics),'observed_metrics');
  if(payload.observed_metrics!==null)for(const [k,v] of Object.entries(payload.observed_metrics)){
   if(k.endsWith('_us')&&typeof v==='string')require(exactInt(v)>=0,'observed_metrics.'+k);else finite(v,'observed_metrics.'+k);
  }
  if(payload.schema_version.endsWith('/2')){
    for(const k of ['planned_suffix_metrics','projected_whole_metrics']){require(payload[k]===null||typeof payload[k]==='object'&&!Array.isArray(payload[k]),k);if(payload[k]!==null)for(const n of Object.values(payload[k]))finite(n,k);}
    const t=payload.accepted_trajectory;require(t===null||typeof t==='object'&&!Array.isArray(t),'accepted_trajectory');
    trajectory(t);
  }
  return {label:'SIMULATED REPLAY — not GPS',delivered:payload.delivered_prefix.length,planned:payload.planned_served_suffix.length,unserved:payload.unserved.length};
}
export function jobView(v){
 const keys=['schema_version','job_id','job_status','input_basis','business_status','internal_status','diagnostics','validation','coverage_evaluated','served_orders','unserved_orders','plan_available','execution_view_required','public_api_v1_dynamic_plan_available'];fields(v,keys,[],'$');
 require(v.schema_version==='task02-m2-runtime-job-view/1','schema_version');id(v.job_id,'job_id');require(['QUEUED','RUNNING','COMPLETED','FAILED'].includes(v.job_status),'job_status');
 const b=v.input_basis;fields(b,['session_id','root_sha256','head_sha256','head_version','generation','source_sha256','context_version','overlay_sha256','build_sha256'],[],'input_basis');id(b.session_id,'input_basis.session_id');id(b.context_version,'input_basis.context_version');for(const k of ['root_sha256','head_sha256','source_sha256','build_sha256'])hash(b[k],'input_basis.'+k);for(const k of ['head_version','generation'])require(exactInt(b[k],'input_basis.'+k)>=0n,'input_basis.'+k);if(b.overlay_sha256!==null)hash(b.overlay_sha256,'input_basis.overlay_sha256');
 require(v.execution_view_required===true&&v.public_api_v1_dynamic_plan_available===false,'execution_view_required');
 for(const k of ['diagnostics','served_orders','unserved_orders'])require(Array.isArray(v[k]),k);for(const k of ['coverage_evaluated','plan_available'])require(typeof v[k]==='boolean',k);object(v.validation,'validation');
 const ids=[...v.served_orders];ids.forEach((o,i)=>id(o,`served_orders[${i}]`));v.unserved_orders.forEach((u,i)=>{object(u,`unserved_orders[${i}]`);id(u.order_id,`unserved_orders[${i}].order_id`);require(typeof u.reason==='string'&&u.reason.length>0,`unserved_orders[${i}].reason`);ids.push(u.order_id);});require(ids.length===new Set(ids).size,'served_orders');
 v.diagnostics.forEach((d,i)=>{object(d,`diagnostics[${i}]`);for(const k of ['code','path','message'])require(typeof d[k]==='string'&&d[k].length>0,`diagnostics[${i}].${k}`);});
 if(v.plan_available){require(v.job_status==='COMPLETED'&&['FEASIBLE','PARTIAL','RETURN_ONLY'].includes(v.internal_status)&&v.coverage_evaluated===true,'plan_available');require(v.validation.status==='VALIDATED'&&v.validation.valid===true&&typeof v.validation.validator_version==='string','validation');require(v.business_status===(v.internal_status==='RETURN_ONLY'?'UNSUPPORTED':v.internal_status),'business_status');}
 else{require(v.coverage_evaluated===false&&v.served_orders.length===0&&v.unserved_orders.length===0,'coverage_evaluated');fields(v.validation,['status','valid'],[],'validation');require(v.validation.status==='NOT_RUN'&&v.validation.valid===null,'validation');if(['QUEUED','RUNNING','FAILED'].includes(v.job_status))require(v.business_status===null&&v.internal_status===null,'business_status');if(['COMPLETED','FAILED'].includes(v.job_status))require(v.diagnostics.length>0,'diagnostics');}
 if(v.internal_status==='FEASIBLE')require(v.served_orders.length>0&&v.unserved_orders.length===0,'served_orders');if(v.internal_status==='PARTIAL')require(v.served_orders.length>0&&v.unserved_orders.length>0,'unserved_orders');
 if(!v.plan_available&&v.job_status==='COMPLETED'){require(['NO_SERVICE','SEARCH_LIMIT','TIME_LIMIT','UNSUPPORTED','INVALID_DATA'].includes(v.internal_status),'internal_status');require(v.business_status===(v.internal_status==='NO_SERVICE'?'UNSUPPORTED':v.internal_status),'business_status');}
 return {label:'SIMULATED JOB — not execution',status:v.job_status};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  try{const result=view(strictJSON(fs.readFileSync(process.argv[2],'utf8')));console.log(JSON.stringify({status:'REFERENCE_CONSUMER_PASS',...result}));}
  catch(e){console.log(JSON.stringify({status:'FAIL',diagnostic:String(e)}));process.exitCode=2;}
}
