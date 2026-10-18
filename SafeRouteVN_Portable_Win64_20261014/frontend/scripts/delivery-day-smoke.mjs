import puppeteer from 'puppeteer-core';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const origin=process.argv[2]||'http://127.0.0.1:5185';
const out=resolve(process.argv[3]||'../outputs/delivery-day-ui');await mkdir(out,{recursive:true});
const browser=await puppeteer.launch({headless:true,pipe:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
const report={status:'RUNNING',steps:[],errors:[]};
try{
  const page=await browser.newPage();page.setDefaultTimeout(300000);await page.setViewport({width:1600,height:1200});
  page.on('pageerror',e=>report.errors.push(e.message));
  await page.setRequestInterception(true);
  page.on('request',r=>new URL(r.url()).hostname==='127.0.0.1'||r.url().startsWith('data:')?r.continue():r.abort());
  const click=async text=>{
    await page.waitForFunction(t=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()===t&&!b.disabled),{},text);
    await page.evaluate(t=>[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===t&&!b.disabled).click(),text);
  };
  const api=async(path,body)=>page.evaluate(async({path,body})=>{
    const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-SafeRoute-Local':'1',...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
    const v=await r.json();if(!r.ok)throw new Error(JSON.stringify(v));return v.data;
  },{path,body});
  await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});
  await click('Bắt đầu ngày giao hàng');
  await page.waitForSelector('[aria-label="Delivery day"]');
  const sid=await page.$eval('.admin-page',e=>e.dataset.sessionId);report.session_id=sid;
  if(await page.$$eval('input[type="range"]',e=>e.length))throw new Error('Preference sliders remain');
  let view=await api(`/api/sessions/${sid}/state`);if(!view.current_time.includes('T08:00:00'))throw new Error('Day did not start at 08:00');
  report.steps.push('start_0800','no_preference_sliders');
  console.log('DAY_UI_STARTED');
  await page.click('.cta-optimize');
  await page.waitForFunction(()=>[...document.querySelectorAll('.alt-view-btn')].filter(b=>!b.disabled).length===3);
  await page.click('[aria-label="Select FASTEST"]');
  await page.waitForFunction(()=>document.querySelector('.accept-fullwidth')&&!document.querySelector('.accept-fullwidth').disabled);
  await page.click('.accept-fullwidth');
  await page.waitForFunction(()=>document.querySelector('.admin-page')?.dataset.activeJobId);
  report.steps.push('three_profiles','accept');
  console.log('DAY_UI_ACCEPTED');
  for(let i=0;i<6;i++){
    view=await api(`/api/sessions/${sid}/state`);if(view.delivery_day.complete)break;
    await api(`/api/sessions/${sid}/replay/step`,{request_id:crypto.randomUUID(),expected_revision:{head_version:view.basis.head_version,generation:view.basis.generation},target_time:view.delivery_barrier});
    view=await api(`/api/sessions/${sid}/state`);const ready=view.vehicles.find(v=>v.awaiting_delivery);if(!ready)throw new Error('No delivery confirmation barrier');
    await page.goto(origin+'/driver',{waitUntil:'domcontentloaded'});
    await click('Deliver · '+ready.awaiting_delivery);
    await page.waitForFunction(id=>!document.body.textContent.includes('Deliver · '+id),{},ready.awaiting_delivery);
    const delivered=await api(`/api/sessions/${sid}/state`);if(!delivered.delivered_prefix.includes(ready.awaiting_delivery))throw new Error('Deliver did not commit');
    report.steps.push('manual_deliver_'+ready.awaiting_delivery);
    console.log('DAY_UI_DELIVERED',ready.awaiting_delivery);
  }
  await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('[aria-label="Delivery day"]');
  await page.evaluate(()=>document.querySelectorAll('[aria-label="Delivery day"] details').forEach(e=>e.open=true));
  await page.type('[aria-label="Delivery day"] input[type="tel"]','0900000001');
  await page.type('[aria-label="Delivery day"] input[maxlength="120"]','Demo customer');
  await page.click('[aria-label="Delivery day"] input[type="checkbox"]');
  await click('Thêm đơn thường');
  await page.waitForFunction(()=>document.querySelector('[aria-label="Delivery day"]')?.textContent.includes('Queue ngày sau (1)'));
  await page.screenshot({path:resolve(out,'queue.png'),fullPage:true});report.steps.push('late_regular_queued');
  await click('Bắt đầu ngày mới');
  await page.waitForFunction(()=>document.querySelector('[aria-label="Delivery day"]')?.textContent.includes('Queue ngày sau (0)'));
  view=await api(`/api/sessions/${sid}/state`);report.next_day=view.current_time;
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('[aria-label="Delivery day"]');
  report.steps.push('next_day','reload_persists');
  await page.screenshot({path:resolve(out,'next-day.png'),fullPage:true});
  if(report.errors.length)throw new Error(report.errors.join(';'));
  report.status='PASS';console.log('DELIVERY_DAY_UI_PASS');
}catch(e){report.status='FAIL';report.error=String(e);throw e;}
finally{await writeFile(resolve(out,'result.json'),JSON.stringify(report,null,2));await browser.close();}
