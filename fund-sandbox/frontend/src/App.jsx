import { Component, useEffect, useState } from 'react'
import { useStore } from './store/useStore'
import { Icon } from './components/Icons'
import Toast from './components/Toast'
import ConfirmDialog from './components/ConfirmDialog'
import Overview from './views/Overview'
import Positions from './views/Positions'
import Trade from './views/Trade'
import News from './views/News'
import Analysis from './views/Analysis'
import Intel from './views/Intel'
import Health from './views/Health'
import RealPositions from './views/RealPositions'
import Settings from './views/Settings'
import './styles/tokens.css'
import './styles/app.css'

class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    console.error('[ErrorBoundary] 页面出错了：', error, info?.componentStack || '')
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="page-error" role="alert">
        <strong>这个页面暂时无法显示</strong>
        <p>{String(this.state.error?.message || this.state.error || '未知错误').slice(0, 200)}</p>
        <button className="btn btn-primary" onClick={() => window.location.reload()}>重新加载</button>
      </div>
    )
  }
}

const NAV_GROUPS = [
  // 独立账户（人工接管的真实仓位）放最前：它是本应用里「人管的那一半」，
  // 优先级高于 AI 侧的沙盒与投研，故置于「工作台」之前。
  { title: '独立账户', items: [
    { key: 'real', label: '真实仓位', icon: Icon.wallet, sub: '人工管理 · AI 只读' },
  ] },
  { title: '工作台', items: [
    { key: 'overview', label: '决策总览', icon: Icon.overview, sub: '组合状态与待办' },
  ] },
  { title: '投研流程', items: [
    { key: 'news', label: '市场资讯', icon: Icon.news, sub: '新闻与板块动态' },
    { key: 'intel', label: '事件情报', icon: Icon.intel, sub: '事件、方向与落地' },
    { key: 'analysis', label: '基金分析', icon: Icon.analysis, sub: '筛选与持仓诊断' },
  ] },
  { title: '模拟账户', items: [
    { key: 'positions', label: '模拟持仓', icon: Icon.positions, sub: '批次、盈亏与定投' },
    { key: 'trade', label: '模拟交易', icon: Icon.trade, sub: '候选池与订单记录' },
  ] },
  { title: '系统', items: [
    { key: 'health', label: '数据健康', icon: Icon.health, sub: '来源、更新与异常' },
    { key: 'settings', label: '资金与规则', icon: Icon.settings, sub: '预算、费率与边界' },
  ] },
]

const VIEWS = {
  overview: Overview,
  positions: Positions,
  trade: Trade,
  news: News,
  analysis: Analysis,
  intel: Intel,
  health: Health,
  real: RealPositions,
  settings: Settings,
}

function StatusPill() {
  const status = useStore((s) => s.status.data)
  if (!status) return <div className="status-pill"><span className="led" />连接中</div>
  const online = status.ai_online
  return (
    <div className={`status-pill ${online ? 'online' : 'offline'}`}>
      <span className="led" />{online ? 'AI 在线' : 'AI 离线'}
    </div>
  )
}

function StopPill() {
  const status = useStore((s) => s.status.data)
  const setEmergencyStop = useStore((s) => s.setEmergencyStop)
  const notify = useStore((s) => s.notify)
  const [confirm, setConfirm] = useState(false)
  const stopped = status?.emergency_stop

  const onToggle = async () => {
    try {
      await setEmergencyStop(!stopped, stopped ? '恢复自动交易' : '界面紧急暂停')
      notify(stopped ? '已恢复自动操作' : '已暂停自动操作', stopped ? 'success' : 'error')
      setConfirm(false)
    } catch (e) {
      notify('操作失败：' + e.message, 'error')
      throw e
    }
  }

  return (
    <>
      <button
        className={`status-pill as-btn ${stopped ? 'stop' : 'online'}`}
        disabled={!status}
        onClick={() => setConfirm(true)}
        title={stopped ? '恢复定投等预先设定的自动操作' : '暂停定投等预先设定的自动操作'}
      >
        <span className="led" />{stopped ? '自动操作已暂停' : '自动操作可执行'}
      </button>
      <ConfirmDialog
        open={confirm}
        danger={!stopped}
        title={stopped ? '恢复自动操作' : '紧急暂停自动操作'}
        message={stopped
          ? '将恢复定投等预先设定的自动操作。交易决策仍由接管 AI 执行。'
          : '将暂停定投等自动操作。已持仓不受影响；交易决策不会由内置规则或模型接管。'}
        confirmText={stopped ? '恢复自动操作' : '确认暂停'}
        onConfirm={onToggle}
        onClose={() => setConfirm(false)}
      />
    </>
  )
}

