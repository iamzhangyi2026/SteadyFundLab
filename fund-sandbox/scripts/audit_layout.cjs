#!/usr/bin/env node
/**
 * audit_layout.cjs — 用 CDP 逐页实测布局对齐（统计条是否撑满、卡片左右边缘是否一致）。
 * 不截图，只读 getBoundingClientRect，输出可核对的数字。端口 9226。
 * 用法：node scripts/audit_layout.cjs [--port 9226]
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function arg(name, def) { const i = process.argv.indexOf('--' + name); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def; }
const APP = arg('app', 'http://127.0.0.1:8765/');
const PORT = parseInt(arg('port', '9226'), 10);
const WIDTH = parseInt(arg('width', '1440'), 10);
const VIEWS = arg('views', 'overview,positions,trade,news,analysis,intel,triage,health,settings').split(',');
const isWin = process.platform === 'win32';

function findChrome() {
  const base = path.join(os.homedir(), '.agent-browser', 'browsers');
  if (fs.existsSync(base)) {
    for (const v of fs.readdirSync(base).filter((d) => d.startsWith('chrome-')).sort().reverse()) {
      const c = path.join(base, v, 'chrome.exe');
      if (fs.existsSync(c)) return c;
    }
  }
  for (const c of ['C:/Program Files/Google/Chrome/Application/chrome.exe']) if (fs.existsSync(c)) return c;
  return null;
}
const CHROME = findChrome();
if (!CHROME) { console.error('✗ 找不到 Chrome'); process.exit(2); }
function killStale() { return new Promise((res) => { const k = isWin ? spawn('taskkill', ['//F', '//IM', 'chrome.exe'], { stdio: 'ignore' }) : spawn('pkill', ['-f', 'chrome'], { stdio: 'ignore' }); k.on('close', () => res()); }); }
async function newPage(url) { const r = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' }); return r.json(); }
function rpcChannel(ws) {
  let id = 0; const pending = new Map();
  ws.addEventListener('message', (e) => { let m; try { m = JSON.parse(e.data); } catch { return; } if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
  return { send: (method, params = {}) => new Promise((res) => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }) };
}
async function connect(u) { const ws = new WebSocket(u); await new Promise((res, rej) => { const t = setTimeout(() => rej(new Error('ws timeout')), 12000); ws.addEventListener('open', () => { clearTimeout(t); res(); }); ws.addEventListener('error', () => { clearTimeout(t); rej(new Error('ws err')); }); }); return ws; }
const ev = (rpc, expr) => rpc.send('Runtime.evaluate', { expression: expr, returnByValue: true }).then((r) => r.result && r.result.result && r.result.result.value);

const PROBE = `(() => {
  const R = (el) => { const r = el.getBoundingClientRect(); return { l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) }; };
  const chips = [...document.querySelectorAll('.stat-chip')].map(R);
  const strip = document.querySelector('.stat-strip');
  const cards = [...document.querySelectorAll('.card')].map((el) => {
    const t = (el.querySelector('.card-title, .card-head h3, h3, .kicker')?.textContent || '').trim().slice(0, 14);
    return Object.assign({ t }, R(el));
  });
  return {
    vw: window.innerWidth,
    chips: chips.map((c) => c.w),
    chipL: chips.length ? chips[0].l : null,
    chipR: chips.length ? Math.max(...chips.map((c) => c.r)) : null,
    stripR: strip ? Math.round(strip.getBoundingClientRect().right) : null,
    stripL: strip ? Math.round(strip.getBoundingClientRect().left) : null,
    cards,
  };
})()`;

(async () => {
  console.log('Chrome:', CHROME, '| port', PORT);
  await killStale(); await sleep(700);
  const prof = path.join(os.tmpdir(), 'audit-layout-prof-' + Date.now());
  fs.rmSync(prof, { recursive: true, force: true });
  const chromeProc = spawn(CHROME, ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--in-process-gpu', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--hide-scrollbars', `--remote-debugging-port=${PORT}`, `--user-data-dir=${prof}`, 'about:blank'], { stdio: 'ignore' });
  let ready = false;
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${PORT}/json/version`); ready = true; break; } catch { await sleep(300); } }
  if (!ready) { try { chromeProc.kill('SIGKILL'); } catch {} console.error('✗ Chrome 未就绪'); process.exit(1); }

  for (const view of VIEWS) {
    try {
      const tab = await newPage(APP + '?view=' + view);
      const ws = await connect(tab.webSocketDebuggerUrl);
      const rpc = rpcChannel(ws);
      await rpc.send('Runtime.enable');
      await rpc.send('Page.enable');
      await rpc.send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: 2000, deviceScaleFactor: 1, mobile: false });
      // 等渲染
      for (let i = 0; i < 30; i++) { const t = await ev(rpc, "(() => { const s=document.querySelectorAll('[class*=skeleton]').length; const x=(document.body.innerText||'').trim().length; return s===0 && x>60; })()"); if (t) break; await sleep(400); }
      await sleep(900);
      const d = await ev(rpc, PROBE);
      console.log('\n===== ' + view + ' =====');
      if (!d) { console.log('  (no data)'); }
      else {
        if (d.chips && d.chips.length) {
          const gap = d.stripR != null ? d.stripR - d.chipR : null;
          console.log(`  统计条: ${d.chips.length} 个  宽度=[${d.chips.join(', ')}]  芯片右=${d.chipR}  条右=${d.stripR}  右侧空隙=${gap}`);
          const uniq = [...new Set(d.chips)];
          console.log(`  芯片宽度是否一致: ${uniq.length === 1 ? '是' : '否 → ' + uniq.join('/')}`);
        } else console.log('  统计条: 无');
        const L = [...new Set(d.cards.map((c) => c.l))];
        const Rt = [...new Set(d.cards.map((c) => c.r))];
        console.log(`  卡片 ${d.cards.length} 张  左边缘集合=[${L.join(', ')}]  右边缘集合=[${Rt.join(', ')}]`);
        d.cards.forEach((c) => console.log(`    · ${(c.t || '(无题)').padEnd(14)} w=${String(c.w).padStart(5)} h=${String(c.h).padStart(5)} l=${c.l} r=${c.r}`));
      }
      await rpc.send('Page.close').catch(() => {});
      await sleep(150); ws.close();
    } catch (e) { console.log(`  ✗ ${view} FAILED: ${e.message}`); }
  }

  try { const v = await fetch(`http://127.0.0.1:${PORT}/json/version`).then((r) => r.json()); const c = await connect(v.webSocketDebuggerUrl); c.send(JSON.stringify({ id: 1, method: 'Browser.close' })); await sleep(400); } catch {}
  try { chromeProc.kill('SIGTERM'); } catch {} await sleep(300); try { chromeProc.kill('SIGKILL'); } catch {}
  await killStale();
  process.exit(0);
})().catch((e) => { console.error('FATAL', e.message); process.exit(1); });
