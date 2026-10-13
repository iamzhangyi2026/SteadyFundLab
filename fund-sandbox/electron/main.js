/**
 * fund-sandbox 桌面入口（Electron 版）
 * ====================================
 * 这是一个**本地桌面应用**，不是网站。Electron 用 Chromium 内核渲染，
 * 但外面套的是原生窗口壳——没有地址栏、没有标签页、没有书签栏，
 * 双击打开就是一个普通桌面软件。
 *
 * 架构
 * ----
 *   主进程 (main.js)
 *     ├─ 先 spawn 起 Python 后端（uvicorn，监听 127.0.0.1:8765）
 *     ├─ 轮询 /api/status 直到返回 200（后端就绪）
 *     └─ 创建 BrowserWindow，加载 http://127.0.0.1:8765
 *   渲染进程
 *     └─ 就是现有的 frontend/index.html（由 FastAPI 在 / 提供），一行不改
 *
 * 关窗时主进程会 kill 掉 Python 子进程，不会残留端口占用。
 *
 * 启动方式
 * --------
 *   npm start              ← 开发/调试（带 DevTools）
 *   打包后双击 fund-sandbox.exe  ← 分发版
 */

'use strict';

const { app, BrowserWindow, Menu, shell, ipcMain } = require('electron');
const { spawn, execSync } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

// fund-sandbox 工程根目录。
// 开发态：electron/ 的上一级（项目根）。
// 打包态（electron-builder 把 backend/data/config/frontend/dist/runtime/python 作为 extraResources
// 落在 process.resourcesPath）：用 resourcesPath，那里结构与开发态一致，后端 __file__ 相对解析照常工作。
// 仅打包态的根目录取值不同，开发态与打包态行为一致。
const ROOT = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');

// Python 解释器：只用随包打包的独立运行时 runtime/python（路径无关、换机器可跑）。
// 打包分发版只带 runtime/python 即可，无需目标机装 Python；开发用 .venv 已移除，不再回退。
function resolvePython() {
  return path.join(ROOT, 'runtime', 'python', 'python.exe');
}
const PY = resolvePython();
// 后端环境变量：打包态注入 FUND_SANDBOX_CONFIG_DIR 指向可写 userData，
// 让 LLM Key 等用户私密配置写进可写目录（不进只读 resources、不打进 exe）。
function backendEnv() {
  const env = Object.assign({}, process.env);
  if (app.isPackaged) {
    env.FUND_SANDBOX_CONFIG_DIR = app.getPath('userData');
  }
  return env;
}
const HOST = '127.0.0.1';
const PORT = 8765;
const URL = `http://${HOST}:${PORT}`;

// ── 后端起护参数 ────────────────────────────────────────────────────────────
// 移植前提：终端用户没有排障能力，后端一旦在运行中挂掉，必须自己站起来，
// 并且把「挂过 / 拉起来过」留在文件里，事后查得到。
const WATCHDOG_INTERVAL_MS = 10000;   // 健康探测间隔
const WATCHDOG_FAIL_LIMIT = 3;        // 连续失败几次就判定后端已失效（约 30 秒）
const RESPAWN_BASE_DELAY_MS = 3000;   // 自动拉起基础退避
const RESPAWN_MAX_DELAY_MS = 60000;   // 退避上限（连续失败时逐步拉长，避免空转）
const WATCHDOG_LOG = path.join(ROOT, 'data', 'logs', 'watchdog.log');
const WATCHDOG_STATE = path.join(ROOT, 'data', 'logs', 'watchdog-status.json');

let backend = null;   // Python 子进程句柄
let ownBackend = false; // 后端是否由本次启动拉起（复用时为 false，退出时不杀）
let booting = false;  // 是否处于「本次启动拉起后端」的等待窗口（用于自动重试）
let bootRetries = 0;  // 启动期已重拉次数