// 打开软件 / 重启时的默认页面：**真实持仓**。
// 它是人工接管的那一半，优先级最高 —— 与侧栏把「独立账户」置于最前保持一致。
const DEFAULT_VIEW = 'real'

function readInitialView() {
  const view = new URLSearchParams(window.location.search).get('view')
  return view && VIEWS[view] ? view : DEFAULT_VIEW
}

export default function App() {
  const [view, setView] = useState(readInitialView)
  const [relaunchConfirm, setRelaunchConfirm] = useState(false)
  const refreshAll = useStore((s) => s.refreshAll)
  const startAutoRefresh = useStore((s) => s.startAutoRefresh)
  const stopAutoRefresh = useStore((s) => s.stopAutoRefresh)

  useEffect(() => {
    refreshAll()
    startAutoRefresh(15000)
    return () => stopAutoRefresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const go = (key) => {
    if (!VIEWS[key]) return
    setView(key)
    const query = key === DEFAULT_VIEW ? window.location.pathname : `?view=${key}`
    window.history.replaceState(null, '', query)
  }

  const Active = VIEWS[view] || Overview
  const navItem = NAV_GROUPS.flatMap((group) => group.items).find((item) => item.key === view)
  const navGroup = NAV_GROUPS.find((group) => group.items.some((item) => item.key === view))

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">S</span>
          <div>Steady Fund<small>基金投研工作台</small></div>
        </div>
        <nav className="nav" aria-label="主导航">
          {NAV_GROUPS.map((group) => (
            <section className="nav-group" key={group.title} aria-label={group.title}>
              <div className="nav-heading">{group.title}</div>
              {group.items.map((item) => {
                const ItemIcon = item.icon
                return (
                  <button
                    key={item.key}
                    className={`nav-btn ${view === item.key ? 'active' : ''} ${item.key === 'real' ? 'nav-real' : ''}`}
                    aria-current={view === item.key ? 'page' : undefined}
                    onClick={() => go(item.key)}
                  >
                    <ItemIcon className="ico" />
                    <span className="nav-copy"><span>{item.label}</span><small>{item.sub}</small></span>
                  </button>
                )
              })}
            </section>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-footer-title"><span className="sidebar-live-dot" />虚拟资金 · 真实规则</div>
          <p>真实持仓独立存储，AI 只读分析</p>
          <span>LOCAL RESEARCH ENVIRONMENT</span>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="topbar-title">
            <div className="topbar-crumb">STEADY FUND <span>/</span> {navGroup?.title}</div>
            <h1>{navItem?.label || '决策总览'}</h1>
            <div className="sub">{navItem?.sub || '组合状态与待办'}</div>
          </div>
          <div className="topbar-actions">
            <StatusPill />
            <StopPill />
            <button className="btn btn-sm btn-relaunch" onClick={() => setRelaunchConfirm(true)} title="重启应用以加载最新代码与数据">
              <Icon.refresh className="ico" />重启应用
            </button>
            <ConfirmDialog
              open={relaunchConfirm}
              danger
              title="重启应用"
              message="将重启整个应用以加载最新代码与数据，未保存的临时状态会丢失。确定继续？"
              confirmText="重启"
              onConfirm={() => {
                if (window.electronAPI?.relaunchApp) window.electronAPI.relaunchApp()
                else window.location.href = window.location.pathname + '?t=' + Date.now()
              }}
              onClose={() => setRelaunchConfirm(false)}
            />
          </div>
        </header>

        <div className="page">
          <ErrorBoundary><Active onNavigate={go} /></ErrorBoundary>
        </div>
      </main>
      <Toast />
    </div>
  )
}
