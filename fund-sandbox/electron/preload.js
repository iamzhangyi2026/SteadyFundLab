'use strict';

// 预加载脚本：在渲染进程（前端）与 Electron 主进程之间架一座最小可信桥。
// 只暴露两件事——① 用系统默认浏览器打开外链；② 请求主进程全量重启（刷新用）。
// 即便 Electron 处于 sandbox 模式，contextBridge + shell + ipcRenderer 也是被允许的合法组合。
const { contextBridge, ipcRenderer, shell } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  openExternal(url) {
    if (url && /^https?:\/\//i.test(String(url))) {
      shell.openExternal(url).catch((err) => console.error('[preload] openExternal failed:', err));
    }
  },
  // 在**应用内**轻量浏览器窗口打开外链（资讯「阅读全文」）。
  // 主进程用独立 BrowserWindow 承载 Chromium 渲染，X-Frame-Options 拦不住它；
  // 返回 Promise<boolean>：false 表示协议不合法（非 http/https）被主进程拒了。
  openInAppBrowser(url) {
    if (!url || !/^https?:\/\//i.test(String(url))) {
      return Promise.resolve(false);
    }
    return ipcRenderer.invoke('app:open-inapp', String(url))
      .catch((err) => { console.error('[preload] openInAppBrowser failed:', err); return false; });
  },
  // 请求主进程 relaunch：重新加载最新的 main.js / preload.js / 后端代码。
  // 前端「刷新」按钮在 Electron 下调用它，实现「点一下 = 完整重启应用」。
  relaunchApp() {
    ipcRenderer.send('app:relaunch');
  },
  // 订阅后端看护事件（崩溃 / 自动拉起 / 恢复），供界面如实提示用户。
  // 只在 Electron 桌面端存在；浏览器里 window.electronAPI 为 undefined，调用方需自行判空。
  onBackendWatchdog(callback) {
    if (typeof callback !== 'function') return;
    ipcRenderer.on('backend:watchdog', (_e, line) => callback(line));
  }
});
