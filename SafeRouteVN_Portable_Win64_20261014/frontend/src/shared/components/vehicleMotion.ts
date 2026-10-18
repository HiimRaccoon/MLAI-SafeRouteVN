type Point=[number,number];
const distance=(a:Point,b:Point)=>Math.hypot(a[0]-b[0],a[1]-b[1]);
/** Animate only a confirmed, contiguous road section between two observations. */
export function observedRoadPath(start:Point,end:Point,lines:Point[][]):Point[]|null {
  const chains:Point[][]=[];
  for(const line of lines){
    if(line.length<2)continue;
    const last=chains.at(-1);
    if(last&&distance(last.at(-1)!,line[0])<1e-7)last.push(...line.slice(1));
    else chains.push([...line]);
  }
  function closest(p:Point,line:Point[]){
    let best={d:Infinity,index:0,t:0,point:p};
    for(let i=0;i<line.length-1;i++){
      const a=line[i],b=line[i+1],dx=b[0]-a[0],dy=b[1]-a[1];
      const t=Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy||1)));
      const point:Point=[a[0]+t*dx,a[1]+t*dy],d=distance(p,point);
      if(d<best.d)best={d,index:i,t,point};
    }return best;
  }
  for(const line of chains){
    const a=closest(start,line),b=closest(end,line);
    if(a.d<1e-5&&b.d<1e-5&&a.index+a.t<=b.index+b.t){
      return [start,...line.slice(a.index+1,b.index+1),end];
    }
  }return null;
}
export function pointAlong(path:Point[],fraction:number):Point{
  const lengths=path.slice(1).map((p,i)=>distance(path[i],p));
  let remaining=lengths.reduce((a,b)=>a+b,0)*Math.max(0,Math.min(1,fraction));
  for(let i=0;i<lengths.length;i++){
    if(remaining<=lengths[i]&&lengths[i]>0){const t=remaining/lengths[i];return [path[i][0]+t*(path[i+1][0]-path[i][0]),path[i][1]+t*(path[i+1][1]-path[i][1])];}
    remaining-=lengths[i];
  }return path.at(-1)!;
}

interface ConfirmedInterval { start: number; end: number; point: (fraction: number) => Point }
/** A delayed simulation clock over queued confirmed observations. Never extrapolates. */
export class ConfirmedVehicleMotion {
  private position?: Point;
  private target?: Point;
  private intervals: ConfirmedInterval[] = [];
  private cursor = 0;
  private latest = 0;
  private frameAt?: number;
  private observedAt?: number;
  private nominalRate = 0;
  private displayRate = 0;
  private cadence = 2500;
  private jitter = 0;

  sample(now: number): Point | undefined {
    const elapsed = Math.max(0, now - (this.frameAt ?? now));
    this.frameAt = now;
    if (!this.intervals.length) return this.position;
    // Returning from a long-hidden tab settles to the newest confirmed point.
    if (elapsed > 30000) {
      this.position = this.target;
      this.cursor = this.latest;
      this.intervals = [];
      return this.position;
    }
    // Small integration steps make velocity changes independent of frame rate.
    for (let left = elapsed; left > 0; left -= 50) {
      const dt = Math.min(50, left);
      const buffered = this.latest - this.cursor;
      const delay = Math.max(500, Math.min(12000, this.cadence + 2 * this.jitter));
      const reserve = Math.max(1, this.nominalRate * delay);
      const desired = this.nominalRate * Math.max(.15, Math.min(1.6, buffered / reserve));
      this.displayRate += (desired - this.displayRate) * (1 - Math.exp(-dt / 500));
      this.cursor = Math.min(this.latest, this.cursor + this.displayRate * dt);
      if (this.latest - this.cursor < Math.max(.001, this.nominalRate * 10)) this.cursor = this.latest;
    }
    while (this.intervals.length && this.cursor >= this.intervals[0].end) {
      this.position = this.intervals.shift()!.point(1);
    }
    const interval = this.intervals[0];
    if (interval) this.position = interval.point(Math.max(0, (this.cursor - interval.start) / (interval.end - interval.start)));
    return this.position;
  }

  get moving(): boolean { return this.intervals.length > 0; }

  observe(target: Point, lines: Point[][], now: number, animate: boolean, simulationTime = now): Point {
    this.sample(now);
    if (!Number.isFinite(simulationTime)) simulationTime = now;
    if (!animate || !this.position || simulationTime < this.latest) {
      this.intervals = [];
      this.position = target;
      this.target = target;
      this.cursor = this.latest = simulationTime;
      this.observedAt = now;
      this.nominalRate = this.displayRate = 0;
      return target;
    }
    if (simulationTime <= this.latest) return this.position;
    const delta = simulationTime - this.latest;
    const arrival = Math.max(1, now - (this.observedAt ?? now));
    this.jitter = this.jitter * .8 + Math.abs(arrival - this.cadence) * .2;
    this.cadence = this.cadence * .8 + arrival * .2;
    const measured = delta / arrival;
    this.nominalRate = this.nominalRate ? this.nominalRate * .8 + measured * .2 : measured;
    if (!this.displayRate) this.displayRate = this.nominalRate * .8;
    const same = this.target && distance(this.target, target) < 1e-10;
    const path = same ? [target, target] : observedRoadPath(this.target!, target, lines);
    if (!path || this.intervals.length >= 512) {
      // A missing road connection or a discontinuity is a sync, never a shortcut.
      this.intervals = [];
      this.position = target;
      this.cursor = simulationTime;
    } else {
      this.intervals.push({ start: this.latest, end: simulationTime, point: preparePath(path) });
    }
    this.latest = simulationTime;
    this.target = target;
    this.observedAt = now;
    return this.position!;
  }
}

/** Cache distances once, then locate each animation frame with a binary search. */
function preparePath(path: Point[]): (fraction: number) => Point {
  const cumulative = [0];
  for (let i = 1; i < path.length; i++) cumulative.push(cumulative[i - 1] + distance(path[i - 1], path[i]));
  const total = cumulative.at(-1)!;
  return fraction => {
    if (fraction >= 1 || total === 0) return path.at(-1)!;
    const target = total * Math.max(0, fraction);
    let low = 1, high = path.length - 1;
    while (low < high) {
      const middle = (low + high) >>> 1;
      if (cumulative[middle] < target) low = middle + 1; else high = middle;
    }
    const length = cumulative[low] - cumulative[low - 1];
    const t = length ? (target - cumulative[low - 1]) / length : 0;
    return [path[low - 1][0] + t * (path[low][0] - path[low - 1][0]), path[low - 1][1] + t * (path[low][1] - path[low - 1][1])];
  };
}
