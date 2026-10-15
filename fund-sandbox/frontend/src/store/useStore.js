import { create } from 'zustand'
import { apiGet, apiPost, apiPut, apiDelete } from './api'

// 资源登记表：每个 store slice 对应一个 GET 端点与可选查询参数。
// 这是「旧 23 个手动 loadXxx → 统一声明式取数」的核心。
const RESOURCES = {
  status: { url: '/api/status' },
  account: { url: '/api/account' },
  positions: { url: '/api/positions' },
  orders: { url: '/api/orders', params: { limit: 100 } },
  dingtou: { url: '/api/dingtou/plans' },
  boards: { url: '/api/boards', params: { limit: 6 } },
  exposure: { url: '/api/exposure' },
  analysis: { url: '/api/analysis' },
  portfolioCurve: { url: '/api/portfolio/curve', params: { limit: 365 } },
  news: { url: '/api/news', params: { limit: 60 } },
  marketNews: { url: '/api/market-news', params: { limit: 50 } },
  intel: { url: '/api/intel', params: { limit: 100 } },
  // 健康页渲染的是「数据源抓取状态」字段（source/data_type/record_count），
  // 数据源在 /api/data-health（/api/health 返回持仓健康度，无 items 字段）。
  health: { url: '/api/data-health' },
  // 持仓六维健康度（趋势/持续性/相对强度/回撤/估值/舆情 + 四档处置）
  positionHealth: { url: '/api/health' },
  funds: { url: '/api/funds' },
  dividends: { url: '/api/dividends', params: { limit: 50 } },
  settings: { url: '/api/settings' },
}

// 自动刷新轮询覆盖的资源（高频、对决策敏感的）。
const AUTO_KEYS = [
  'status',
  'account',
  'positions',
  'orders',
  'dingtou',
  'boards',
  'analysis',
  'funds',
  'marketNews',
  'news',
  // 情报库由后端后台线程自动采集（rss_news 每 30 分钟一轮，抓完自动调 intel_collector
  // 提取事件）。这里把 intel 纳入轮询，页面才会自动显示新采集到的情报，不必手动点「采集」。
  'intel',
  'portfolioCurve',
  // 数据源状态是轻量只读，进轮询开销可忽略。
  'health',
]

const initialResourceState = () => {
  const s = {}
  for (const k of Object.keys(RESOURCES)) {
    s[k] = { data: null, loading: false, error: null, lastUpdated: null }
  }
  return s
}

let timerId = null