// ── 运行期看护状态 ──────────────────────────────────────────────────────────
let shuttingDown = false;    // 应用自身正在退出 → 不触发看护重拉（避免关窗后反复复活）
let watchdogTimer = null;    // 健康巡检定时器
let watchdogFails = 0;       // 连续探测失败次数
let watchdogAttempts = 0;    // 已自动拉起次数（同时用于退避长度）
let respawnPending = false;  // 已有一次拉起排队中（防重复拉起）
let backendHealthy = true;   // 当前后端是否判定为健康

/**
 * 拉起 FastAPI 后端。
 * 用 `python -m uvicorn` 而不是 app.py，避免再依赖 pywebview 那套壳。
 */
function spawnOnce() {
  backend = spawn(
    PY,
    ['-m', 'uvicorn', 'backend.api.main:app',
     '--host', HOST, '--port', String(PORT), '--log-level', 'warning'],
    { cwd: ROOT, windowsHide: true, env: backendEnv() }
  );

  backend.stdout.on('data', (d) => console.log('[backend]', d.toString().trim()));
  backend.stderr.on('data', (d) => console.error('[backend]', d.toString().trim()));
  backend.on('exit', (code) => {
    if (shuttingDown) return; // 应用自身正在退出（关窗 / 刷新重启），属预期
    // 启动等待期内非预期退出（最常见：上一进程刚被 relaunch/关闭杀掉，
    // Windows 端口仍被 TIME_WAIT 短暂占用，新 uvicorn bind 报 10048 即退）——
    // 等端口释放后自动重拉，避免「启动失败 → 应用停摆」。
    if (booting && code !== 0 && bootRetries < 3) {
      bootRetries += 1;
      console.log(`[main] 后端非预期退出（code=${code}），${1500 * bootRetries}ms 后第 ${bootRetries} 次重拉…`);
      setTimeout(() => {
        if (booting) spawnOnce();
      }, 1500 * bootRetries);
      return;
    }
    // 运行期退出：后端崩溃且非启动等待期时进入看护流程。
    // 落盘记录 + 自动拉起，避免应用悄无声息地挂死、只能靠用户手动重开。
    watchdogLog(`后端进程意外退出（exit code=${code}），进入自动拉起流程`);
    backendHealthy = false;
    scheduleRespawn();
  });
}

function startBackend() {
  booting = true;
  bootRetries = 0;
  spawnOnce();
}

function stopBooting() {
  booting = false;
}

// ── 运行期看护（watchdog）────────────────────────────────────────────────────
// 为什么需要：启动完成后若后端崩溃，Electron 默认不重拉，应用会悄无声息地挂死。
// 这层 watchdog 负责运行期自动拉起后端，避免只能靠用户手动重开。

/**
 * 看护日志：写文件（事后查得到）+ 送渲染进程（界面上看得见）。
 * 日志 IO 失败绝不能影响看护本身，故全部吞异常。
 */
function watchdogLog(msg) {
  const stamp = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const line = `[${stamp}] ${msg}`;
  console.log('[watchdog]', msg);
  try {
    fs.mkdirSync(path.dirname(WATCHDOG_LOG), { recursive: true });
    fs.appendFileSync(WATCHDOG_LOG, line + '\n', 'utf8');
  } catch (_) { /* 日志不可写不属于看护的职责范围 */ }
  try {
    BrowserWindow.getAllWindows().forEach((w) => {
      if (!w.isDestroyed()) w.webContents.send('backend:watchdog', line);
    });
  } catch (_) { /* 窗口可能尚未创建 */ }
  writeWatchdogState();
}

/** 把看护状态落成 JSON，供诊断接口与界面读取（后端重启后仍可读到历史次数）。 */
function writeWatchdogState() {
  try {
    fs.mkdirSync(path.dirname(WATCHDOG_STATE), { recursive: true });
    fs.writeFileSync(WATCHDOG_STATE, JSON.stringify({
      updated_at: new Date().toISOString(),
      healthy: backendHealthy,
      consecutive_failures: watchdogFails,
      auto_restarts: watchdogAttempts,
      backend_owned: ownBackend,
      backend_pid: backend && backend.pid ? backend.pid : null,
    }, null, 2), 'utf8');
  } catch (_) { /* 同上 */ }
}

