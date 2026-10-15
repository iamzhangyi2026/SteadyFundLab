import { useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import StatTile from '../components/StatTile'
import NetValueChart from '../components/NetValueChart'
import Skeleton from '../components/Skeleton'
import EmptyState from '../components/EmptyState'
import { Icon } from '../components/Icons'
import ExportButton from '../components/ExportButton'
import DonutChart from '../components/DonutChart'
import AsyncButton from '../components/AsyncButton'
import Modal from '../components/Modal'
import { apiGet } from '../store/api'
import { money, pct, date, time, tone } from '../lib/format'

const FLOW = [
  { key: 'news', number: '01', title: '读取市场', detail: '资讯与板块变化', icon: Icon.rss },
  { key: 'intel', number: '02', title: '提取事件', detail: '方向、可信度与落地', icon: Icon.intel },
  { key: 'analysis', number: '03', title: '筛选基金', detail: '分位、趋势与回撤', icon: Icon.analysis },
  { key: 'positions', number: '04', title: '核对纪律', detail: '持仓、批次与定投', icon: Icon.layers },
  { key: 'trade', number: '05', title: '模拟交易', detail: '订单与虚拟资金账本', icon: Icon.trade },
]

// 「全部资讯」弹窗每页条数。库里已有数万条历史资讯，一次全拉会卡死浏览器；
// 按页翻，取不满一页即到底。
const NEWS_PAGE = 100

export default function Overview({ onNavigate = () => {} }) {
  const account = useStore((s) => s.account.data)
  const accountLoading = useStore((s) => s.account.loading)
  const curve = useStore((s) => s.portfolioCurve.data)
  const curveLoading = useStore((s) => s.portfolioCurve.loading)
  const marketNews = useStore((s) => s.marketNews.data)
  const intel = useStore((s) => s.intel.data)
  const positions = useStore((s) => s.positions.data)
  const funds = useStore((s) => s.funds.data)
  const orders = useStore((s) => s.orders.data)
  const openExternal = useStore((s) => s.openExternal)
  const refreshMarketNews = useStore((s) => s.refreshMarketNews)
  const notify = useStore((s) => s.notify)
  const [newsOpen, setNewsOpen] = useState(false)
  // 「全部资讯」弹窗：库里数万条，分页加载（每次 NEWS_PAGE 条，取空即到底）
  const [newsAll, setNewsAll] = useState([])
  const [newsLoading, setNewsLoading] = useState(false)
  const [newsDone, setNewsDone] = useState(false)

  const stats = curve?.stats || {}
  const assets = curve?.items || []
  const marketItems = marketNews?.items || []
  const intelItems = intel?.items || []
  const positionItems = positions?.items || []
  const orderItems = orders?.items || []
  const budget = account?.budget
  const available = budget?.available || 0
  const used = budget?.used_in_positions || 0
  const pending = budget?.pending_committed || 0
  const budgetBase = Math.max(used + pending + available, 1)
  const usedPct = Math.min((used / budgetBase) * 100, 100)
  const pendingPct = Math.min((pending / budgetBase) * 100, 100 - usedPct)
  const freePct = Math.min((available / budgetBase) * 100, 100 - usedPct - pendingPct)
  const budgetRows = budget
    ? [
        { 项目: '初始资金', 金额: budget.initial },
        { 项目: '累积新增', 金额: budget.cumulative_added },
        { 项目: '持仓占用', 金额: budget.used_in_positions },
        { 项目: '待成交冻结', 金额: budget.pending_committed },
        { 项目: '可用余额', 金额: budget.available },
        { 项目: '现金余额', 金额: budget.cash_balance },
        { 项目: '已实现盈亏', 金额: budget.realized_pnl },
        { 项目: '浮动盈亏', 金额: budget.unrealized_pnl },
      ]
    : []

  const holdings = positionItems
    .filter((item) => (item.market_value || 0) > 0)
    .map((item) => ({ name: item.fund_name || item.fund_code, value: item.market_value }))
  const heldLots = positionItems.flatMap((position) =>
    (position.lots || []).map((lot) => ({ ...lot, fund_code: position.fund_code, fund_name: position.fund_name }))
  )
  const penaltyLots = heldLots
    .filter((lot) => lot.confirmed && lot.days_to_penalty_free > 0)
    .sort((a, b) => a.days_to_penalty_free - b.days_to_penalty_free)
  const activeAlerts = account?.risk_alert?.alerts || []
  const pendingOrders = orderItems.filter((order) => order.status === 'pending').length
  const currentDate = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })

  const onRefreshNews = async () => {
    try {
      const result = await refreshMarketNews()
      notify(`抓取完成，新增 ${result?.fetched ?? 0} 条`, 'success')
    } catch (error) {
      notify('抓取失败：' + error.message, 'error')
    }
  }

  const readNews = async (link) => {
    if (!link) return
    try {
      const result = await openExternal(link)
      if (!result?.ok) notify('打开失败', 'error')
    } catch (error) {
      notify('打开失败：' + error.message, 'error')
    }
  }

  // 「全部资讯」弹窗：打开时取第一页，点「加载更多」按 offset 往后翻
  const loadNewsPage = async (offset) => {
    setNewsLoading(true)
    try {
      const data = await apiGet('/api/market-news', { limit: NEWS_PAGE, offset })
      const items = data?.items || []
      setNewsAll((prev) => (offset === 0 ? items : [...prev, ...items]))
      setNewsDone(items.length < NEWS_PAGE)   // 取不满一页 → 已到底
    } catch (error) {
      notify('加载失败：' + error.message, 'error')
      setNewsDone(true)
    } finally {
      setNewsLoading(false)
    }
  }

  const openNewsAll = () => {
    setNewsOpen(true)
    setNewsAll([])
    setNewsDone(false)
    loadNewsPage(0)
  }

  return (
    <div className="stack overview-stack">
      <div className="overview-intro wide">
        <div>
          <div className="overview-eyebrow">账户监控 <span>·</span> PORTFOLIO</div>
          <h2>资产与风险</h2>
          <p>{currentDate} <span>·</span> 虚拟账户数据 <span>·</span> 真实持仓独立管理</p>
        </div>
        <div className="overview-shortcuts">
          <button className="btn btn-primary" onClick={() => onNavigate('analysis')}><Icon.search className="ico" />开始筛选</button>
          <button className="btn btn-light" onClick={() => onNavigate('trade')}><Icon.plus className="ico" />新建交易</button>
        </div>
      </div>

      <section className="overview-hero wide" aria-label="模拟账户总览">
        <div className="overview-hero-main">
          <div className="hero-label"><span className="hero-label-dot" />模拟组合总资产</div>
          {accountLoading && !account ? (
            <Skeleton variant="block" />
          ) : (
            <strong className="hero-total">{money(account?.total_assets)}</strong>
          )}
          <div className="hero-performance">
            <span>累计收益 <b className={tone(stats.latest_cumulative_pnl) ? `tone-${tone(stats.latest_cumulative_pnl)}` : ''}>{money(stats.latest_cumulative_pnl)}</b></span>
            <span className="hero-divider" />
            <span>区间收益 <b className={tone(stats.total_return_pct) ? `tone-${tone(stats.total_return_pct)}` : ''}>{pct(stats.total_return_pct)}</b></span>
            <span className="hero-divider" />
            <span>最大回撤 <b>{stats.max_drawdown_pct == null ? '—' : pct(-Math.abs(stats.max_drawdown_pct))}</b></span>
          </div>
          <div className="hero-caption">只展示模拟账户数据；真实持仓在独立账户中管理。</div>
        </div>
        <div className="overview-hero-budget">
          <div className="hero-budget-head"><span>可用预算</span><Icon.wallet className="ico" /></div>
          <strong>{money(available)}</strong>
          <div className="hero-budget-bar" aria-label={`持仓占用 ${usedPct.toFixed(0)}%，待成交 ${pendingPct.toFixed(0)}%`}>
            <span className="hero-budget-used" style={{ width: `${usedPct}%` }} />
            <span className="hero-budget-pending" style={{ width: `${pendingPct}%` }} />
            <span className="hero-budget-free" style={{ width: `${freePct}%` }} />
          </div>
          <div className="hero-budget-legend">
            <span><i className="used-dot" />持仓 {money(used)}</span>
            <span><i className="pending-dot" />待成交 {money(pending)}</span>
            <span><i className="free-dot" />可用 {money(available)}</span>
          </div>
          <div className="hero-budget-foot">
            <span>现金 {money(account?.cash)}</span>
            <ExportButton rows={budgetRows} fileName="预算明细" sheetName="预算" notify={notify} label="导出" />
          </div>
        </div>
      </section>

      {activeAlerts.length > 0 && (
        <div className="wide overview-alert" role="alert">
          <span className="overview-alert-icon"><Icon.warn className="ico" /></span>
          <div><strong>{activeAlerts.length} 条纪律提醒</strong><p>{activeAlerts[0].msg}</p></div>
          <button className="link-btn" onClick={() => onNavigate('positions')}>查看持仓 <span aria-hidden="true">→</span></button>
        </div>
      )}

      <div className="overview-metrics wide">
        <StatTile label="持仓市值" num={account?.market_value} format={money} sub={`${positionItems.length} 只基金`} />
        <StatTile label="现金余额" num={account?.cash} format={money} sub="账户可即时使用" />
        <StatTile label="持仓批次" value={heldLots.length} sub={`${positionItems.length} 只基金`} />
        <StatTile label="待确认订单" value={pendingOrders} sub="查看模拟交易记录" />
      </div>

      <Card className="wide overview-flow-card" title="投研工作流" kicker="从市场信号到模拟交易">
        <div className="overview-flow">
          {FLOW.map((step, index) => {
            const StepIcon = step.icon
            const count = step.key === 'news' ? marketItems.length
              : step.key === 'intel' ? intelItems.length
                : step.key === 'analysis' ? (funds?.items || []).length
                  : step.key === 'positions' ? positionItems.length
                    : pendingOrders
            const unit = step.key === 'news' ? '条资讯'
              : step.key === 'intel' ? '条事件'
                : step.key === 'analysis' ? '只在基金池'
                  : step.key === 'positions' ? '只持仓'
                    : '笔待确认'
            return (
              <button className="flow-step" key={step.key} onClick={() => onNavigate(step.key)}>
                <span className="flow-step-top"><span className="flow-step-number">{step.number}</span><StepIcon className="ico" /></span>
                <strong>{step.title}</strong>
                <span className="flow-step-detail">{step.detail}</span>
                <span className="flow-step-count">{count}<small>{unit}</small></span>
                {index < FLOW.length - 1 && <span className="flow-step-arrow" aria-hidden="true">→</span>}
              </button>
            )
          })}
        </div>
      </Card>

      <Card className="col-7 overview-chart-card" title="组合资产曲线" kicker="账户走势" icon={<Icon.chartLine className="ico" />}
        right={<span className="muted cell-note">{assets.length ? `${date(assets[0]?.snap_date)} — ${date(assets[assets.length - 1]?.snap_date)} · ${assets.length} 个记录日` : '记录快照后显示'}</span>}>
        {curveLoading && !assets.length
          ? <Skeleton variant="block" />
          : <NetValueChart categories={assets.map((item) => item.snap_date)} values={assets.map((item) => item.total_assets)} height={270} label="模拟组合资产曲线" />}
      </Card>

      <Card className="col-5 overview-allocation-card" title="持仓分布" kicker="按当前市值" icon={<Icon.pie className="ico" />}>
        {holdings.length
          ? <DonutChart data={holdings} height={250} unit="money" label="模拟账户持仓分布" centerValue={holdings.reduce((sum, item) => sum + item.value, 0)} centerLabel="持仓市值" />
          : <EmptyState title="还没有模拟持仓" hint="完成第一笔买入后，这里会显示组合分布。" action={<button className="btn btn-sm btn-primary" onClick={() => onNavigate('trade')}>前往模拟交易</button>} />}
      </Card>

      <Card className="col-7 overview-lots-card" title="批次与赎回费提醒" kicker="持有期纪律" icon={<Icon.layers className="ico" />}
        right={<button className="btn btn-sm btn-light" onClick={() => onNavigate('positions')}>全部持仓 <span aria-hidden="true">→</span></button>}>
        {penaltyLots.length ? (
          <div className="overview-lot-list">
            {penaltyLots.slice(0, 4).map((lot) => (
              <div className="overview-lot-row" key={`${lot.fund_code}-${lot.lot_id}`}>
                <div className="overview-lot-name"><strong>{lot.fund_name || lot.fund_code}</strong><span>{lot.fund_code} · 买入确认 {date(lot.confirm_date)}</span></div>
                <span className="tag warn">还需 {lot.days_to_penalty_free} 天</span>
                <span className="overview-lot-fee">赎回费 {(lot.fee_rate * 100).toFixed(2)}%</span>
              </div>
            ))}
            {penaltyLots.length > 4 && <div className="overview-lot-more">另有 {penaltyLots.length - 4} 个批次仍在惩罚期</div>}
          </div>
        ) : (
          <EmptyState title="当前没有惩罚期批次" hint={heldLots.length ? '现有批次已达到对应的低费率持有期。' : '买入确认后，系统会按批次跟踪持有天数与赎回费。'} />
        )}
        <p className="overview-note">基金赎回费按自然日和批次分别计算，具体以基金公告为准。</p>
      </Card>

      {/* 点整卡弹出全部资讯（与「板块资金流」卡同一交互：整卡可点 + 视觉提示条）。
          子按钮（更新 / 标题）必须 stopPropagation，否则点它们会连带弹窗。 */}
      <Card className="col-5 overview-news-card" title="市场最新动态" kicker="资讯与板块信号" icon={<Icon.rss className="ico" />}
        hoverable
        onClick={marketItems.length ? openNewsAll : undefined}
        right={<div className="overview-news-actions"><AsyncButton className="btn btn-sm btn-light" onClick={(e) => { e.stopPropagation(); onRefreshNews() }} pendingText="抓取中…"><Icon.refresh className="ico" />更新</AsyncButton><span className="link-btn" aria-hidden="true">查看全部 ›</span></div>}>
        {marketItems.length ? (
          <div className="overview-news-list">
            {marketItems.slice(0, 4).map((item, index) => (
              <article className="overview-news-item" key={item.link || `${item.title}-${index}`}>
                <span className="overview-news-source">{item.source || '市场资讯'}</span>
                <button className="overview-news-title" onClick={(e) => { e.stopPropagation(); readNews(item.link) }}>{item.title}</button>
                <span className="overview-news-time">{time(item.pub_time)}</span>
              </article>
            ))}
          </div>
        ) : <EmptyState title="暂无市场资讯" hint="抓取后会在这里汇总新闻与市场动态。" action={<AsyncButton className="btn btn-sm btn-primary" onClick={onRefreshNews} pendingText="抓取中…">立即抓取</AsyncButton>} />}
      </Card>

      <Modal open={newsOpen} title="市场最新动态" onClose={() => setNewsOpen(false)} width="680px">
        <div className="muted cell-note" style={{ marginBottom: 'var(--space-3)' }}>
          {marketNews?.date} · 已加载 {newsAll.length} 条{newsDone ? '（已全部加载）' : ''}
        </div>
        {!newsAll.length && newsLoading && <div className="muted cell-note">加载中…</div>}
        {!newsAll.length && !newsLoading && (
          <EmptyState title="暂无市场资讯" hint="抓取后会在这里汇总新闻与市场动态。" />
        )}
        {newsAll.length > 0 && (
          <div className="modal-scroll">
            <div className="list">
              {newsAll.map((item, index) => (
                <div className="list-item" key={item.link || `${item.title}-${index}`}>
                  <h4>{item.title}</h4>
                  <p className="meta"><span>{item.source}</span><span>{time(item.pub_time)}</span>
                    {item.link && <button className="link-btn" onClick={() => readNews(item.link)}>阅读全文 ↗</button>}
                  </p>
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12, textAlign: 'center' }}>
              {newsDone ? (
                <span className="muted cell-note">已到底部 · 共 {newsAll.length} 条</span>
              ) : (
                <button className="btn btn-sm" disabled={newsLoading} onClick={() => loadNewsPage(newsAll.length)}>
                  {newsLoading ? '加载中…' : '加载更多'}
                </button>
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
