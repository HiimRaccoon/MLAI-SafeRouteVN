import puppeteer from 'puppeteer-core';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const out=resolve('../outputs/delivery-layout-ui');await mkdir(out,{recursive:true});
const browser=await puppeteer.launch({headless:true,pipe:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
try{
 const page=await browser.newPage();page.setDefaultTimeout(120000);await page.setViewport({width:1600,height:1200});
 await page.setRequestInterception(true);page.on('request',r=>new URL(r.url()).hostname==='127.0.0.1'||r.url().startsWith('data:')?r.continue():r.abort());
 await page.goto('http://127.0.0.1:5185/admin',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Bắt đầu ngày giao hàng'&&!b.disabled));
 await page.evaluate(()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='Bắt đầu ngày giao hàng').click());
 await page.waitForSelector('.delivery-comparison');
 await page.click('.admin-controls summary');
 const dimensions=await page.$eval('.delivery-comparison',panel=>{
  const table=panel.querySelector('table'),wrap=table.parentElement;
  return {tableHeight:table.getBoundingClientRect().height,visibleHeight:wrap.clientHeight,headers:[...table.querySelectorAll('th')].map(x=>x.textContent)};
 });
 if(dimensions.visibleHeight<dimensions.tableHeight||dimensions.tableHeight<100)throw new Error('Comparison table clipped');
 await page.screenshot({path:resolve(out,'admin-table-menu.png'),fullPage:true});
 await writeFile(resolve(out,'result.json'),JSON.stringify({status:'PASS',...dimensions},null,2));
 console.log('DELIVERY_LAYOUT_PASS',JSON.stringify(dimensions));
}finally{await browser.close();}
