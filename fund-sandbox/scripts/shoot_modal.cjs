#!/usr/bin/env node
/**
 * shoot_modal.cjs — 专门截「基金详情弹窗」(FundDetailModal) 验证 M6：手写 table → .table。
 * 复用 shoot_all.cjs 的 CDP 框架（同进程 spawn Chrome、SwiftShader、关 tab）。
 * 默认端口 9224（避开 shoot_all 的 9222，二者可先后跑）。
 *
 * 用法：node scripts/shoot_modal.cjs [--out DIR] [--chrome "..."] [--port 9224]
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
const OUT = arg('out', path.join(__dirname, '..', 'screenshots', 'after'));
const WIDTH = parseInt(arg('width', '1440'), 10);
const PORT = parseInt(arg('port', '9224'), 10);
const SETTLE = 1000;
const isWin = process.platform === 'win32';

function findChrome() {
  const p = arg('chrome', '');
  if (p && fs.existsSync(p)) return p;
  const base = path.join(os.homedir(), '.agent-browser', 'browsers');
  if (fs.existsSync(base)) {
    const vers = fs.readdirSync(base).filter((d) => d.startsWith('chrome-')).sort().reverse();
    for (const v of vers) {
      const c = path.join(base, v, 'chrome.exe');
      if (fs.existsSync(c)) return c;
    }
  }
  for (const c of ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'])
    if (fs.existsSync(c)) return c;
  return null;
}
const CHROME = findChrome();
if (!CHROME) { console.error('✗ 找不到 Chrome'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

function killStale() {
  return new Promise((res) => {
    const k = isWin ? spawn('taskkill', ['//F', '//IM', 'chrome.exe'], { stdio: 'ignore' })
      : spawn('pkill', ['-f', 'chrome'], { stdio: 'ignore' });
    k.on('close', () => res());
  });
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
  return { events, send(method, params = {}) { return new Promise((resolve) => { const mid = ++id; pending.set(mid, resolve); ws.send(JSON.stringify({ id: mid, method, params })); }); } };
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
const ev = (rpc, expr) => rpc.send('Runtime.evaluate', { expression: expr, returnByValue: true })
  .then((r) => r.result && r.result.result && r.result.result.value);

(async () => {
  console.log('Chrome:', CHROME, '| port', PORT);
  await killStale();
  await sleep(800);
  const prof = path.join(OUT, '.chrome-profile-modal');
  fs.rmSync(prof, { recursive: true, force: true });
  const chromeProc = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
    '--in-process-gpu', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
    '--hide-scrollbars', `--remote-debugging-port=${PORT}`, `--user-data-dir=${prof}`, 'about:blank',
  ], { stdio: 'ignore' });
  let ready = false;
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${PORT}/json/version`); ready = true; break; } catch { await sleep(300); } }
  if (!ready) { try { chromeProc.kill('SIGKILL'); } catch {} console.error('✗ Chrome 未就绪'); process.exit(1); }

  const tab = await newPage(APP + '?view=positions');
  const ws = await connect(tab.webSocketDebuggerUrl);
  const rpc = rpcChannel(ws);
  const errs = [];
  await rpc.send('Runtime.enable');
  await rpc.send('Page.enable');
  await rpc.send('Log.enable').catch(() => {});
  await rpc.send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: 2000, deviceScaleFactor: 1, mobile: false });

  // 确保持仓 tab 激活（默认可能是关注）
  const tabState = await ev(rpc, `(() => {
    const btns = Array.from(document.querySelectorAll('button.tab'));
    const hold = btns.find((b) => (b.textContent || '').includes('持仓'));
    if (!hold) return 'none';
    if (hold.getAttribute('aria-selected') !== 'true') { hold.click(); return 'clicked'; }
    return 'already';
  })()`);
  console.log('tab(持仓):', tabState);
  // 等持仓列表加载（数据异步，给足时间）
  for (let i = 0; i < 80; i++) {
    const has = await ev(rpc, "(() => { return !!document.querySelector('.name-btn'); })()");
    if (has) break;
    await sleep(300);
  }
  // 点第一个基金名按钮 → 开详情弹窗
  const clicked = await ev(rpc, `(() => { const b = document.querySelector('.name-btn'); if (b) { b.click(); return true; } return false; })()`);
  console.log(clicked ? '✓ 已点击 .name-btn 打开详情' : '✗ 未找到 .name-btn');

  // 等弹窗出现 + 前十大重仓表渲染（数据异步加载）
  let modalOk = false;
  for (let i = 0; i < 40; i++) {
    const st = await ev(rpc, `(() => {
      const modal = document.querySelector('.modal');
      if (!modal) return { modal: false };
      const hasTable = !!modal.querySelector('table.table');
      const rows = modal.querySelectorAll('table.table tbody tr').length;
      const txt = (modal.innerText || '').length;
      return { modal: true, hasTable, rows, txt };
    })()`);
    if (st && st.modal && st.hasTable && st.rows > 0) { modalOk = true; console.log(`✓ 弹窗已渲染：前十大重仓表 ${st.rows} 行，文本 ${st.txt}`); break; }
    await sleep(400);
  }
  if (!modalOk) console.log('⚠ 弹窗/表格未完全渲染（仍截图，肉眼确认）');
  await sleep(SETTLE);

  for (const e of rpc.events) {
    if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') errs.push((e.params.args || []).map((a) => a.value || a.description || '').join(' '));
    if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') errs.push(e.params.entry.text);
  }
  const m = await rpc.send('Page.getLayoutMetrics');
  const css = (m.result && (m.result.cssContentSize || m.result.contentSize)) || { height: 1000 };
  const fullH = Math.min(Math.max(Math.ceil(css.height || 2000), 2000), 9000);
  await rpc.send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: fullH, deviceScaleFactor: 1, mobile: false });
  await sleep(500);
  const shot = await rpc.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const b64 = shot.result && shot.result.data;
  if (!b64) { console.error('✗ 无截图数据'); process.exit(1); }
  const file = path.join(OUT, 'fund-detail.png');
  fs.writeFileSync(file, Buffer.from(b64, 'base64'));

  try { const v = await fetch(`http://127.0.0.1:${PORT}/json/version`).then((r) => r.json()); const c = await connect(v.webSocketDebuggerUrl); c.send(JSON.stringify({ id: 1, method: 'Browser.close' })); await sleep(400); } catch {}
  try { chromeProc.kill('SIGTERM'); } catch {}
  await sleep(300);
  try { chromeProc.kill('SIGKILL'); } catch {}
  await killStale();
  console.log(`✓ 详情弹窗截图 → ${file} (${fs.statSync(file).size}B) ${errs.length ? '⚠ console×' + errs.length : ''}`);
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
