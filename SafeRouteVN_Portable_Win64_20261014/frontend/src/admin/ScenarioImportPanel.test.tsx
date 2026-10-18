import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {expect,it} from 'vitest';
import {DispatchProvider,useDispatch} from '../app/DispatchContext';
import {MockDispatchApi} from '../services/api/MockDispatchApi';
import type {M3Run} from '../integrations/member3/types';
import ScenarioImportPanel from './ScenarioImportPanel';

class ImportApi extends MockDispatchApi {
  ids:string[]=[];
  fail=false;
  failAfterCommit=false;
  async importedScenarios():Promise<{imports:M3Run[]}> {return {imports:[{
    run_id:'run-'+'a'.repeat(32),name:'Test import',scenario_id:'S0',status:'READY',message:'',
    epoch:'2026-10-17T00:00:00Z',manifest_sha256:'a'.repeat(64),fixture_sha256:'b'.repeat(64),online_weather_enabled:false,
  }]};}
  async loadWeatherRun(_run:string,id:string){
    this.ids.push(id);
    if(this.fail){this.fail=false;throw new Error('lost response');}
    const result=await this.loadScenario('S0');
    if(this.failAfterCommit){this.failAfterCommit=false;throw new Error('world read failed after commit');}
    return result;
  }
}
function Builtin(){const {api,invoke}=useDispatch();return <button onClick={()=>void invoke(()=>api.loadScenario('S1'))}>Open S1</button>;}

it('creates a new import intent after returning from a built-in scenario but reuses failed intents',async()=>{
  const api=new ImportApi();
  render(<DispatchProvider api={api}><Builtin/><ScenarioImportPanel/></DispatchProvider>);
  await screen.findByRole('option',{name:/Test import/});
  fireEvent.change(screen.getByRole('combobox'),{target:{value:'run-'+'a'.repeat(32)}});
  const open=screen.getByRole('button',{name:'Mở scenario đã import'});
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(open);await waitFor(()=>expect(api.ids).toHaveLength(1));
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(screen.getByRole('button',{name:'Open S1'}));
  await waitFor(()=>expect(open).toBeEnabled());
  api.fail=true;fireEvent.click(open);await waitFor(()=>expect(api.ids).toHaveLength(2));
  expect(api.ids[1]).not.toBe(api.ids[0]);
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(open);await waitFor(()=>expect(api.ids).toHaveLength(3));
  expect(api.ids[2]).toBe(api.ids[1]);
});

it('retires an import receipt after switching scenario even when its world read failed',async()=>{
  const api=new ImportApi();api.failAfterCommit=true;
  render(<DispatchProvider api={api}><Builtin/><ScenarioImportPanel/></DispatchProvider>);
  await screen.findByRole('option',{name:/Test import/});
  fireEvent.change(screen.getByRole('combobox'),{target:{value:'run-'+'a'.repeat(32)}});
  const open=screen.getByRole('button',{name:'Mở scenario đã import'});
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(open);await waitFor(()=>expect(api.ids).toHaveLength(1));
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(screen.getByRole('button',{name:'Open S1'}));
  await waitFor(()=>expect(open).toBeEnabled());
  fireEvent.click(open);await waitFor(()=>expect(api.ids).toHaveLength(2));
  expect(api.ids[1]).not.toBe(api.ids[0]);
});
