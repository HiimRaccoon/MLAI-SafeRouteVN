import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { LiveEta } from './LiveEta';

const originalLocation=Object.getOwnPropertyDescriptor(navigator,'geolocation');
afterEach(()=>{vi.useRealTimers();vi.restoreAllMocks();if(originalLocation)Object.defineProperty(navigator,'geolocation',originalLocation);else Reflect.deleteProperty(navigator,'geolocation');});

it('only watches after opt-in, discards stale ETA, and releases the watch',()=>{
  vi.useFakeTimers();
  let success:PositionCallback=()=>{};
  const clearWatch=vi.fn();
  const watchPosition=vi.fn((callback:PositionCallback)=>{success=callback;return 17;});
  Object.defineProperty(navigator,'geolocation',{configurable:true,value:{watchPosition,clearWatch,getCurrentPosition:vi.fn()}});
  const {unmount}=render(<LiveEta target={{latitude:10.81,longitude:106.7}} simulatedMinutes={3.2}/>);
  expect(watchPosition).not.toHaveBeenCalled();
  expect(screen.getByText('Simulation to next stop: 4 min')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'Use device location'}));
  act(()=>success({timestamp:Date.now(),coords:{latitude:10.8,longitude:106.7,accuracy:10,speed:10,altitude:null,altitudeAccuracy:null,heading:null}} as GeolocationPosition));
  expect(screen.getByText(/GPS straight-line estimate/)).toBeInTheDocument();
  act(()=>vi.advanceTimersByTime(31000));
  expect(screen.queryByText(/GPS straight-line estimate/)).not.toBeInTheDocument();
  expect(screen.getByText(/Waiting for a fresh/)).toBeInTheDocument();
  unmount();expect(clearWatch).toHaveBeenCalledWith(17);
});
