import {describe,it,expect} from 'vitest';
import {observedRoadPath,pointAlong,ConfirmedVehicleMotion} from './vehicleMotion';
describe('confirmed road animation',()=>{
  it('follows a bend instead of cutting across the map',()=>{
    const path=observedRoadPath([0.5,0],[1,0.5],[[[0,0],[1,0]],[[1,0],[1,1]]]);
    expect(path).toEqual([[0.5,0],[1,0],[1,0.5]]);
    expect(pointAlong(path!,0.5)).toEqual([1,0]);
  });
  it('does not invent a bridge over disconnected road sections',()=>{
    expect(observedRoadPath([0,0],[2,2],[[[0,0],[1,0]],[[2,1],[2,2]]])).toBeNull();
  });
  it('does not animate a rewind or unconfirmed off-road position',()=>{
    expect(observedRoadPath([1,0],[0,0],[[[0,0],[1,0]]])).toBeNull();
    expect(observedRoadPath([9,9],[1,0],[[[0,0],[1,0]]])).toBeNull();
  });
});


describe('queued confirmed playback clock', () => {
  const roads: [number,number][][] = [[[0,0],[1,0],[1,1],[2,1],[3,1],[4,1]]];
  it('queues new observations without replacing the road interval already playing', () => {
    const motion = new ConfirmedVehicleMotion();
    motion.observe([0,0], roads, 0, true, 0);
    motion.observe([1,1], roads, 2500, true, 60000);
    const before = motion.sample(3500)!;
    expect(before[0]).toBeGreaterThan(0);
    expect(before[1]).toBe(0);
    expect(motion.observe([2,1], roads, 3500, true, 120000)).toEqual(before);
    expect(motion.moving).toBe(true);
    const samples = Array.from({length:160}, (_,i) => motion.sample(3500 + (i+1)*50)!);
    expect(samples.some(p => p[0] === 1 && p[1] > 0 && p[1] < 1)).toBe(true);
    expect(samples.every(p => p[0] <= 2 && p[1] <= 1)).toBe(true);
    expect(motion.sample(60000)).toEqual([2,1]);
    expect(motion.moving).toBe(false);
  });
  it('absorbs irregular arrivals while remaining monotonic and behind the server', () => {
    const motion = new ConfirmedVehicleMotion();
    const straight: [number,number][][] = [[[0,0],[20,0]]];
    motion.observe([0,0], straight, 0, true, 0);
    let previous = 0;
    for (const [arrival, x] of [[2500,2],[5200,4],[7450,6],[10400,8],[13000,10]]) {
      const before = motion.sample(arrival)!;
      const after = motion.observe([x,0], straight, arrival, true, x * 60000);
      expect(after).toEqual(before);
      expect(after[0]).toBeGreaterThanOrEqual(previous);
      expect(after[0]).toBeLessThan(x);
      expect(motion.moving).toBe(true);
      previous = after[0];
    }
  });
  it('does not restart on duplicate snapshots and flushes pause, off-road, and clock rewinds', () => {
    const motion = new ConfirmedVehicleMotion();
    motion.observe([0,0], roads, 0, true, 0);
    motion.observe([1,1], roads, 2500, true, 60000);
    const before = motion.sample(3000);
    expect(motion.observe([1,1], roads, 3000, true, 60000)).toEqual(before);
    expect(motion.observe([1,1], roads, 3100, false, 60000)).toEqual([1,1]);
    expect(motion.moving).toBe(false);
    expect(motion.observe([9,9], roads, 4000, true, 120000)).toEqual([9,9]);
    expect(motion.moving).toBe(false);
    expect(motion.observe([0,0], roads, 5000, true, 0)).toEqual([0,0]);
  });
  it('honors a confirmed dwell without inventing movement', () => {
    const motion = new ConfirmedVehicleMotion();
    motion.observe([0,0], roads, 0, true, 0);
    motion.observe([0,0], roads, 2500, true, 60000);
    expect(motion.sample(3500)).toEqual([0,0]);
    motion.observe([1,0], roads, 5000, true, 120000);
    expect(motion.sample(5050)).toEqual([0,0]);
    expect(motion.sample(60000)).toEqual([1,0]);
  });
});
