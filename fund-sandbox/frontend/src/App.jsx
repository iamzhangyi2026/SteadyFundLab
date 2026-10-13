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
import Settings from './views/Settings'
import './styles/tokens.css'
import './styles/app.css'

// ⚠️ ErrorBoundary 是崩溃兜底组件。
// 没有它，**任何一个组件渲染抛错都会让整页白屏**（React 会卸载整棵树），用户看不到任何提示，
// 只能以为"软件坏了"—— 这与「软件崩溃必须尽快修复」直接冲突。
// 只包视图区域（`<Active />`），侧栏与顶栏仍可用，用户还能切换视图自救。
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
      <div style={{ padding: '48px 24px', textAlign: 'center' }}>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 8 }}>页面出错了</div>
        <div className="muted" style={{ marginBottom: 18, fontSize: 13 }}>
          {String(this.state.error?.message || this.state.error || '未知错误').slice(0, 200)}
        </div>
        <button
          className="btn btn-primary"
          onClick={() => {
            this.setState({ hasError: false, error: null })
            window.location.reload()
          }}
        >
          重新加载
        </button>
      </div>
    )
  }
}

const NAV = [
  { key: 'overview', label: '总览', icon: Icon.overview },
  { key: 'positions', label: '持仓', icon: Icon.positions },
  { key: 'trade', label: '交易', icon: Icon.trade },
  { key: 'news', label: '资讯', icon: Icon.news },
  { key: 'analysis', label: '分析', icon: Icon.analysis },
  { key: 'intel', label: '情报', icon: Icon.intel },
  { key: 'health', label: '健康', icon: Icon.health },
  { key: 'settings', label: '设置', icon: Icon.settings },
]

const VIEWS = {
  overview: Overview,
  positions: Positions,
  trade: Trade,
  news: News,
  analysis: Analysis,
  intel: Intel,
  health: Health,
  settings: Settings,
}

// 顶栏胶囊一：AI 是否在线 + 当前生效模式（急停状态交给 StopPill，避免重复）
function StatusPill() {
  const status = useStore((s) => s.status.data)
  if (!status) return <div className="status-pill"><span className="led" />连接中…</div>
  const online = status.ai_online
  return (
    <div className={`status-pill ${online ? 'online' : 'offline'}`}>
      <span className="led" />
      {online ? <Icon.check className="ico" style={{ width: 14, height: 14 }} /> : <Icon.warn className="ico" style={{ width: 14, height: 14 }} />}
      {online ? 'AI 在线' : 'AI 离线'} · {status.effective_mode === 'auto' ? '自动' : '手动'}模式
    </div>
  )
}

// 紧急停止开关（顶栏）。危险操作，必须经 ConfirmDialog 二次确认。
function StopPill() {
  const status = useStore((s) => s.status.data)
  const setEmergencyStop = useStore((s) => s.setEmergencyStop)
  const notify = useStore((s) => s.notify)
  const [confirm, setConfirm] = useState(false)
  const stopped = status?.emergency_stop

  const onToggle = async () => {
    try {
      await setEmergencyStop(!stopped, stopped ? '恢复自动交易' : '界面紧急暂停')
      notify(stopped ? '已恢复自动交易' : '已紧急暂停所有自动交易', stopped ? 'success' : 'error')
    } catch (e) {
      notify('操作失败：' + e.message, 'error')
    }
  }

  return (
    <>
      <button
        className={`status-pill as-btn ${stopped ? 'stop' : 'online'}`}
        onClick={() => setConfirm(true)}
        title={
          stopped
            ? '点击恢复自动交易'
            : '点击紧急暂停所有自动交易（定投 / 规则引擎自动减仓）'
        }
      >
        <span className="led" />
        {stopped ? '已紧急停止' : '运行中'}
      </button>
      <ConfirmDialog
        open={confirm}
        danger={!stopped}
        title={stopped ? '恢复自动交易' : '紧急暂停所有自动交易'}
        message={
          stopped
            ? '将恢复定投与规则引擎的自动交易。'
            : '将立即暂停全部自动交易（定投 / 规则引擎自动减仓）。已持仓不受影响，但不再自动操作。'
        }
        confirmText={stopped ? '恢复' : '确认暂停'}
        onConfirm={onToggle}
        onClose={() => setConfirm(false)}
      />
    </>
  )
}

// 初始视图读自 URL（?view=），旧版支持的深链；无效值回退总览。
function readInitialView() {
  const v = new URLSearchParams(window.location.search).get('view')
  return v && VIEWS[v] ? v : 'overview'
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

  const Active = VIEWS[view] || Overview
  const navItem = NAV.find((n) => n.key === view)

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="dot" />
          <div>
            沙盒基金
            <small>STEADY FUND LAB</small>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((n) => {
            const I = n.icon
            return (
              <button
                key={n.key}
                className={`nav-btn ${view === n.key ? 'active' : ''}`}
                onClick={() => {
                  setView(n.key)
                  // 同步 URL，便于深链 / 刷新保持当前视图（旧版 ?view= 行为）
                  const q = n.key === 'overview' ? window.location.pathname : '?view=' + n.key
                  window.history.replaceState(null, '', q)
                }}
              >
                <I className="ico" />
                {n.label}
              </button>
            )
          })}
          <div className="spacer" />
        </nav>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <h1>{navItem?.label || '总览'}</h1>
            <div className="sub">虚拟资金 · 真实行情 · 纪律定投</div>
          </div>
          <div className="topbar-actions">
            <StatusPill />
            <StopPill />
            <button
              className="btn btn-sm btn-danger"
              onClick={() => setRelaunchConfirm(true)}
              title="重启整个应用以加载最新代码与数据"
            >
              <Icon.refresh className="ico" />
              重启应用
            </button>
            <ConfirmDialog
              open={relaunchConfirm}
              danger
              title="重启应用"
              message="将重启整个应用以加载最新代码与数据，未保存的临时状态会丢失。确定继续？"
              confirmText="重启"
              onConfirm={() => {
                if (window.electronAPI && window.electronAPI.relaunchApp) {
                  window.electronAPI.relaunchApp()
                } else {
                  window.location.href = window.location.pathname + '?t=' + Date.now()
                }
              }}
              onClose={() => setRelaunchConfirm(false)}
            />
          </div>
        </header>

        <div className="page">
          <ErrorBoundary>
            <Active />
          </ErrorBoundary>
        </div>
      </main>

      <Toast />
    </div>
  )
}