export const useStore = create((set, get) => {
  // 1) 为每个资源生成 fetch_<key> 动作
  const actions = {}
  for (const key of Object.keys(RESOURCES)) {
    actions['fetch_' + key] = async () => {
      const cfg = RESOURCES[key]
      set((st) => ({ [key]: { ...st[key], loading: true, error: null } }))
      try {
        const data = await apiGet(cfg.url, cfg.params)
        set((st) => ({
          [key]: { ...st[key], data, loading: false, lastUpdated: Date.now() },
        }))
        return data
      } catch (e) {
        set((st) => ({ [key]: { ...st[key], loading: false, error: e.message } }))
        return null
      }
    }
  }

  // 2) 写操作：调用对应 POST/PUT/DELETE 后，顺手刷新相关资源
  actions.placeOrder = async (payload) => {
    const r = await apiPost('/api/order', payload)
    await Promise.all([get().fetch_orders(), get().fetch_account(), get().fetch_positions()])
    return r
  }
  actions.cancelOrder = async (id) => {
    const r = await apiPost(`/api/orders/${id}/cancel`)
    await get().fetch_orders()
    return r
  }
  actions.deleteOrder = async (id) => {
    const r = await apiDelete(`/api/orders/${id}`)
    await get().fetch_orders()
    return r
  }
  actions.addDingtouPlan = async (payload) => {
    const r = await apiPost('/api/dingtou/plans', payload)
    await get().fetch_dingtou()
    return r
  }
  actions.updateDingtouPlan = async (id, payload) => {
    const r = await apiPut(`/api/dingtou/plans/${id}`, payload)
    await get().fetch_dingtou()
    return r
  }
  actions.deleteDingtouPlan = async (id) => {
    const r = await apiDelete(`/api/dingtou/plans/${id}`)
    await get().fetch_dingtou()
    return r
  }
  actions.runDingtou = async () => {
    const r = await apiPost('/api/dingtou/run')
    await Promise.all([
      get().fetch_dingtou(),
      get().fetch_orders(),
      get().fetch_account(),
      get().fetch_positions(),
    ])
    return r
  }
  actions.addIntel = async (payload) => {
    const r = await apiPost('/api/intel', payload)
    await get().fetch_intel()
    return r
  }
  actions.collectIntel = async () => {
    const r = await apiPost('/api/intel/collect')
    await get().fetch_intel()
    return r
  }
  actions.refreshNews = async () => {
    const r = await apiPost('/api/news/refresh')
    await get().fetch_news()
    return r
  }
  actions.refreshMarketNews = async () => {
    const r = await apiPost('/api/market-news/refresh')
    await get().fetch_marketNews()
    return r
  }
  actions.refreshFunds = async () => {
    const r = await apiPost('/api/funds/refresh')
    await get().fetch_funds()
    return r
  }
  // 原实现调 `POST /api/funds` 发 `{fund_code, fetch_nav}` —— **契约完全不匹配**：
  // 后端是 `POST /api/funds/add`，字段名是 `code`，且 `reason` **必填**
  // （业务规则：添加候选必须说明"为什么添加"）。原调用必然 404/422，添加基金从来没成功过。
  actions.addFund = async (code, reason) => {
    const r = await apiPost('/api/funds/add', { code, reason })
    await get().fetch_funds()
    return r
  }
  actions.removeFund = async (code) => {
    const r = await apiDelete(`/api/funds/${code}`)
    await get().fetch_funds()
    return r
  }
  actions.searchFunds = async (q, limit = 20) => apiGet('/api/funds/search', { q, limit })
  actions.takeSnapshot = async () => {
    const r = await apiPost('/api/portfolio/snapshot')
    await get().fetch_portfolioCurve()
    return r
  }
  actions.setEmergencyStop = async (stop, note = '') => {
    const r = await apiPost('/api/emergency-stop', { stop, note })
    await get().fetch_status()
    return r
  }
  // 打开外链（资讯「阅读全文」等）：
  //   桌面端 → **应用内轻量浏览器**（Electron 主进程新开 BrowserWindow，Chromium 完整渲染，
  //   东财/36氪/中新网的 X-Frame-Options 拦不住它，所以在软件内就能读全文）；
  //   浏览器调试（无 electronAPI）→ 退回 window.open。
  // ⚠️ 老实现调 `apiPost('/api/open-external')`，而后端只有 GET 路由 → **405**，
  // 点「阅读全文」只会弹「打开失败」。这是本段重写的原因。
  actions.openExternal = async (url) => {
    const link = String(url || '')
    if (!/^https?:\/\//i.test(link)) return { ok: false, reason: 'invalid-scheme' }
    const api = typeof window !== 'undefined' ? window.electronAPI : undefined
    if (api?.openInAppBrowser) {
      const opened = await api.openInAppBrowser(link)
      return opened ? { ok: true, mode: 'in-app' } : { ok: false, reason: 'rejected' }
    }
    window.open(link, '_blank', 'noopener,noreferrer')
    return { ok: true, mode: 'window.open' }
  }
  // 保存设置：后端写回 config.toml 并热生效；顺带刷新账户（资金规则展示随之更新）
  actions.updateSettings = async (values) => {
    const r = await apiPut('/api/settings', { values })
    await Promise.all([get().fetch_settings(), get().fetch_account()])
    return r
  }

  // 3) 聚合 + 自动刷新
  actions.refreshAll = async () => {
    await Promise.all(AUTO_KEYS.map((k) => get()['fetch_' + k]()))
  }
  actions.startAutoRefresh = (ms = 15000) => {
    if (timerId) return
    timerId = setInterval(() => {
      get().refreshAll()
    }, ms)
  }
  actions.stopAutoRefresh = () => {
    if (timerId) {
      clearInterval(timerId)
      timerId = null
    }
  }

  // 4) 轻量 toast（操作反馈，避免弹窗轰炸）
  actions.notify = (message, type = 'info') => {
    const ts = Date.now()
    set({ toast: { message, type, ts } })
    setTimeout(() => {
      const t = get().toast
      if (t && t.ts === ts) set({ toast: null })
    }, 3200)
  }
  actions.clearToast = () => set({ toast: null })

  return {
    ...initialResourceState(),
    ...actions,
    toast: null,
  }
})
