#!/usr/bin/env node
/**
 * shoot_all.cjs — 单进程内 spawn 无头 Chrome（CDP 驱动）给 fund-sandbox 全页截图。
 *
 * 为什么不用通用截图 CLI：其后台进程在某些托管环境会被终止（exit 3 /
 * 连接被掐）。本脚本用 Node 22 内置 WebSocket/fetch 自驱 CDP，遵循以下
 * 8 条技术要点：同进程起 Chrome、--in-process-gpu（非 --single-process）、SwiftShader
 * 软件渲染、禁用 --virtual-time-budget、等数据渲染再截全页。
 *
 * 用法：
 *   node scripts/shoot_all.cjs [--out DIR] [--app URL] [--width 1440] [--port 9222]
 *                              [--views a,b,c] [--chrome "C:/.../chrome.exe"]
 * 例：
 *   node scripts/shoot_all.cjs --out screenshots/baseline
 *   node scripts/shoot_all.cjs --out screenshots/after
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function arg(name, def) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

const APP = arg('app', 'http://127.0.0.1:8765/');
const OUT = arg('out', path.join(__dirname, '..', 'screenshots'));
const WIDTH = parseInt(arg('width', '1440'), 10);
const PORT = parseInt(arg('port', '9222'), 10);
const SETTLE = parseInt(arg('settle', '1000'), 10);
const MAXWAIT = parseInt(arg('maxwait', '14000'), 10);
const VIEWS = arg('views', 'overview,positions,trade,news,analysis,intel,health,settings').split(',');
const isWin = process.platform === 'win32';

function findChrome() {
  const p = arg('chrome', '');
  if (p && fs.existsSync(p)) return p;
  for (const c of [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  ]) if (fs.existsSync(c)) return c;
  return null;
}
const CHROME = findChrome();
if (!CHROME) { console.error('✗ 找不到 Chrome，请用 --chrome 指定路径'); process.exit(2); }

fs.mkdirSync(OUT, { recursive: true });

function killStale() {
  return new Promise((res) => {
    const k = isWin
      ? spawn('taskkill', ['//F', '//IM', 'chrome.exe'], { stdio: 'ignore' })
      : spawn('pkill', ['-f', 'chrome'], { stdio: 'ignore' });
    k.on('close', () => res());
  });
}

// ---------- CDP ----------
async function cdpVersion() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
  return r.json();
}
async function newPage(url) {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!res.ok) throw new Error(`/json/new HTTP ${res.status}`);
  return res.json();
}
function rpcChannel(ws) {
  let id = 0; const pending = new Map(); const events = [];
  ws.addEventListener('message', (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    else if (m.method) events.push(m);
  });
  return {
    events,
    send(method, params = {}) {
      return new Promise((resolve) => {
        const mid = ++id; pending.set(mid, resolve);
        ws.send(JSON.stringify({ id: mid, method, params }));
      });
    },
  };
}
async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('ws timeout')), 12000);
    ws.addEventListener('open', () => { clearTimeout(t); resolve(); });
    ws.addEventListener('error', () => { clearTimeout(t); reject(new Error('ws err')); });
  });
  return ws;
}
async function waitReady(rpc) {
  const start = Date.now();
  let last = '';
  while (Date.now() - start < MAXWAIT) {
    const r = await rpc.send('Runtime.evaluate', {
      expression: `(() => {
        const sk = document.querySelectorAll('.skeleton, .sk, [class*="skeleton"]').length;
        const tiles = document.querySelectorAll('.tile, .card').length;
        const txt = (document.body.innerText || '').trim().length;   // '.page' 容器在分析页不存在 → 恒 0，会导致过早判定就绪、截到空页
        return { sk, tiles, txt };
      })()`,
      returnByValue: true,
    });
    const v = r.result && r.result.result && r.result.result.value;
    if (v) {
      last = `skeleton=${v.sk} cards=${v.tiles} text=${v.txt}`;
      const el = Date.now() - start;
      // 必须**骨架清零 + 有实质文本**才算就绪。
      // 原条件 (tiles>0 || txt>120) 里 tiles 数的是 .card（卡片框架），
      // 卡片一渲染出来就 >0，于是数据还没到就 break → 截出来全是「暂无XX」。
      if (v.sk === 0 && v.txt > 200 && el >= SETTLE) break;
    }
    await sleep(300);
  }
  await sleep(SETTLE);
  return last;
}
async function shootOne(view) {
  const url = APP + (APP.includes('?') ? '&' : '?') + 'view=' + view + '&t=' + Date.now();  // 缓存旁路：不加时间戳，Chrome 会用磁盘缓存的旧 dist，重新 build 也看不到
  const tab = await newPage(url);
  const ws = await connect(tab.webSocketDebuggerUrl);
  const rpc = rpcChannel(ws);
  const errs = [];
  await rpc.send('Runtime.enable');
  await rpc.send('Page.enable');
  await rpc.send('Log.enable').catch(() => {});
  await rpc.send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: 2000, deviceScaleFactor: 1, mobile: false });
  const info = await waitReady(rpc);
  for (const ev of rpc.events) {
    if (ev.method === 'Runtime.consoleAPICalled' && ev.params.type === 'error')
      errs.push((ev.params.args || []).map((a) => a.value || a.description || '').join(' '));
    if (ev.method === 'Log.entryAdded' && ev.params.entry.level === 'error')
      errs.push(ev.params.entry.text);
  }
  const m = await rpc.send('Page.getLayoutMetrics');
  const css = (m.result && (m.result.cssContentSize || m.result.contentSize)) || { height: 1000 };
  const fullH = Math.min(Math.max(Math.ceil(css.height || 2000), 2000), 9000);
  await rpc.send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: fullH, deviceScaleFactor: 1, mobile: false });
  await sleep(500);
  const shot = await rpc.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const b64 = shot.result && shot.result.data;
  if (!b64) throw new Error('no screenshot data');
  const file = path.join(OUT, `${view}.png`);
  fs.writeFileSync(file, Buffer.from(b64, 'base64'));
  await rpc.send('Page.close').catch(() => {});
  await sleep(150);
  ws.close();
  return { view, file, size: fs.statSync(file).size, height: fullH, info, errs };
}

(async () => {
  console.log(`Chrome: ${CHROME}`);
  console.log(`App:   ${APP}`);
  console.log(`Out:   ${OUT}`);
  console.log(`Views: ${VIEWS.join(', ')}`);
  console.log('---');
  await killStale();
  await sleep(800);
  const prof = path.join(OUT, '.chrome-profile');
  fs.rmSync(prof, { recursive: true, force: true });
  const chromeProc = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
    '--in-process-gpu', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
    '--hide-scrollbars', `--remote-debugging-port=${PORT}`, `--user-data-dir=${prof}`, 'about:blank',
  ], { stdio: 'ignore' });

  let ready = false;
  for (let i = 0; i < 50; i++) {
    try { await cdpVersion(); ready = true; break; } catch { await sleep(300); }
  }
  if (!ready) {
    console.error('✗ Chrome 未在端口 ' + PORT + ' 就绪（可能 sandbox 限制或端口被占）');
    try { chromeProc.kill('SIGKILL'); } catch {}
    process.exit(1);
  }

  const results = [];
  const allErrs = [];
  for (const v of VIEWS) {
    try {
      const r = await shootOne(v);
      results.push(r);
      const tag = r.errs.length ? ` ⚠ console×${r.errs.length}` : '';
      console.log(`✓ ${v.padEnd(10)} ${String(r.size).padStart(8)}B  ${WIDTH}x${r.height}  ${r.info}${tag}`);
      r.errs.slice(0, 3).forEach((e) => { allErrs.push(`[${v}] ${e}`); console.log('    ! ' + String(e).slice(0, 200)); });
    } catch (e) {
      console.log(`✗ ${v.padEnd(10)} FAILED: ${e.message}`);
    }
  }

  // 优雅关闭浏览器
  try {
    const v = await cdpVersion();
    const ws = await connect(v.webSocketDebuggerUrl);
    ws.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
    await sleep(400);
  } catch {}
  try { chromeProc.kill('SIGTERM'); } catch {}
  await sleep(300);
  try { chromeProc.kill('SIGKILL'); } catch {}
  await killStale();

  console.log(`\nDone. ${results.length}/${VIEWS.length} captured → ${OUT}`);
  if (allErrs.length) console.log(`⚠ 共 ${allErrs.length} 条控制台报错（见上）`);
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
