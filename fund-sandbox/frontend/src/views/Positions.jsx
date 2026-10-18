import { useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import OrderModal from '../components/OrderModal'
import FundDetailModal from '../components/FundDetailModal'
import EmptyState from '../components/EmptyState'
import ExportButton from '../components/ExportButton'
import ConfirmDialog from '../components/ConfirmDialog'
import StatStrip from '../components/StatStrip'
import { money, pct, cls } from '../lib/format'
import { psClass } from '../lib/purchaseStatus'

export default function Positions() {
  const positions = useStore((s) => s.positions.data)
  const funds = useStore((s) => s.funds.data)
  const dingtou = useStore((s) => s.dingtou.data)
  const updateDingtouPlan = useStore((s) => s.updateDingtouPlan)
  const deleteDingtouPlan = useStore((s) => s.deleteDingtouPlan)
  const placeOrder = useStore((s) => s.placeOrder)
  const notify = useStore((s) => s.notify)

  const [tab, setTab] = useState('hold') // hold | watch
  const [detail, setDetail] = useState(null) // { code, name }
  const [order, setOrder] = useState(null) // { code, side }
  const [planDelConfirm, setPlanDelConfirm] = useState(null) // plan object to delete

  const items = positions?.items || []
  const held = new Set(items.map((p) => p.fund_code))
  const planByCode = {}
  for (const p of dingtou?.items || []) planByCode[p.fund_code] = p
  // 关注列表：已加入交易池、但还没建仓的基金
  // 关注列表的**搜索框**：关注一多时便于快速定位。
  // 本地过滤：代码 / 名称 / 类型都能匹配（不调接口，纯前端，实时）。
  const [watchQuery, setWatchQuery] = useState('')
  const _watchAll = (funds?.items || []).filter((f) => f.added && !held.has(f.code))
  const _wq = watchQuery.trim().toLowerCase()
  const watch = _wq
    ? _watchAll.filter((f) => [f.code, f.name, f.category].join(' ').toLowerCase().includes(_wq))
    : _watchAll

  const totalMv = items.reduce((s, p) => s + (p.market_value || 0), 0)
  const totalCost = items.reduce((s, p) => s + (p.total_cost || 0), 0)
  const totalProfit = items.reduce((s, p) => s + (p.profit || 0), 0)
  const avgPct = totalCost > 0 ? (totalProfit / totalCost) * 100 : 0
  const planCount = dingtou?.items?.length || 0

  const posRows = items.map((p) => ({
    基金: p.fund_name,
    代码: p.fund_code,
    份额: p.total_shares,
    成本: p.total_cost,
    市值: p.market_value,
    '持仓收益': p.profit,
    '收益率(%)': p.profit_pct,
    '时间加权(%)': p.twr_pct,
    当前净值: p.current_nav,
  }))

  // 详情弹窗自己按 code 拉净值（含回撤修复 / 定投回测），这里只记录点了哪只
  const openDetail = (code, name) => setDetail({ code, name })

  const onTogglePlan = async (p) => {
    try {
      // 后端 PUT 的 Pydantic 模型要求 fund_code / amount 必填，整条回传、只翻转 enabled
      await updateDingtouPlan(p.id, {
        fund_code: p.fund_code,
        amount: p.amount,
        frequency: p.frequency,
        weekday: p.weekday,
        day_of_month: p.day_of_month,
        fund_name: p.fund_name || null,
        enabled: p.enabled ? 0 : 1,
      })
      notify(p.enabled ? '定投已暂停' : '定投已恢复', 'success')
    } catch (e) {
      notify('更新失败：' + e.message, 'error')
    }
  }

  const onDeletePlan = (p) => setPlanDelConfirm(p)
  const confirmDeletePlan = async () => {
    const p = planDelConfirm
    if (!p) return
    try {
      await deleteDingtouPlan(p.id)
      setPlanDelConfirm(null)
      notify('已删除定投计划', 'success')
    } catch (e) {
      notify('删除失败：' + e.message, 'error')
      throw e
    }
  }

  const buySell = (code, side) => {
    setOrder({ code, side })
    setDetail(null)
  }

  return (
    <div className="stack">
      <StatStrip
        className="wide"
        items={[
          { label: '持仓基金', value: items.length },
          { label: '总市值', value: money(totalMv) },
          { label: '总成本', value: money(totalCost) },
          { label: '持仓收益', value: money(totalProfit), tone: totalProfit > 0 ? 'up' : totalProfit < 0 ? 'down' : undefined },
          { label: '平均收益率', value: pct(avgPct), tone: avgPct > 0 ? 'up' : avgPct < 0 ? 'down' : undefined },
          { label: '定投计划', value: planCount },
          { label: '关注', value: watch.length },
        ]}
      />
      <Card className="wide"
        title={tab === 'hold' ? '持仓明细' : '关注列表'}
        kicker={tab === 'hold' ? 'POSITIONS' : 'WATCHLIST'}
        right={
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {tab === 'hold' && (
              <ExportButton rows={posRows} fileName="持仓明细" sheetName="持仓" notify={notify} />
            )}
            {/* 这两个是「切换视图」而非「执行动作」，故做成 Tab 而非并列按钮，避免与买卖操作混淆 */}
            <div className="tabs" role="tablist" aria-label="持仓 / 关注 切换">
              <button
                className="tab"
                role="tab"
                aria-selected={tab === 'hold'}
                onClick={() => setTab('hold')}
              >
                持仓 {items.length}
              </button>
              <button
                className="tab"
                role="tab"
                aria-selected={tab === 'watch'}
                onClick={() => setTab('watch')}
              >
                关注 {watch.length}
              </button>
            </div>
          </div>
        }
      >
        {tab === 'hold' ? (
          <>
            {!items.length && <EmptyState title="当前无持仓" hint="完成首笔买入后，持仓会显示在这里" />}
            {items.length > 0 && (
              <table className="table">
                <thead>
                  <tr>
                    <th>基金</th>
                    <th className="right">份额</th>
                    <th className="right">成本</th>
                    <th className="right">市值</th>
                    <th className="right">收益率</th>
                    <th className="right">时间加权</th>
                    <th className="right">当前净值</th>
                    <th>状态</th>
                    <th className="right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((p) => {
                    const plan = planByCode[p.fund_code]
                    return (
                      <tr key={p.fund_code}>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                            {/* 用原生 button 而非 <strong onClick>，保证键盘可聚焦、可访问 */}
                            <button
                              className="name-btn"
                              onClick={() => openDetail(p.fund_code, p.fund_name)}
                              title="查看净值曲线"
                            >
                              {p.fund_name}
                            </button>
                            {p.is_qdii && <span className="tag warn">QDII</span>}
                            {plan && (
                              <span className={`tag ${plan.enabled ? 'accent' : 'neutral'}`}>
                                定投{plan.enabled ? '' : '·暂停'}
                              </span>
                            )}
                            {plan && p.purchase_status === '暂停申购' && (
                              <span className="tag danger" title="该基金已暂停申购，定投计划无法买入，请暂停或删除计划">定投受阻·暂停申购</span>
                            )}
                            {plan && p.purchase_status === '限大额' && (
                              <span className="tag warn" title="该基金限大额，定投每期金额受单日限额约束">定投·限大额</span>
                            )}
                          </div>
                          <div className="mono muted cell-note" style={{ marginTop: 2 }}>{p.fund_code}</div>
                        </td>
                        <td className="num">{p.total_shares != null ? p.total_shares.toFixed(2) : '—'}</td>
                        <td className="num">{money(p.total_cost)}</td>
                        <td className="num">{money(p.market_value)}</td>
                        <td className="num">
                          <span className={`tag ${cls(p.profit_pct)}`}>{pct(p.profit_pct)}</span>
                        </td>
                        <td className="num">
                          {p.twr_pct == null ? '—' : <span className={cls(p.twr_pct)}>{pct(p.twr_pct)}</span>}
                        </td>
                        <td className="num">{p.current_nav ?? '—'}</td>
                        <td>
                          <span className={`tag ${psClass(p.purchase_status)}`}>
                            {p.purchase_status || '状态未知'}
                          </span>
                          {p.purchase_status === '限大额' && p.purchase_limit != null && (
                            <div className="muted cell-note" style={{ color: '#8a6d00', fontWeight: 600, marginTop: 2 }}>
                              单日上限 ¥{p.purchase_limit}
                            </div>
                          )}
                          {p.purchase_status === '暂停申购' && (
                            <div className="muted cell-note" style={{ color: '#c62828', marginTop: 2 }}>暂不可买入</div>
                          )}
                        </td>
                        <td className="right">
                          <div
                            style={{
                              display: 'inline-flex',
                              gap: 6,
                              alignItems: 'center',
                              flexWrap: 'wrap',
                              justifyContent: 'flex-end',
                            }}
                          >
                            <button className="btn btn-sm" onClick={() => buySell(p.fund_code, 'buy')}>
                              买
                            </button>
                            <button className="btn btn-sm" onClick={() => buySell(p.fund_code, 'sell')}>
                              赎回
                            </button>
                            {plan && (
                              <button className="btn btn-sm" onClick={() => onTogglePlan(plan)}>
                                {plan.enabled ? '暂停定投' : '恢复定投'}
                              </button>
                            )}
                            {plan && (
                              <button className="btn btn-sm btn-danger" onClick={() => onDeletePlan(plan)}>删除计划</button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
            {items.length > 0 && (
              <div className="muted cell-note" style={{ marginTop: 10 }}>
                点基金名查看净值曲线与中长期统计 ↗
              </div>
            )}
          </>
        ) : (
          <>
            {/* 关注列表搜索框 */}
            <div className="field" style={{ marginBottom: 10 }}>
              <input
                value={watchQuery}
                onChange={(e) => setWatchQuery(e.target.value)}
                placeholder="搜索已关注基金"
              />
            </div>
            {!watch.length && (
              watchQuery.trim()
                ? (
                  <EmptyState
                    title="没有匹配的关注基金"
                    hint={`换个关键词试试（当前搜索：${watchQuery.trim()}）`}
                  />
                )
                : (
                  <EmptyState title="暂无关注" hint="在「交易」页搜索基金并添加，加入后还没买入的会显示在这里" />
                )
            )}
            {watch.length > 0 && (
              <table className="table">
                <thead>
                  <tr>
                    <th>基金</th>
                    <th>类型</th>
                    <th className="right">起购</th>
                    <th>状态</th>
                    <th className="right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {watch.map((f) => (
                    <tr key={f.code}>
                      <td>
                        <strong>{f.name || f.code}</strong>
                        <div className="mono muted cell-note" style={{ marginTop: 2 }}>{f.code}</div>
                      </td>
                      <td className="muted cell-note">
                        {f.category || '—'}
                        {f.is_qdii ? ' · QDII' : ''}
                      </td>
                      <td className="num">{f.min_purchase != null ? `${f.min_purchase} 元` : '未知'}</td>
                      <td>
                        <span className={`tag ${psClass(f.purchase_status)}`}>
                          {f.purchase_status || '状态未知'}
                        </span>
                      </td>
                      <td className="right">
                        <button
                          className="btn btn-sm btn-primary"
                          onClick={() => setOrder({ code: f.code, side: 'buy' })}
                        >
                          买
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </Card>

      <FundDetailModal
        open={!!detail}
        code={detail?.code}
        name={detail?.name}
        onClose={() => setDetail(null)}
        onBuy={() => detail && buySell(detail.code, 'buy')}
        onSell={() => detail && buySell(detail.code, 'sell')}
      />

      <OrderModal
        open={!!order}
        initialCode={order?.code || ''}
        initialSide={order?.side || 'buy'}
        mode={order?.side === 'sell' ? 'redeem' : 'purchase'}
        onClose={() => setOrder(null)}
        onSubmit={placeOrder}
        notify={notify}
      />

      <ConfirmDialog
        open={!!planDelConfirm}
        danger
        title="删除定投计划"
        message={planDelConfirm ? `将删除「${planDelConfirm.fund_name || planDelConfirm.fund_code}」的定投计划，已持仓份额不受影响。确定删除？` : ''}
        confirmText="删除"
        onConfirm={confirmDeletePlan}
        onClose={() => setPlanDelConfirm(null)}
      />
    </div>
  )
}

