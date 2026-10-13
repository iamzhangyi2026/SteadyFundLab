#!/usr/bin/env node
// 程序化核查前端修复（审计 A–F 的运行时证据）：
//   - 健康页数据源表 tbody 行数（C：应 3，旧 50）
//   - 总览「最大回撤」瓦片值（F：应 0.00%，不带 +）
//   - 交易页「关注」按钮是否可见可点（B：btn-outline 应有边框+深色字）
const CDP = process.argv[2] || 'http://127.0.0.1:9223';
const APP = process.argv[3] || 'http://127.0.0.1:8765/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function newPage(url) {
  const res = await fetch(`${CDP}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  return res.json();
}
function rpcChannel(ws) {
  let id = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  return { send(method, params = {}) { return new Promise((res) => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); }); } };
}
async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { const t = setTimeout(() => rej(new Error('ws timeout')), 10000); ws.addEventListener('open', () => { clearTimeout(t); res(); }); ws.addEventListener('error', () => { clearTimeout(t); rej(new Error('ws err')); }); });
  return ws;
}
async function open(view) {
  const tab = await newPage(APP + '?view=' + view);
  const ws = await connect(tab.webSocketDebuggerUrl);
  const rpc = rpcChannel(ws);
  await rpc.send('Runtime.enable');
  await rpc.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 2000, deviceScaleFactor: 1, mobile: false });
  // 等数据渲染
  for (let i = 0; i < 60; i++) {
    const r = await rpc.send('Runtime.evaluate', { expression: `(()=>{const sk=document.querySelectorAll('.skeleton').length;const t=(document.querySelector('.page')?.innerText||'').trim().length;return{sk,t};})()`, returnByValue: true });
    const v = r.result && r.result.result && r.result.result.value;
    if (v && v.t > 100 && (v.sk === 0 || i > 15)) break;
    await sleep(300);
  }
  await sleep(800);
  return rpc;
}
async function evalJs(rpc, expr) {
  const r = await rpc.send('Runtime.evaluate', { expression: expr, returnByValue: true });
  return r.result && r.result.result && r.result.result.value;
}

(async () => {
  // C：健康页数据源表行数
  let rpc = await open('health');
  const healthRows = await evalJs(rpc, `document.querySelectorAll('table tbody tr').length`);
  console.log('HEALTH table tbody rows =', healthRows, '(expect 3 after dedup, was 50)');
  const healthHeaders = await evalJs(rpc, `Array.from(document.querySelectorAll('table tbody tr td strong')).map(e=>e.innerText).slice(0,6)`);
  console.log('HEALTH first rows =', JSON.stringify(healthHeaders));

  // F：总览最大回撤
  rpc = await open('overview');
  const dd = await evalJs(rpc, `(()=>{const tiles=Array.from(document.querySelectorAll('.tile'));const t=tiles.find(x=>(x.querySelector('.tile-label')||{}).innerText==='最大回撤');return t?(t.querySelector('.tile-value')||{}).innerText:null;})()`);
  console.log('OVERVIEW 最大回撤 =', JSON.stringify(dd), '(expect "0.00%", no "+")');

  // B：交易页「关注」按钮对比度（btn-outline 应有 border + 非浅色字）
  rpc = await open('trade');
  // 先尝试真实搜索渲染出「关注」按钮（search_funds 依赖 akshare 全市场表，可能离线失败）
  await evalJs(rpc, `(()=>{const inp=document.querySelector('input');if(inp){const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;setter.call(inp,'沪深300');inp.dispatchEvent(new Event('input',{bubbles:true}));const btn=[...document.querySelectorAll('button')].find(b=>b.innerText.trim()==='搜索');if(btn)btn.click();return true;}return false;})()`);
  // 等搜索结果里的 .btn-outline 出现（最多 ~18s）
  for (let i = 0; i < 60; i++) {
    const found = await evalJs(rpc, `!!document.querySelector('button.btn-outline')`);
    if (found) break;
    await sleep(300);
  }
  let btn = await evalJs(rpc, `(()=>{const b=[...document.querySelectorAll('button.btn-outline')][0];if(!b)return null;const cs=getComputedStyle(b);return{border:cs.borderTopWidth+' '+cs.borderTopColor, color:cs.color, bg:cs.backgroundColor, live:true};})()`);
  if (!btn) {
    // 离线兜底：注入同 className 元素，验证 CSS 规则确能产出"可见可点"的样式
    btn = await evalJs(rpc, `(()=>{const b=document.createElement('button');b.className='btn btn-sm btn-outline';b.textContent='关注';document.body.appendChild(b);const cs=getComputedStyle(b);const r={border:cs.borderTopWidth+' '+cs.borderTopColor, color:cs.color, bg:cs.backgroundColor, live:false};b.remove();return r;})()`);
  }
  console.log('TRADE .btn-outline computed =', JSON.stringify(btn), '(expect border ~1px rgba(15,23,42,.18), color #1f2733 dark)');

  // A：设置页 help 文字字号/色（.cell-note）
  rpc = await open('settings');
  const help = await evalJs(rpc, `(()=>{const e=[...document.querySelectorAll('.cell-note')][0];if(!e)return null;const cs=getComputedStyle(e);return{fs:cs.fontSize,color:cs.color};})()`);
  console.log('SETTINGS .cell-note computed =', JSON.stringify(help), '(expect ~13px, darker than before)');

  process.exit(0);
})().catch((e) => { console.error('ERR', e.message); process.exit(1); });
