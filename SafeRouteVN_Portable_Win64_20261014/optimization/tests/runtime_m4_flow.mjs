// Concrete reference M4 map/KPI/driver projection, not frontend implementation.
// Inputs come only from the locked public execution-view, never M2 Store.
import fs from 'node:fs';
import {view,strictJSON} from '../runtime/reference_consumer.mjs';
const filename=process.argv[2],payload=strictJSON(fs.readFileSync(filename,'utf8'));
const checked=view(payload),features=[];
for(const route of payload.accepted_trajectory?.vehicle_routes??[]){
  for(const action of route.actions){
    if(action.kind!=='EDGE')continue;
    const coordinates=action.geometry;
    if(!Array.isArray(coordinates)||coordinates.length<2||!coordinates.every(p=>Array.isArray(p)&&p.length===2&&p.every(n=>typeof n==='number'&&Number.isFinite(n))&&Math.abs(p[0])<=180&&Math.abs(p[1])<=90))throw Error('map.geometry');
    features.push({type:'Feature',geometry:{type:'LineString',coordinates},properties:{vehicle_id:route.vehicle_id,edge_id:action.edge_id,scope:'FORECAST_SOURCE_EDGE_GEOMETRY',fraction_start:action.fraction_start,simulation:true}});
  }
}
const drivers=payload.vehicles.map(v=>({vehicle_id:v.vehicle_id,availability:v.availability,coordinates:v.position.coordinates,position_kind:v.position.kind,current_load_kg:v.current_load_kg,onboard_order_ids:v.onboard_order_ids,remaining_range_m:v.remaining_range_m}));
const result={status:'M3_RUNTIME_M4_REFERENCE_FLOW_PASS',source:filename,consumer_contract:payload.schema_version,label:checked.label,real_world_observation:false,
  kpi:{delivered_actual:checked.delivered,planned_not_delivered:checked.planned,unserved:checked.unserved,observed_prefix:payload.observed_metrics,remaining_forecast:payload.planned_suffix_metrics,projected_whole:payload.projected_whole_metrics},
  drivers,map:{type:'FeatureCollection',features},source_or_vrp_authentication_performed:false};
console.log(JSON.stringify(result));
