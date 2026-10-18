import { useEffect, useRef, useState } from 'react';
import { distanceKm, type Point } from '../shared/geo';

/** GPS is opt-in, tab-local and never changes the simulated server authority. */
export function LiveEta({target,simulatedMinutes}:{target?:Point;simulatedMinutes?:number}) {
  const [sample,setSample]=useState<GeolocationPosition>();
  const [error,setError]=useState('');
  const [tracking,setTracking]=useState(false);
  const [now,setNow]=useState(Date.now());
  const watch=useRef<number|undefined>(undefined);
  const stop=()=>{if(watch.current!==undefined)navigator.geolocation.clearWatch(watch.current);watch.current=undefined;setTracking(false);setSample(undefined);};
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>{clearInterval(timer);if(watch.current!==undefined)navigator.geolocation.clearWatch(watch.current);};},[]);
  const fresh=sample&&now-sample.timestamp<30000&&sample.coords.accuracy<=100;
  const speed=sample?.coords.speed;
  const eta=fresh&&target&&typeof speed==='number'&&speed>=0.5?distanceKm({latitude:sample.coords.latitude,longitude:sample.coords.longitude},target)*1000/speed/60:undefined;
  return <section className="drv-stop-section"><div className="drv-empty-card">
    <h3>Arrival estimate</h3>
    {simulatedMinutes!==undefined&&<p>Simulation to next stop: {Math.ceil(simulatedMinutes)} min</p>}
    <button type="button" onClick={()=>{
      if(tracking){stop();return;}setError('');
      if(!navigator.geolocation){setError('Device location is unavailable.');return;}
      setTracking(true);
      watch.current=navigator.geolocation.watchPosition(p=>{setSample(p);setNow(Date.now());setError('');},e=>{setError(e.message);if(e.code===1)stop();},{enableHighAccuracy:true,maximumAge:0,timeout:15000});
    }}>{tracking?'Stop device location':'Use device location'}</button>
    <p>GPS stays in this tab. It does not confirm deliveries or change replay.</p>
    {tracking&&<p role="status">{!fresh?'Waiting for a fresh, accurate GPS fix.':eta===undefined?'Speed or destination unavailable; ETA is not calculated.':`GPS straight-line estimate: ${Math.ceil(eta)} min (excludes roads and traffic).`}</p>}
    {sample&&<p>GPS accuracy: ±{Math.round(sample.coords.accuracy)} m · sample age {Math.max(0,Math.floor((now-sample.timestamp)/1000))} s</p>}
    {error&&<p role="alert">{error}</p>}
  </div></section>;
}