/**
 * 安排一次自动拉起（带退避）。
 * 连续失败时逐次拉长间隔（3s→6s→…上限 60s），成功恢复后从 3s 重新起算，
 * 避免「拉不起来就死循环空转」把机器拖垮。
 */
function scheduleRespawn() {
  if (shuttingDown || !ownBackend || respawnPending) return;
  respawnPending = true;
  watchdogAttempts += 1;
  const delay = Math.min(RESPAWN_BASE_DELAY_MS * watchdogAttempts, RESPAWN_MAX_DELAY_MS);
  watchdogLog(`第 ${watchdogAttempts} 次自动拉起将在 ${Math.round(delay / 1000)} 秒后执行`);
  setTimeout(async () => {
    respawnPending = false;
    if (shuttingDown) return;
    // 端口可能仍被上一进程短暂占用（Windows TIME_WAIT）；也可能外部已把后端
    // 拉起来了。先探测一次，能连上就复用，不做无谓的重复拉起。
    if (await probeServer()) {
      watchdogLog('拉起前探测到后端已在线，跳过本次拉起');
      backendHealthy = true;
      watchdogFails = 0;
      watchdogAttempts = 0;
      return;
    }
    spawnOnce();
    try {
      await waitForServer(45000);
      const n = watchdogAttempts;
      backendHealthy = true;
      watchdogFails = 0;
      watchdogAttempts = 0;
      watchdogLog(`后端已自动拉起并恢复服务（本次为第 ${n} 次拉起）`);
    } catch (_) {
      watchdogLog('自动拉起后仍未就绪，稍后由看护继续重试');
    }
  }, delay);
}

/**
 * 启动长期健康巡检。
 * 除了「进程已退出」（由 exit 事件覆盖），还兜住「进程还在但服务无响应」的僵死情形。
 */
function startWatchdog() {
  if (watchdogTimer) return;
  watchdogTimer = setInterval(async () => {
    if (shuttingDown || !ownBackend || booting) return;
    if (await probeServer()) {
      if (!backendHealthy) {
        backendHealthy = true;
        watchdogLog('后端健康探测已恢复正常');
      }
      watchdogFails = 0;
      watchdogAttempts = 0;
      writeWatchdogState();
      return;
    }
    watchdogFails += 1;
    if (!backendHealthy) return; // 已在拉起流程中，避免重复告警
    if (watchdogFails >= WATCHDOG_FAIL_LIMIT) {
      backendHealthy = false;
      watchdogLog(
        `连续 ${watchdogFails} 次健康探测失败（约 ${Math.round(watchdogFails * WATCHDOG_INTERVAL_MS / 1000)} 秒无响应），判定后端已失效`
      );
      scheduleRespawn();
    } else {
      watchdogLog(`健康探测失败（${watchdogFails}/${WATCHDOG_FAIL_LIMIT}）`);
    }
  }, WATCHDOG_INTERVAL_MS);
  watchdogLog('后端看护已启动：每 10 秒探测一次，连续 3 次失败即自动拉起');
}

function stopWatchdog() {
  if (watchdogTimer) {
    clearInterval(watchdogTimer);
    watchdogTimer = null;
  }
}

/**
 * 快速探测后端是否已经在外运行（处理"上一次没关干净、端口被占"的情况）。
 * 返回 true 表示已有实例在 8765 上响应，本次应直接复用、不再自起后端。
 */
function probeServer() {
  return new Promise((resolve) => {
    const req = http.get(`${URL}/api/status`, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1500, () => { req.destroy(); resolve(false); });
  });
}

/**
 * 轮询等待后端就绪。
 * 窗口启动比服务快，直接打开会白屏；等 200 再开，用户看到的就是有数据的界面。
 */
