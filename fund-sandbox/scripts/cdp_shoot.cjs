#!/usr/bin/env node
/**
 * cdp_shoot.cjs — 用 Chrome DevTools Protocol 驱动无头 Chrome 给 fund-sandbox
 * 前端各页面截图。不依赖 Playwright/Puppeteer（用 Node 22 内置 WebSocket/fetch）。
 *
 * 为什么不用 `chrome --screenshot`：那样只等 load 事件，SPA 数据还没渲染完，
 * 截到的是骨架屏；也不支持「等待数据 → 再截」，更不支持全页高度。
 *
 * 用法：
 *   node scripts/cdp_shoot.cjs [--out DIR] [--app http://127.0.0.1:8765/] [--width 1440]
 *   （需先以 --remote-debugging-port=9222 起好无头 Chrome，见 scripts/shoot_all.sh）
 */
const fs = require('fs');
const path = require('path');

function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

const CDP = arg('cdp', 'http://127.0.0.1:9222');
const APP = arg('app', 'http://127.0.0.1:8765/');
const OUT = arg('out', path.join(__dirname, '..', 'screenshots'));
const WIDTH = parseInt(arg('width', '1440'), 10);
const HEIGHT = parseInt(arg('height', '2000'), 10);
const SETTLE_MS = parseInt(arg('settle', '1500'), 10);
const MAX_WAIT_MS = parseInt(arg('maxwait', '20000'), 10);
const VIEWS = (arg('views', 'overview,positions,trade,news,analysis,intel,triage,health,settings')).split(',');

fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function newPage(url) {
  // 新版 Chrome 需要 PUT 才会创建目标页
  const res = await fetch(`${CDP}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!res.ok) throw new Error(`/json/new failed: HTTP ${res.status}`);
  return res.json();
}

function rpcChannel(ws) {
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener('message', (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    else if (msg.method) events.push(msg);
  });
  return {
    events,
    send(method, params = {}) {
      return new Promise((resolve) => {
        const mid = ++id;
        pending.set(mid, resolve);
        ws.send(JSON.stringify({ id: mid, method, params }));
      });
    },
  };
}

async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('ws connect timeout')), 10000);
    ws.addEventListener('open', () => { clearTimeout(t); resolve(); });
    ws.addEventListener('error', (e) => { clearTimeout(t); reject(new Error('ws error')); });
  });
  return ws;
}

// 等待「数据渲染完成」：骨架屏消失 + 页面有实质节点；再兜一个最短 settle
async function waitReady(rpc, pageEvents) {
  const start = Date.now();
  let lastInfo = '';
  while (Date.now() - start < MAX_WAIT_MS) {
    const r = await rpc.send('Runtime.evaluate', {
      expression: `(() => {
        const sk = document.querySelectorAll('.skeleton, .sk, [class*="skeleton"]').length;
        const tiles = document.querySelectorAll('.tile, .card').length;
        const txt = (document.querySelector('.page')?.innerText || '').trim().length;
        return { sk, tiles, txt };
      })()`,
      returnByValue: true,
    });
    const v = r.result && r.result.result && r.result.result.value;
    if (v) {
      lastInfo = `skeleton=${v.sk} cards=${v.tiles} text=${v.txt}`;
      const elapsed = Date.now() - start;
      if ((v.tiles > 0 || v.txt > 120) && (v.sk === 0 || elapsed > 5000) && elapsed >= SETTLE_MS) break;
    }
    await sleep(300);
  }
  // 再等一帧 + 固定 settle，给 ECharts canvas 动画留时间
  await sleep(SETTLE_MS);
  return lastInfo;
}

async function shootOne(view) {
  const url = APP + (APP.includes('?') ? '&' : '?') + 'view=' + view;
  const tab = await newPage(url);
  const ws = await connect(tab.webSocketDebuggerUrl);
  const rpc = rpcChannel(ws);

  const consoleErrors = [];
  await rpc.send('Runtime.enable');
  await rpc.send('Page.enable');
  await rpc.send('Log.enable').catch(() => {});

  await rpc.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
  });

  const info = await waitReady(rpc);

  for (const ev of rpc.events) {
    if (ev.method === 'Runtime.consoleAPICalled' && ev.params.type === 'error') {
      consoleErrors.push((ev.params.args || []).map((a) => a.value || a.description || '').join(' '));
    }
    if (ev.method === 'Log.entryAdded' && ev.params.entry.level === 'error') {
      consoleErrors.push(ev.params.entry.text);
    }
  }

  // 全页高度
  const m = await rpc.send('Page.getLayoutMetrics');
  const css = (m.result && (m.result.cssContentSize || m.result.contentSize)) || { height: 1000 };
  const fullH = Math.min(Math.max(Math.ceil(css.height || HEIGHT), HEIGHT), 9000);
  await rpc.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: fullH, deviceScaleFactor: 1, mobile: false,
  });
  await sleep(500);

  const shot = await rpc.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const b64 = shot.result && shot.result.data;
  if (!b64) throw new Error('no screenshot data');
  const file = path.join(OUT, `${view}.png`);
  fs.writeFileSync(file, Buffer.from(b64, 'base64'));
  const size = fs.statSync(file).size;

  ws.close();
  return { view, file, size, height: fullH, info, errors: consoleErrors };
}

async function closeBrowser() {
  try {
    const v = await (await fetch(`${CDP}/json/version`)).json();
    const ws = await connect(v.webSocketDebuggerUrl);
    ws.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
    await sleep(400);
  } catch { /* 忽略 */ }
}

(async () => {
  const results = [];
  for (const v of VIEWS) {
    try {
      const r = await shootOne(v);
      results.push(r);
      const errTag = r.errors.length ? ` ⚠ console×${r.errors.length}` : '';
      console.log(`✓ ${v.padEnd(10)} ${String(r.size).padStart(8)}B  ${WIDTH}x${r.height}  ${r.info}${errTag}`);
      if (r.errors.length) r.errors.slice(0, 3).forEach((e) => console.log(`    ! ${String(e).slice(0, 200)}`));
    } catch (e) {
      console.log(`✗ ${v.padEnd(10)} FAILED: ${e.message}`);
    }
  }
  await closeBrowser();
  console.log(`\nDone. ${results.length}/${VIEWS.length} captured → ${OUT}`);
})();
