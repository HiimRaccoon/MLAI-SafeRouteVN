// Measure click -> three real forecasts AND generated Qwen assessment.
import puppeteer from 'puppeteer-core';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const origin=process.argv[2] || 'http://127.0.0.1:5185';
const output=resolve(process.argv[3] || '../outputs/latency-ui');
await mkdir(output,{recursive:true});
const report={status:'RUNNING',cases:[],errors:[]};
const browser=await puppeteer.launch({headless:true,pipe:true,timeout:60000,userDataDir:resolve(output,'browser-profile'),executablePath:process.env.SAFEROUTE_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
try {
  const page=await browser.newPage();await page.setViewport({width:1920,height:1200});page.setDefaultTimeout(60000);
  await page.setRequestInterception(true);
  page.on('request',r=>new URL(r.url()).hostname==='127.0.0.1' || /^(data|blob):/.test(r.url()) ? r.continue() : r.abort());
  page.on('pageerror',e=>report.errors.push(e.message));
  let explanationReads=0;const explanations=new Map();
  page.on('response',async r=>{if(r.url().endsWith('/explanations')&&r.ok()){
    explanationReads++;try{const {data}=await r.json();if(data.status==='READY')explanations.set(data.comparison_id,data);}catch{}}});
  await page.goto(origin+'/admin',{waitUntil:'domcontentloaded'});await page.waitForSelector('.admin-page');
  await page.evaluate(()=>{const details=document.querySelector('.scenario-collapsible');if(details)details.open=true;});
  const before=await page.$eval('.admin-page',e=>e.dataset.sessionId);
  await page.select('.scenario-collapsible select','S4');
  await page.waitForFunction(sid=>document.querySelector('.admin-page')?.dataset.sessionId!==sid,{},before);
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Bắt đầu ngày giao hàng'&&!b.disabled));
  await page.evaluate(()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='Bắt đầu ngày giao hàng').click());
  await page.waitForSelector('[aria-label="Delivery day"]');
  for(const mode of ['cold','warm']) {
    await page.waitForFunction(()=>{const b=document.querySelector('.cta-optimize,.cta-reoptimize');return b&&!b.disabled;});
    const previousIds=await page.$$eval('[data-job-id]',els=>els.map(e=>e.dataset.jobId));
    const count=explanations.size,started=performance.now();
    await page.click('.cta-optimize,.cta-reoptimize');
    await page.waitForFunction(previous=>{
      const cards=[...document.querySelectorAll('[data-job-id]')];
      return cards.length===3 && cards.every(c=>!previous.includes(c.dataset.jobId)) &&
        [...document.querySelectorAll('.alt-view-btn')].filter(b=>!b.disabled).length===3;
    },{},previousIds);
    const forecastsSeconds=(performance.now()-started)/1000;
    await page.click('[aria-label="Select BALANCED"]');
    await page.waitForFunction(()=>document.querySelector('.selected-explanation')?.textContent.includes('Đánh giá do AI tạo'));
    while(explanations.size===count && performance.now()-started<60000)await new Promise(r=>setTimeout(r,50));
    if(explanations.size===count)throw new Error('New comparison did not get Qwen prose');
    const totalSeconds=(performance.now()-started)/1000;
    if(totalSeconds>60)throw new Error(`Exceeded one minute: ${totalSeconds}`);
    const cards=await page.$$eval('.alt-card',els=>els.map(e=>e.textContent));
    if(cards.length!==3 || cards.some(t=>!t.includes('8 served / 0 unserved')))throw new Error('Missing full eight-order certified alternatives');
    const reads=explanationReads,switchStart=performance.now();
    const reviews={};
    for(const p of ['FASTEST','SAFER','BALANCED']) {
      await page.click(`[aria-label="Select ${p}"]`);
      await page.waitForSelector(`[aria-label="Giải thích ${p}"] li`);
      if(await page.$$eval('.selected-explanation',els=>els.length)!==1)throw new Error('Old explanation retained');
      const prose=await page.$eval('.selected-explanation li',e=>e.textContent);
      const card=cards.find(t=>t.includes(p));
      const numbers=prose.match(/\d[\d,]*(?:\.\d+)?/g) || [];
      if(numbers.length<2 || numbers.some(n=>!card.includes(n)) || /[{}]/.test(prose))throw new Error(`Ungrounded numeric review for ${p}: ${prose}`);
      reviews[p]=prose;
    }
    if(explanationReads!==reads)throw new Error('Selection regenerated an explanation');
    report.cases.push({scenario:'S4-delivery-day',mode,forecastsSeconds,totalSeconds,switchThreeMs:performance.now()-switchStart,threeCertified:true,qwenGenerated:true,reviews});
    console.log(JSON.stringify(report.cases.at(-1)));
    const panel=await page.$('.decision-intelligence');
    await (panel || page).screenshot({path:resolve(output,`${mode}.png`)});
  }
  if(report.errors.length)throw new Error(report.errors.join(';'));
  report.status='PASS';
}catch(e){report.status='FAIL';report.failure=String(e);throw e;}
finally{await writeFile(resolve(output,'result.json'),JSON.stringify(report,null,2));await browser.close();}