function waitForServer(timeoutMs) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs;

    const tryOnce = () => {
      const req = http.get(`${URL}/api/status`, (res) => {
        res.resume(); // 丢弃响应体，只关心状态码
        if (res.statusCode === 200) {
          resolve();
        } else {
          retry();
        }
      });
      req.on('error', retry);
      req.setTimeout(1000, () => { req.destroy(); retry(); });

      function retry() {
        if (Date.now() > deadline) {
          reject(new Error('后端在超时时间内未就绪'));
        } else {
          // 启动优化：轮询间隔 400ms → 120ms。
          // 后端就绪的「最后 400ms」是白等的——这段时间窗口一直停在启动页。
          // 探测的是本机 127.0.0.1 的 /api/status，开销可忽略，加密轮询纯赚。
          setTimeout(tryOnce, 120);
        }
      }
    };

    tryOnce();
  });
}

function showError(message) {
  const { BrowserWindow: BW } = require('electron');
  const html =
    `<!doctype html><meta charset="utf-8">` +
    `<body style="font-family:system-ui;padding:24px;color:#333">` +
    `<h2 style="color:#c0392b">启动失败</h2>` +
    `<p>${message}</p>` +
    `<p style="color:#888;font-size:13px">请确认随包 runtime/python 已存在、依赖齐全，再重试（开发环境直接跑 run.bat 选 [10] 启动后端）。</p>` +
    `</body>`;
  const win = new BW({ width: 520, height: 280, title: 'fund-sandbox 启动失败', resizable: false });
  win.loadURL('data:text/html,' + encodeURIComponent(html));
}

function createLoadingWindow() {
  const win = new BrowserWindow({
    width: 440, height: 240, title: 'fund-sandbox',
    frame: false, resizable: false, center: true,
    webPreferences: { sandbox: true }
  });
  const html =
    '<!doctype html><meta charset="utf-8">' +
    '<body style="font-family:system-ui;margin:0;height:100vh;display:flex;flex-direction:column;' +
    'align-items:center;justify-content:center;background:#0f172a;color:#e2e8f0">' +
    '<div style="font-size:16px;font-weight:600;margin-bottom:8px">fund-sandbox 基金沙盒</div>' +
    '<div style="font-size:13px;color:#94a3b8">正在启动后端服务，约需 5–15 秒，请稍候…</div>' +
    '<div style="margin-top:20px;width:180px;height:4px;background:#1e293b;border-radius:2px;overflow:hidden">' +
    '<div style="width:40%;height:100%;background:#3b82f6;animation:sp 1.1s ease-in-out infinite"></div></div>' +
    '<style>@keyframes sp{0%{margin-left:-40%}100%{margin-left:100%}}</style>' +
    '</body>';
  win.loadURL('data:text/html,' + encodeURIComponent(html));
  return win;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 1000,
    minHeight: 660,
    title: 'fund-sandbox 基金模拟交易沙盒',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, 'preload.js')
    }
  });

  // 去掉默认菜单栏，更像原生桌面软件（macOS 保留系统菜单更自然，这里仅 Win 去菜单）
  if (process.platform === 'win32') {
    Menu.setApplicationMenu(null);
  }

  // 所有外链（新闻「阅读全文」等 target="_blank"/window.open）用系统浏览器打开，
  // 禁止在应用内弹新窗或跳转，避免空白/登录 fund-sandbox 窗口。
  const handleExternal = (url) => {
    if (url && url.startsWith('http')) {
      console.log('[main] open external:', url);
      shell.openExternal(url).catch((err) => console.error('[main] openExternal failed:', err));
      return true;
    }
    return false;
  };
  // 新版 Electron 推荐 setWindowOpenHandler
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (handleExternal(url)) return { action: 'deny' };
    return { action: 'allow' };
  });
  // 旧版/某些 target="_blank" 场景兜底用 new-window
  win.webContents.on('new-window', (e, url) => {
    e.preventDefault();
    handleExternal(url);
  });
  // 当前窗口被外链导航时拦截
  win.webContents.on('will-navigate', (e, url) => {
    if (url && !url.startsWith(URL)) {
      e.preventDefault();
      handleExternal(url);
    }
  });

  // 每次启动带时间戳加载，绕过 Chromium 磁盘缓存，保证看到的是最新前端
  win.loadURL(URL + '/?t=' + Date.now());
  win.once('ready-to-show', () => win.show());

  // 开发调试：F12 开 DevTools（刷新走界面上的「刷新」按钮，不设键盘快捷键）
  win.webContents.on('before-input-event', (e, input) => {
    if (input.key === 'F12') { win.webContents.openDevTools(); e.preventDefault(); }
  });

  // 关窗即退出整个应用；仅当后端是本次拉起的才清理（复用的不杀）
  win.on('closed', cleanupBackend);
}

