import puppeteer from 'puppeteer-core';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
const output = resolve(process.argv[2] || '../outputs/workspace-collapse-20261014');
await mkdir(output, { recursive: true });
const browser = await puppeteer.launch({ headless: true, pipe: true, timeout: 60000,
  executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', userDataDir: resolve(output, 'browser-profile') });
const report = { status: 'RUNNING', states: [], errors: [] };
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });
  await page.setRequestInterception(true);
  page.on('request', r => new URL(r.url()).hostname === '127.0.0.1' || /^(data|blob):/.test(r.url()) ? r.continue() : r.abort());
  page.on('pageerror', e => report.errors.push(e.message));
  await page.goto('http://127.0.0.1:5185/admin', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.workspace-toggle');
  const settle = () => new Promise(r => setTimeout(r, 350));
  const measure = () => page.evaluate(() => {
    const rect = selector => { const { x, y, width, height } = document.querySelector(selector).getBoundingClientRect(); return { x, y, width, height }; };
    return { center: rect('.admin-center'), map: rect('.dispatch-map'), comparison: rect('.workspace-panel-comparison'),
      session: document.querySelector('.admin-page').dataset.sessionId,
      overflow: document.documentElement.scrollWidth > innerWidth,
      buttons: [...document.querySelectorAll('.workspace-toggle')].map(e => ({ label: e.getAttribute('aria-label'), opacity: getComputedStyle(e).opacity })) };
  });
  await page.mouse.move(900, 20); await settle();
  const baseline = await measure();
  assert(baseline.buttons.every(b => b.opacity === '0'), 'Controls must be hidden away from their regions');
  for (const area of ['left', 'right', 'comparison']) {
    await page.hover(`.workspace-panel-${area} > .workspace-toggle`); await settle();
    assert.equal(await page.$eval(`.workspace-panel-${area} > .workspace-toggle`, e => getComputedStyle(e).opacity), '1');
    await page.screenshot({ path: resolve(output, `hover-${area}.png`) });
  }
  const areas = ['left', 'right', 'comparison'];
  for (let mask = 0; mask < 8; mask++) {
    for (let i = 0; i < areas.length; i++) {
      const selector = `.workspace-panel-${areas[i]} > .workspace-toggle`;
      const collapsed = await page.$eval(selector, e => e.getAttribute('aria-expanded') === 'false');
      if (collapsed !== Boolean(mask & (1 << i))) {
        await page.hover(selector); await settle(); await page.click(selector); await settle();
      }
    }
    await page.mouse.move(900, 20); await settle();
    const state = await measure(); report.states.push({ mask, ...state });
    assert.equal(state.session, baseline.session, 'Collapse must not replace the session');
    assert(!state.overflow, 'No horizontal overflow');
    assert(Math.abs(state.map.width - state.comparison.width) < 2, 'Comparison must follow map width');
    if (mask & 1) assert(state.map.x < baseline.map.x - 250, 'Map must expand left');
    if (mask & 2) assert(state.map.x + state.map.width > baseline.map.x + baseline.map.width + 350, 'Map must expand right');
    if (mask & 4) assert(state.map.height > baseline.map.height + 180, 'Map must grow down');
    assert(state.buttons.every(b => b.opacity === '0'), 'Controls must hide after pointer leaves');
    if ([1, 2, 3, 7].includes(mask)) await page.screenshot({ path: resolve(output, `collapsed-${mask}.png`) });
  }
  // Keyboard access still reveals an otherwise hidden restore control.
  await page.focus('.workspace-panel-left > .workspace-toggle');
  await page.keyboard.press('Enter'); await settle();
  assert.equal(await page.$eval('.workspace-panel-left > .workspace-toggle', e => e.getAttribute('aria-expanded')), 'true');
  // Restore all regions without remounting their contents.
  for (const area of ['right', 'comparison']) {
    await page.hover(`.workspace-panel-${area} > .workspace-toggle`); await settle();
    await page.click(`.workspace-panel-${area} > .workspace-toggle`); await settle();
  }
  const restored = await measure();
  assert(Math.abs(restored.map.width - baseline.map.width) < 2);
  assert(Math.abs(restored.map.height - baseline.map.height) < 2);
  for (const width of [1280, 768]) {
    await page.setViewport({ width, height: 900 }); await settle();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow at ${width}px`);
    await page.screenshot({ path: resolve(output, `responsive-${width}.png`) });
  }
  assert.deepEqual(report.errors, []);
  report.status = 'PASS';
} catch (error) { report.status = 'FAIL'; report.failure = String(error); throw error; }
finally { await writeFile(resolve(output, 'result.json'), JSON.stringify(report, null, 2)); await browser.close(); }
