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
import NewsRotator from '../components/NewsRotator'
import AsyncButton from '../components/AsyncButton'
import Modal from '../components/Modal'
import { money, pct, date, tone, time } from '../lib/format'

export default function Overview() {
  const account = useStore((s) => s.account.data)
  const accountLoading = useStore((s) => s.account.loading)
  const curve = useStore((s) => s.portfolioCurve.data)
  const curveLoading = useStore((s) => s.portfolioCurve.loading)
  const marketNews = useStore((s) => s.marketNews.data)
  const positions = useStore((s) => s.positions.data)
  const openExternal = useStore((s) => s.openExternal)
  const refreshMarketNews = useStore((s) => s.refreshMarketNews)
  const notify = useStore((s) => s.notify)

  // 点击「最新财经资讯」卡片 → 弹窗看更多。
  // 数据直接复用 store 里已拉取的 marketNews（`/api/market-news?limit=50`），无需再请求。
  const [newsOpen, setNewsOpen] = useState(false)

  const onRefreshNews = async () => {
    try {
      const r = await refreshMarketNews()
      notify(`抓取完成，新增 ${r?.fetched ?? 0} 条`, 'success')
    } catch (e) {
      notify('抓取失败：' + e.message, 'error')
    }
  }

  const stats = curve?.stats || {}
  const assets = curve?.items || []
  const categories = assets.map((a) => a.snap_date)
  const values = assets.map((a) => a.total_assets)
  const newsItems = marketNews?.items || []

  // 预算使用进度（多段资金占用条）。零数据下应显示 100% 可用。
  const bd = account?.budget
  const bUsed = bd?.used_in_positions || 0
  const bPending = bd?.pending_committed || 0
  const bAvail = bd?.available || 0
  const bTotal = bUsed + bPending + bAvail || 1
  const segUsed = (bUsed / bTotal) * 100
  const segPending = (bPending / bTotal) * 100
  const segFree = (bAvail / bTotal) * 100
  const pctUsed = bTotal ? (bUsed / bTotal) * 100 : 0
  const pctPending = bTotal ? (bPending / bTotal) * 100 : 0
  const pctFree = bTotal ? (bAvail / bTotal) * 100 : 0
  const budgetRows = bd
    ? [
        { 项目: '初始资金', 金额: bd.initial },
        { 项目: '累积新增', 金额: bd.cumulative_added },
        { 项目: '持仓占用', 金额: bd.used_in_positions },
        { 项目: '待成交冻结', 金额: bd.pending_committed },
        { 项目: '可用余额', 金额: bd.available },
        { 项目: '现金余额', 金额: bd.cash_balance },
        { 项目: '已实现盈亏', 金额: bd.realized_pnl },
        { 项目: '浮动盈亏', 金额: bd.unrealized_pnl },
      ]
    : []

  // 资产配置：按各持仓市值占比（旧版总览的环形图，重写时被漏掉）
  const allocData = (positions?.items || [])
    .filter((p) => (p.market_value || 0) > 0)
    .map((p) => ({ name: p.fund_name || p.fund_code, value: p.market_value }))
  const allocTotal = allocData.reduce((s, d) => s + d.value, 0)

  // 买入批次与罚期（旧版总览能力回归）：惩罚期按「批次」而非「基金」分别计算
  const lots = (positions?.items || []).flatMap((p) =>
    (p.lots || []).map((l) => ({ ...l, fund_code: p.fund_code, fund_name: p.fund_name }))
  )

  return (
    <div className="stack">
      {account?.risk_alert?.alerts?.length > 0 && (
        <div className="wide risk-banner" role="alert">
          <span className="risk-banner-dot">⚠</span>
          <div className="risk-banner-body">
            <strong>风控提醒（仅提醒，不拦截交易）</strong>
            <div className="risk-banner-msg">{account.risk_alert.alerts[0].msg}</div>
            <div className="risk-banner-time">最近更新：{account.risk_alert.updated_at}</div>
          </div>
        </div>
      )}
      {accountLoading && !account ? (
        <div className="tile-grid wide metrics">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} variant="tile" />
          ))}
        </div>
      ) : (
        <div className="tile-grid wide metrics">
          <StatTile label="总资产" num={account?.total_assets} format={money} sub={`持仓 ${account?.position_count || 0} 只`} />
          <StatTile label="持仓市值" num={account?.market_value} format={money} />
          <StatTile
            label="可用预算"
            num={account?.budget?.available}
            format={money}
            sub={`现金 ${money(account?.cash)}`}
          />
          <StatTile
            label="累计收益"
            num={stats.latest_cumulative_pnl}
            format={money}
            tone={tone(stats.latest_cumulative_pnl)}
          />
          <StatTile label="区间收益" num={stats.total_return_pct} format={pct} tone={tone(stats.total_return_pct)} />
          <StatTile label="最大回撤" num={stats.max_drawdown_pct} format={pct} />
        </div>
      )}

      <Card className="col-5"
        title="资产配置"
        kicker="ALLOCATION"
        icon={<Icon.pie className="ico" />}
        right={
          <span className="muted cell-note">
            {allocData.length ? `共 ${allocData.length} 只` : '暂无持仓'}
          </span>
        }
      >
        {!allocData.length ? (
          <EmptyState title="暂无持仓" hint="买入后这里会按市值显示各基金占比" />
        ) : (
          <DonutChart data={allocData} height={240} unit="money" label="资产配置" centerValue={allocTotal} centerLabel="总市值" />
        )}
      </Card>

      <Card className="col-7"
        title="预算使用进度"
        kicker="BUDGET"
        icon={<Icon.wallet className="ico" />}
        right={<ExportButton rows={budgetRows} fileName="预算明细" sheetName="预算" notify={notify} />}
      >
        <div className="budget-bar">
          {segUsed > 0 && <div className="budget-seg pos" style={{ width: `${segUsed}%` }} />}
          {segPending > 0 && <div className="budget-seg pending" style={{ width: `${segPending}%` }} />}
          {segFree > 0 && <div className="budget-seg free" style={{ width: `${segFree}%` }} />}
          <span className="budget-pct">{Math.round(segUsed)}% 已用</span>
        </div>
        <div className="budget-legend">
          <span className="li"><span className="sw" style={{ background: 'var(--accent, #3b6ea5)' }} />持仓占用 <span className="v">{money(bUsed)}</span><span className="pct">{pctUsed.toFixed(1)}%</span></span>
          <span className="li"><span className="sw" style={{ background: '#b9831f' }} />待成交冻结 <span className="v">{money(bPending)}</span><span className="pct">{pctPending.toFixed(1)}%</span></span>
          <span className="li"><span className="sw" style={{ background: 'var(--down, #1f9e5a)' }} />可用余额 <span className="v">{money(bAvail)}</span><span className="pct">{pctFree.toFixed(1)}%</span></span>
        </div>
        {bd?.formula && <div className="budget-formula">{bd.formula}</div>}
      </Card>

      <Card className="col-8"
        title="组合资产曲线"
        kicker="PORTFOLIO"
        icon={<Icon.chartLine className="ico" />}
        right={
          <span className="muted cell-note">
            {date(assets[0])} → {date(assets[assets.length - 1])} · 共 {assets.length} 天
          </span>
        }
      >
        {curveLoading && !assets.length ? (
          <Skeleton variant="block" />
        ) : (
          <NetValueChart categories={categories} values={values} height={280} label="组合资产曲线" />
        )}
      </Card>

      <Card className="wide order-batch"
        title="买入批次与罚期"
        kicker="LOTS"
        icon={<Icon.layers className="ico" />}
        right={<span className="muted cell-note">共 {lots.length} 批</span>}
      >
        {!lots.length && (
          <EmptyState title="暂无批次" hint="买入确认后，每个批次会单独计算 7 天惩罚期" />
        )}
        {lots.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>批次（确认日）</th>
                <th className="right">份额</th>
                <th className="right">已持有</th>
                <th>现在卖的话</th>
              </tr>
            </thead>
            <tbody>
              {lots.map((l) => (
                <tr key={`${l.fund_code}-${l.lot_id}`}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      <strong>{l.fund_name || l.fund_code}</strong>
                      <span className="mono muted cell-note">{l.fund_code}</span>
                      <span className="muted cell-note">· {date(l.confirm_date)}</span>
                      {!l.confirmed && <span className="tag warn">待确认</span>}
                    </div>
                  </td>
                  <td className="num">{l.shares != null ? l.shares.toFixed(2) : '—'}</td>
                  <td className="num">{l.holding_days} 天</td>
                  <td>
                    {l.days_to_penalty_free > 0 ? (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        <span className="tag warn">罚 {(l.fee_rate * 100).toFixed(2)}%</span>
                        <span className="muted cell-note">
                          还需 {l.days_to_penalty_free} 天免罚
                        </span>
                      </span>
                    ) : (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        <span className="tag down">{(l.fee_rate * 100).toFixed(2)}%</span>
                        <span className="muted cell-note">已免罚</span>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {lots.length > 0 && (
          <div className="muted cell-note" style={{ marginTop: 10 }}>
            持有不满 7 天赎回要付 1.5% 罚金，满 7 天降到 0.75%。同一只基金的不同批次分别计算。
          </div>
        )}
      </Card>

      <Card className="col-4 order-news"
        title="最新财经资讯"
        kicker="MARKET NEWS"
        icon={<Icon.rss className="ico" />}
        hoverable
        onClick={() => setNewsOpen(true)}
        right={
          // ⚠️ 整卡可点后，内部按钮必须**阻止冒泡**，否则点「刷新抓取」会顺带弹出弹窗
          <span
            style={{ display: 'inline-flex', alignItems: 'center', gap: 10 }}
            onClick={(e) => e.stopPropagation()}
          >
            <span className="muted cell-note">{marketNews?.date}</span>
            <AsyncButton className="btn btn-sm" onClick={onRefreshNews} pendingText="抓取中…">
              <Icon.refresh className="ico" /> 刷新抓取
            </AsyncButton>
          </span>
        }
      >
        {newsItems.length ? (
          <NewsRotator
            bare
            items={newsItems.slice(0, 6)}
            onOpen={(link) => {
              if (!link) return
              openExternal(link).then((r) => {
                if (!r?.ok) notify('打开失败', 'error')
              })
            }}
          />
        ) : (
          <EmptyState
            title="暂无财经资讯"
            hint="点右上角「刷新」拉取最新市场动态"
            action={
              <AsyncButton className="btn btn-sm" onClick={onRefreshNews} pendingText="抓取中…">
                刷新抓取
              </AsyncButton>
            }
          />
        )}
      </Card>

      {/* 更多资讯弹窗：直接展示 store 已拉取的 marketNews（limit=50），不再额外请求 */}
      <Modal open={newsOpen} title="最新财经资讯" onClose={() => setNewsOpen(false)} width="680px">
        <div className="muted cell-note" style={{ marginBottom: 'var(--space-3)' }}>
          {marketNews?.date} · 共 {newsItems.length} 条
        </div>
        <div className="modal-scroll">
          <div className="list">
            {newsItems.map((it, i) => (
              <div className="list-item" key={it.link || i}>
                <h4>{it.title}</h4>
                <p className="meta">
                  <span>{it.source}</span>
                  <span>{time(it.pub_time)}</span>
                  {it.link && (
                    <button
                      className="link-btn"
                      onClick={() => {
                        openExternal(it.link).then((r) => {
                          if (!r?.ok) notify('打开失败', 'error')
                        })
                      }}
                    >
                      阅读全文 ↗
                    </button>
                  )}
                </p>
              </div>
            ))}
          </div>
        </div>
      </Modal>
    </div>
  )
}