function cleanupBackend() {
  // 必须先标记「应用正在退出」：否则 kill 子进程会触发 exit 事件，
  // 看护以为后端崩了又把它拉起来——就成了关不掉的软件。
  shuttingDown = true;
  stopWatchdog();
  if (ownBackend && backend) {
    try { backend.kill(); } catch (_) {}
    backend = null;
  }
}

/**
 * 找出占用指定端口的进程 PID 集合。
 * 优先用 PowerShell 的 Get-NetTCPConnection（只查单端口，比 netstat 全表扫描快得多；
 * Windows 上 netstat 要 1~3 秒，正是「点重启后卡顿」的元凶）；PowerShell 不可用则回退 netstat。
 */
function findPidsOnPort(port) {
  try {
    const out = execSync(
      `powershell -NoProfile -NonInteractive -Command "Get-NetTCPConnection -LocalPort ${port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"`,
      { windowsHide: true }
    ).toString();
    const pids = new Set();
    out.split(/\r?\n/).forEach((l) => {
      const p = l.trim();
      if (p && /^\d+$/.test(p)) pids.add(p);
    });
    if (pids.size) return pids;
  } catch (_) { /* PowerShell 不可用，走 netstat 兜底 */ }

  // 兜底：netstat 全表扫描（Windows 上较慢，1~3 秒）
  try {
    const out = execSync('netstat -ano -p TCP', { windowsHide: true }).toString();
    const needle = `:${port}`;
    const pids = new Set();
    out.split(/\r?\n/).forEach((line) => {
      const idx = line.indexOf(needle);
      if (idx === -1) return;
      const after = line[idx + needle.length]; // 防误匹配 :87650 之类其他端口
      if (after && /\d/.test(after)) return;
      const cols = line.trim().split(/\s+/);
      const pid = cols[cols.length - 1];
      if (pid && /^\d+$/.test(pid)) pids.add(pid);
    });
    return pids;
  } catch (_) {
    return new Set();
  }
}

/**
 * 找到并强制结束占用指定端口的进程（Windows）。
 * 用于「重启应用」时，无论后端归谁所有（Electron 自拉 / run.bat [10] 看门狗 /
 * 上次残留），都先把 8765 腾空，确保新进程必然拉起一份带最新代码的后端。
 * 若占用者是 run.bat 的 [10] 看门狗（python 的父进程是 cmd），一并结束其父 cmd，
 * 否则看门狗会立即用旧代码把后端重新拉起来，重启依旧看不到新代码。
 */
