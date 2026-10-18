// Native local UI smoke: real scenario loads, profile solves and route previews.
// Uses a fresh headless browser; does not read the user's browser profile/tokens.
import puppeteer from 'puppeteer-core';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const origin=process.argv[2] || 'http://127.0.0.1:5175';
const output=resolve(process.argv[3] || '../../.local/ui-profile-smoke');
const cases=(process.argv[4] || 'S0,S1,S2,S3').split(',');
await mkdir(output,{recursive:true});
const browser=await puppeteer.launch({headless:true,pipe:true,executablePath:process.env.SAFEROUTE_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const report={status:'RUNNING',origin,cases:[],errors:[]};
try {
  const page=await browser.newPage();await page.setViewport({width:1920,height:1200});
  page.setDefaultTimeout(180000);await page.setRequestInterception(true);
  page.on('request',request=>new URL(request.url()).hostname==='127.0.0.1' || request.url().startsWith('data:') ? request.continue() : request.abort());
  page.on('pageerror',error=>report.errors.push(error.message));
  const forecasts=new Map();
  page.on('response',async response=>{
    if(response.url().endsWith('/forecast') && response.ok()) {
      try {const {data}=await response.json();forecasts.set(data.job_id,{profile:data.profile,metrics:data.metrics,valid:data.job_view.validation.valid,
        routes:data.trajectory?.vehicle_routes.map(r=>({vehicle:r.vehicle_id,column:r.route_column_id,orders:r.order_sequence}))});} catch {}
    }
  });
  const initialStarted=performance.now();
  await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.admin-page');
  for(const scenario of cases) {
    const before=await page.$eval('.admin-page',e=>e.dataset.sessionId);
    const current=await page.$eval('.scenario-collapsible select',e=>e.value);
    const started=current===scenario ? initialStarted : performance.now();
    if(current!==scenario) {
      await page.select('.scenario-collapsible select',scenario);
      await page.waitForFunction(sid=>document.querySelector('.admin-page')?.dataset.sessionId!==sid,{},before);
    }
    await page.waitForFunction(()=>!document.querySelector('.cta-optimize')?.disabled);
    const loadSeconds=(performance.now()-started)/1000;
    const optimized=performance.now();await page.click('.cta-optimize');
    await page.waitForFunction(()=>[...document.querySelectorAll('.alt-view-btn')].some(b=>!b.disabled));
    const firstPreviewSeconds=(performance.now()-optimized)/1000;
    await page.waitForFunction(()=>[...document.querySelectorAll('.alt-view-btn')].filter(b=>!b.disabled).length===3);
    const allPreviewSeconds=(performance.now()-optimized)/1000;
    const ids=await page.$$eval('[data-job-id]',els=>els.map(e=>e.dataset.jobId));
    await page.click('[aria-label="Select BALANCED"]');
    const route=await page.waitForSelector('button[aria-label^="View "]:not([disabled])');await route.click();
    await page.waitForFunction(()=>document.querySelectorAll('.leaflet-overlay-pane path').length>0);
    await page.waitForSelector('[aria-label="Giải thích BALANCED"]');
    const screenshot=resolve(output,scenario+'.png');await page.screenshot({path:screenshot,fullPage:true});
    const row={scenario,loadSeconds,firstPreviewSeconds,allPreviewSeconds,forecasts:ids.map(id=>forecasts.get(id)),
      mapPaths:await page.$$eval('.leaflet-overlay-pane path',els=>els.length),screenshot};
    if(row.forecasts.length!==3 || row.forecasts.some(f=>!f?.valid))throw new Error('Missing certified forecast');
    report.cases.push(row);console.log(JSON.stringify(row));
    await writeFile(resolve(output,'result.json'),JSON.stringify(report,null,2));
  }
  if(report.errors.length)throw new Error('Browser runtime errors: '+report.errors.join('; '));
  report.status='PASS';
} catch(error) {report.status='FAIL';report.failure=String(error);throw error;}
finally {await writeFile(resolve(output,'result.json'),JSON.stringify(report,null,2));await browser.close();}
