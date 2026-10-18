import puppeteer from 'puppeteer-core';
import {mkdir,writeFile} from 'node:fs/promises';
const origin=process.argv[2]??'http://127.0.0.1:5178';
const runId=process.argv[3];
if(!runId)throw new Error('Provide an unexpired weather run ID');
const output='../outputs/weather-session-controls';await mkdir(output,{recursive:true});
const browser=await puppeteer.launch({headless:true,pipe:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const report={status:'RUNNING',steps:[],errors:[]};
try{
  const page=await browser.newPage();page.setDefaultTimeout(60000);await page.setViewport({width:1600,height:1100});
  page.on('pageerror',e=>report.errors.push(e.message));
  await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.weather-run-panel');
  const click=async text=>{
    await page.waitForFunction(t=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()===t&&!b.disabled),{},text);
    await page.evaluate(t=>[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===t&&!b.disabled).click(),text);
  };
  const open=async()=>{
    await page.evaluate(id=>sessionStorage.setItem('saferoute.weather-run.intent.v1',JSON.stringify({requestId:crypto.randomUUID(),loadRequestId:crypto.randomUUID(),scenarioId:'S2',eventAfterMinutes:15,runId:id})),runId);
    await page.reload({waitUntil:'domcontentloaded'});await click('Open new session');
    await page.waitForFunction(()=>document.querySelector('.weather-run-panel')?.textContent.includes('Đang chạy S2'));
    if(await page.evaluate(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Open new session')))throw new Error('Open button still visible for active session');
  };
  await open();report.steps.push('active_context_visible');
  const panel=await page.$('.weather-run-panel');await panel.screenshot({path:output+'/active.png'});
  report.buttonFont=await page.$eval('.weather-run-actions button',b=>getComputedStyle(b).fontSize);
  if(report.buttonFont!=='12px')throw new Error('Oversized weather buttons');
  await click('Hủy phiên');
  await page.waitForFunction(()=>!document.querySelector('.weather-run-panel')?.textContent.includes('Context:')&&!sessionStorage.getItem('saferoute.weather-run.intent.v1'));
  report.steps.push('explicit_cancel_clears_context');
  await open();
  await page.click('.scenario-summary');
  await page.waitForFunction(()=>!document.querySelector('.weather-run-panel')?.textContent.includes('Context:')&&!sessionStorage.getItem('saferoute.weather-run.intent.v1'));
  report.steps.push('demo_tools_cancel_before_optimize');
  await page.select('[aria-label="Demo scenario"]','S1');
  await page.waitForFunction(()=>document.querySelector('[aria-label="Demo scenario"]')?.value==='S1'&&!document.querySelector('[aria-label="Demo scenario"]')?.disabled);
  await click('Reset demo to S0');
  await page.waitForFunction(()=>document.querySelector('[aria-label="Demo scenario"]')?.value==='S0'&&!document.querySelector('[aria-label="Demo scenario"]')?.disabled);
  report.steps.push('normal_scenarios_and_reset');
  const di=await page.$eval('.panel-di',e=>e.textContent);
  for(const removed of ['Optimize runs three forecasts through Member 3.','Select a certified current forecast','Refresh backend','OBSERVED_PREFIX_ONLY','PLANNED_SUFFIX','PROJECTED_WHOLE','MEMBER3_HTTP · SIMULATED_REPLAY'])if(di.includes(removed))throw new Error('Technical message remains: '+removed);
  await (await page.$('.panel-di')).screenshot({path:output+'/decision-intelligence.png'});
  await (await page.$('.weather-run-panel')).screenshot({path:output+'/cancelled.png'});
  if(report.errors.length)throw new Error(report.errors.join(';'));
  report.status='PASS';console.log('WEATHER_SESSION_CONTROLS_PASS');
}catch(e){report.status='FAIL';report.error=String(e);throw e;}
finally{await writeFile(output+'/result.json',JSON.stringify(report,null,2));await browser.close();}
