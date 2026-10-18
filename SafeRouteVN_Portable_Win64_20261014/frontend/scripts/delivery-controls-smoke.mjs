import puppeteer from 'puppeteer-core';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const origin=process.argv[2]||'http://127.0.0.1:5185';
const out=resolve(process.argv[3]||'../outputs/delivery-controls-ui');await mkdir(out,{recursive:true});
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
 const waitView=async(sid,predicate,timeout=300000)=>{const started=Date.now();while(Date.now()-started<timeout){const v=await api(`/api/sessions/${sid}/state`);if(predicate(v))return v;await new Promise(r=>setTimeout(r,1500));}throw new Error('Timed out waiting for observed state');};
 let sid,view,ready;
 if(!process.argv[4]){
 await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});
 await click('Bắt đầu ngày giao hàng');await page.waitForSelector('[aria-label="Delivery day"]');
 sid=await page.$eval('.admin-page',e=>e.dataset.sessionId);report.session_id=sid;
 const headings=await page.$$eval('.comparison-table th',els=>els.map(e=>e.textContent));
 if(headings.join('|')!=='Chỉ số|Before|After|Chênh lệch')throw new Error('Comparison headers missing');
 if(await page.$$eval('.comparison-table td',els=>els.filter(e=>e.textContent==='—').length)!==12)throw new Error('Missing metrics must be dashes');
 await page.click('.admin-controls summary');
 await page.waitForSelector('.admin-controls[open]');
 await page.screenshot({path:resolve(out,'admin-menu.png'),fullPage:true});
 report.steps.push('empty_comparison_table','admin_menu');
 await page.click('.admin-controls summary');
 await page.click('.cta-optimize');
 console.log('CONTROLS_OPTIMIZING');
 await page.waitForFunction(()=>[...document.querySelectorAll('.alt-view-btn')].filter(b=>!b.disabled).length===3);
 await page.click('[aria-label="Select FASTEST"]');
 await page.waitForFunction(()=>document.querySelector('.accept-fullwidth')&&!document.querySelector('.accept-fullwidth').disabled);
 await page.click('.accept-fullwidth');await page.waitForFunction(()=>document.querySelector('.admin-page')?.dataset.activeJobId);
 console.log('CONTROLS_ACCEPTED');
 view=await api(`/api/sessions/${sid}/state`);
 await api(`/api/sessions/${sid}/replay/step`,{request_id:crypto.randomUUID(),expected_revision:{head_version:view.basis.head_version,generation:view.basis.generation},target_time:view.delivery_barrier});
 view=await api(`/api/sessions/${sid}/state`);ready=view.vehicles.find(v=>v.awaiting_delivery);
 if(!ready)throw new Error('Manual delivery barrier missing');
 await page.goto(origin+'/driver',{waitUntil:'domcontentloaded'});await page.waitForSelector('#driver-selector');
 await page.waitForFunction(()=>!document.querySelector('[aria-label="Replay speed"]').disabled);
 await page.select('[aria-label="Replay speed"]','8');
 await page.waitForFunction(()=>document.querySelector('[aria-label="Replay speed"]').value==='8');
 if(await page.$$eval('button',els=>els.some(e=>['Reset session','Refresh server'].includes(e.textContent.trim()))))throw new Error('Admin controls exposed in Driver');
 await page.select('#driver-selector','V2');await page.waitForFunction(()=>document.querySelector('[aria-label="Center on V2"]'));
 await page.select('#driver-selector',ready.vehicle_id);
 await click('Deliver · '+ready.awaiting_delivery);
 await waitView(sid,v=>v.delivered_prefix.length>=1);
 report.steps.push('driver_switch_v2','manual_deliver_first');console.log('CONTROLS_FIRST_DELIVERED');
 }else{
 sid=process.argv[4];report.session_id=sid;report.steps.push('resume_after_verified_first_delivery');
 await page.goto(origin+'/ready',{waitUntil:'domcontentloaded'});
 const session=await api(`/api/sessions/${sid}`);
 await page.evaluate(({origin,session})=>localStorage.setItem('saferoute.member3.session.v1:'+origin,JSON.stringify({schemaVersion:1,session})),{origin,session});
 await page.goto(origin+'/driver',{waitUntil:'domcontentloaded'});await page.waitForSelector('#driver-selector');
 }
 await waitView(sid,v=>v.vehicles.some(x=>x.awaiting_delivery));
 view=await api(`/api/sessions/${sid}/state`);ready=view.vehicles.find(v=>v.awaiting_delivery);
 await page.select('#driver-selector',ready.vehicle_id);await click('Deliver · '+ready.awaiting_delivery);
 await waitView(sid,v=>v.delivered_prefix.length>=2);
 await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='Pause'&&!b.disabled));
 report.steps.push('replay_resumes_after_deliver','manual_deliver_second');console.log('CONTROLS_SECOND_DELIVERED');
 // Pause before changing demo settings. Auto Deliver never starts replay itself.
 await api(`/api/sessions/${sid}/replay/playback/pause`,{request_id:crypto.randomUUID()});
 await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});await page.waitForSelector('[aria-label="Delivery day"]');
 await page.click('.admin-controls summary');
 await page.waitForFunction(()=>!document.querySelector('.admin-controls input[type=checkbox]').disabled);
 await page.click('.admin-controls input[type=checkbox]');
 await page.waitForFunction(()=>document.querySelector('.admin-controls input[type=checkbox]').checked);
 await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('.admin-controls summary');await page.click('.admin-controls summary');
 await page.waitForFunction(()=>document.querySelector('.admin-controls input[type=checkbox]').checked);
 report.steps.push('auto_deliver_saved_on_reload');
 await page.goto(origin+'/driver',{waitUntil:'domcontentloaded'});
 const controller=await api(`/api/sessions/${sid}/replay/playback`);
 if(controller.paused)await click('Play');
 await waitView(sid,v=>v.delivery_day.complete);
 view=await api(`/api/sessions/${sid}/state`);
 if(view.delivered_prefix.length!==3)throw new Error('Auto Deliver did not complete remaining order');
 await page.waitForFunction(()=>document.querySelector('.drv-page')?.dataset.stale==='false'&&document.body.textContent.includes('Delivered prefix: O001, O002, O003'));
 await page.screenshot({path:resolve(out,'driver-complete.png'),fullPage:true});
 report.steps.push('auto_deliver_remaining_order');
 const history=await api(`/api/sessions/${sid}/replay/history`);
 if(history.history.filter(r=>r.operation==='deliver').length!==1||!history.history.some(r=>r.operation==='deliver'&&r.order_id==='O003'))throw new Error('Expected exactly one automatic confirmation for the final order');
 if(report.errors.length)throw new Error(report.errors.join(';'));
 report.status='PASS';console.log('DELIVERY_CONTROLS_UI_PASS');
}catch(e){report.status='FAIL';report.error=String(e);throw e;}
finally{
 if(report.status==='FAIL'){try{const p=(await browser.pages()).at(-1);report.ui=await p.evaluate(()=>({text:document.body.innerText,basis:document.querySelector('.drv-page')?.dataset}));await p.screenshot({path:resolve(out,'failure.png'),fullPage:true});}catch{}}
 await writeFile(resolve(out,'result.json'),JSON.stringify(report,null,2));await browser.close();
}