function killPortOwner(port) {
  const pids = findPidsOnPort(port);

  if (pids.size === 0) {
    console.log(`[main] 端口 ${port} 当前无占用，无需强制释放`);
    return;
  }

  // 若占用者是 run.bat [10] 看门狗，连带结束其父 cmd，避免旧代码被重新拉起
  for (const pid of Array.from(pids)) {
    try {
      const parent = execSync(
        `wmic process where "ProcessId=${pid}" get ParentProcessId /value`,
        { windowsHide: true }
      ).toString().match(/ParentProcessId=(\d+)/i);
      if (parent) {
        const ppid = parent[1];
        const cmdline = execSync(
          `wmic process where "ProcessId=${ppid}" get CommandLine /value`,
          { windowsHide: true }
        ).toString();
        if (/run\.bat/i.test(cmdline)) {
          console.log(`[main] 端口 ${port} 占用者为 run.bat 看门狗，连带结束父 cmd PID=${ppid}`);
          pids.add(ppid);
        }
      }
    } catch (_) { /* 取不到父进程信息则忽略 */ }
  }

  for (const pid of pids) {
    try {
      console.log(`[main] 强制释放端口 ${port}，结束进程 PID=${pid}`);
      execSync(`taskkill /F /PID ${pid}`, { windowsHide: true });
    } catch (e) {
      console.warn(`[main] taskkill PID=${pid} 失败（可能已退出）: ${e.message}`);
    }
  }
}

app.whenReady().then(async () => {
  // 渲染进程点「刷新」→ 全量重启：杀本次拉起的后端，relaunch 让新进程重新
  // 加载最新的 main.js / preload.js / 后端 Python 代码。旧进程退出时 ownBackend
  // 已置 false，不会误杀复用的外部后端（非本次拉起的）——这里显式清理本次拉起的。
  ipcMain.on('app:relaunch', () => {
    console.log('[main] relaunch requested from renderer');
    // 重启期间同样不能让看护介入，否则刚被结束的进程会被立刻复活，重启白做。
    shuttingDown = true;
    stopWatchdog();
    if (ownBackend && backend) {
      // Electron 自己拉起的后端：直接结束已知子进程即可，跳过慢速端口扫描
      // （netstat 在 Windows 上要 1~3 秒，正是「点重启后卡顿」的主因；已知 PID 不必扫）。
      try { backend.kill(); } catch (_) {}
      backend = null;
      ownBackend = false;
    } else {
      // 后端归外部（run.bat [10] 看门狗 / 上次残留）：才需做端口扫描强制腾空 8765，
      // 并连带结束看门狗父 cmd，避免旧代码被重新拉起。
      killPortOwner(PORT);
    }
    // Windows 上被硬杀的 uvicorn 可能短暂占用 8765（TIME_WAIT），稍等再重启可自愈，
    // 避免新实例 spawn 撞 10048。
    setTimeout(() => {
      app.relaunch();
      app.exit(0);
    }, 800);
  });

  const loading = createLoadingWindow();
  const alreadyUp = await probeServer();
  if (!alreadyUp) {
    startBackend();
    ownBackend = true;
    try {
      // 实测冷启动约 5 秒；留 60 秒余量防偶发卡顿，超时即报错而非干等
      await waitForServer(60000);
      stopBooting();
    } catch (err) {
      console.error(err);
      stopBooting();
      try { loading.close(); } catch (_) {}
      showError('Python 后端未能启动（端口 ' + PORT + ' 可能被占用、或 runtime/python 依赖缺失）。可先用 run.bat 选 [10] 启动后端，再双击本程序复用。');
      setTimeout(() => { cleanupBackend(); app.quit(); }, 8000);
      return;
    }
  } else {
    console.log('[main] 检测到后端已在运行，直接复用，不再自起。');
  }
  try { loading.close(); } catch (_) {}
  createWindow();

  // 打开看护：此后后端若在运行中崩溃 / 僵死，应用会自动把它拉起来并留下记录，
  // 不再需要用户「重开一下应用」这种开发者动作。
  startWatchdog();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// 任何退出路径都先关掉看护，避免退出过程中子进程被反复拉起来。
app.on('before-quit', () => {
  shuttingDown = true;
  stopWatchdog();
});

// 所有窗口关闭后清理后端并退出（macOS 习惯保留进程，这里统一退出更干脆）
app.on('window-all-closed', () => {
  cleanupBackend();
  if (process.platform !== 'darwin') app.quit();
});
