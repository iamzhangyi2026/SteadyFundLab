import { useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import { Icon } from '../components/Icons'
import EmptyState from '../components/EmptyState'
import OrderModal from '../components/OrderModal'
import FundDetailModal from '../components/FundDetailModal'
import ConfirmDialog from '../components/ConfirmDialog'
import AsyncButton from '../components/AsyncButton'
import StatStrip from '../components/StatStrip'
import { time } from '../lib/format'
import { psClass } from '../lib/purchaseStatus'

export default function Trade() {
  const orders = useStore((s) => s.orders.data)
  const dingtou = useStore((s) => s.dingtou.data)
  const placeOrder = useStore((s) => s.placeOrder)
  const cancelOrder = useStore((s) => s.cancelOrder)
  const deleteOrder = useStore((s) => s.deleteOrder)
  const searchFunds = useStore((s) => s.searchFunds)
  const refreshFunds = useStore((s) => s.refreshFunds)
  const addFund = useStore((s) => s.addFund)
  const removeFund = useStore((s) => s.removeFund)
  const notify = useStore((s) => s.notify)

  const [orderOpen, setOrderOpen] = useState(false)
  // 基金详情弹窗（点击基金即弹出）：{ code, name }
  const [detail, setDetail] = useState(null)
  // 下单方向：买入 / 赎回（详情弹窗里的两个按钮会带入）
  const [orderSide, setOrderSide] = useState('buy')

  // 全市场基金搜索（旧版能力回归：按代码 / 名称 / 拼音找基金并加入交易池）
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [sMsg, setSMsg] = useState(null) // { t: 'ok' | 'err' | 'info', text }
  const [cancelConfirm, setCancelConfirm] = useState(null)
  const [delConfirm, setDelConfirm] = useState(null)
  // 搜索结果「购买」带入下单弹窗的基金代码
  const [orderCode, setOrderCode] = useState('')
  // 批量选择（删除 / 取消下单）：null 以外的 Set 表示已勾选的项
  const [poolSelMode, setPoolSelMode] = useState(false)
  const [poolSel, setPoolSel] = useState(() => new Set())
  const [orderSelMode, setOrderSelMode] = useState(false)
  const [orderSel, setOrderSel] = useState(() => new Set())

  const orderItems = orders?.items || []
  const planItems = dingtou?.items || []

  const onCancel = (id) => setCancelConfirm(id)
  const confirmCancel = async () => {
    const id = cancelConfirm
    if (!id) return
    try {
      await cancelOrder(id)
      setCancelConfirm(null)
      notify('已撤销订单', 'success')
    } catch (e) {
      notify('撤销失败：' + e.message, 'error')
      throw e
    }
  }

  const funds = useStore((s) => s.funds.data)
  const pooled = new Set((funds?.items || []).map((f) => f.code))
  const fundItems = funds?.items || []
  const planActive = planItems.filter((p) => p.enabled).length
  const pendingOrders = orderItems.filter((o) => o.status === 'pending').length

  const onSearch = async () => {
    const kw = q.trim()
    if (!kw) {
      setSMsg({ t: 'err', text: '请输入关键词' })
      return
    }
    setSMsg({ t: 'info', text: '搜索中…' })
    try {
      const r = await searchFunds(kw)
      setResults(r)
      if (r?.ok === false) setSMsg({ t: 'err', text: r.note || '搜索失败' })
      else if (!r?.items?.length) setSMsg({ t: 'info', text: r.note || '没有匹配，换个关键词试试' })
      else setSMsg(null)
    } catch (e) {
      setResults(null)
      setSMsg({ t: 'err', text: '搜索失败：' + e.message })
    }
  }

  const onRefreshMarket = async () => {
    setSMsg({ t: 'info', text: '正在强制重拉东财全市场表，覆盖当天新上市基金…' })
    try {
      const r = await refreshFunds()
      setSMsg({ t: 'ok', text: r?.note || '全市场表已更新' })
      notify('全市场表已刷新', 'success')
    } catch (e) {
      setSMsg({ t: 'err', text: '刷新失败：' + e.message })
    }
  }

  // 人工添加：直接加入候选池并抓取历史净值，**不询问理由**
  // （后端按 `decision_source` 区分——human 不强制 reason，ai / 程序化必须填）。
  // **Electron 不支持 `prompt()`**（调用即抛
  // `Error: prompt() is and will not be supported.`），
  // 因此**任何原生弹窗都不要再用**，统一走自建组件（ConfirmDialog / PromptDialog 等）：
  // 原生调用无人捕获异常时，界面会静默无反应。
  const onAddFund = async (code) => {
    setSMsg({ t: 'info', text: `正在添加 ${code} 并抓取历史净值…` })
    try {
      const r = await addFund(code)
      setSMsg({ t: 'ok', text: r?.note || `已添加 ${code}` })
      notify('已加入候选池', 'success')
    } catch (e) {
      setSMsg({ t: 'err', text: '添加失败：' + e.message })
      notify('添加失败：' + e.message, 'error')
    }
  }

  const onDeleteFund = (code) => setDelConfirm(code)
  const confirmDeleteFund = async () => {
    const code = delConfirm
    if (!code) return
    try {
      await removeFund(code)
      setDelConfirm(null)
      notify('已从交易池移除', 'success')
    } catch (e) {
      notify('移除失败：' + e.message, 'error')
      throw e
    }
  }

  // ── 批量选择（删除 / 取消下单）─────────────────────────────
  const toggleIn = (setter) => (id) =>
    setter((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  const togglePool = toggleIn(setPoolSel)
  const toggleOrder = toggleIn(setOrderSel)
  const allPoolChecked = fundItems.length > 0 && poolSel.size === fundItems.length
  const allOrderChecked = orderItems.length > 0 && orderSel.size === orderItems.length
  const setPoolAll = (on) => setPoolSel(on ? new Set(fundItems.map((f) => f.code)) : new Set())
  const setOrderAll = (on) => setOrderSel(on ? new Set(orderItems.map((o) => o.order_id)) : new Set())

  const doDeletePool = async () => {
    const codes = [...poolSel]
    if (!codes.length) { notify('请先勾选要移除的基金', 'error'); return }
    let ok = 0
    const errs = []
    for (const code of codes) {
      try { await removeFund(code); ok += 1 } catch (e) { errs.push(e.message) }
    }
    setPoolSel(new Set())
    setPoolSelMode(false)
    notify(`已移除 ${ok} 只${errs.length ? `，${errs.length} 只失败（${errs[0]}）` : ''}`, errs.length ? 'warning' : 'success')
  }

  const doDeleteOrders = async () => {
    const ids = [...orderSel]
    if (!ids.length) { notify('请先勾选要删除的订单', 'error'); return }
    let ok = 0
    const errs = []
    for (const id of ids) {
      try { await deleteOrder(id); ok += 1 } catch (e) { errs.push(e.message) }
    }
    setOrderSel(new Set())
    setOrderSelMode(false)
    notify(`已删除 ${ok} 条${errs.length ? `，${errs.length} 条不能删（${errs[0]}）` : ''}`, errs.length ? 'warning' : 'success')
  }

  const doCancelOrders = async () => {
    const ids = [...orderSel]
    if (!ids.length) { notify('请先勾选要取消的订单', 'error'); return }
    let ok = 0
    const errs = []
    for (const id of ids) {
      try { await cancelOrder(id); ok += 1 } catch (e) { errs.push(e.message) }
    }
    setOrderSel(new Set())
    setOrderSelMode(false)
    notify(`已取消 ${ok} 条${errs.length ? `，${errs.length} 条失败（${errs[0]}）` : ''}`, errs.length ? 'warning' : 'success')
  }

  // 打开下单弹窗（买入 / 赎回）
  const openOrder = (code, side = 'buy') => {
    setOrderCode(code || '')
    setOrderSide(side)
    setOrderOpen(true)
  }
  // 搜索结果直接购买：带入基金代码打开下单弹窗
  const onBuy = (code) => openOrder(code, 'buy')

  // 点击基金 → 打开「详细资讯」弹窗。
  // **整行可点**：需求说的是「点击（这个物件）」，不能只绑行内的小按钮 ——
  // 见 docs/项目/系统变更记录.md 里「板块资金流」那次教训（只绑小按钮 → 用户点卡片没反应）。
  const openDetail = (code, name) => setDetail({ code, name })
  const rowKeyDown = (code, name) => (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openDetail(code, name)
    }
  }

  return (
    <div className="stack">
      <StatStrip
        className="wide"
        items={[
          { label: '交易池基金', value: fundItems.length },
          { label: '定投计划', value: planItems.length },
          { label: '启用中', value: planActive, tone: planActive ? 'ok' : undefined },
          { label: '待确认订单', value: pendingOrders, tone: pendingOrders ? 'warn' : undefined },
        ]}
      />
      <Card className="wide"
        title="交易池"
        kicker="FUND POOL"
        right={
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            {poolSelMode ? (
              <>
                <label className="muted cell-note" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                  <input type="checkbox" checked={allPoolChecked} onChange={(e) => setPoolAll(e.target.checked)} /> 全选
                </label>
                <button className="btn btn-sm btn-danger" onClick={doDeletePool} disabled={!poolSel.size}>
                  移除选中（{poolSel.size}）
                </button>
                <button className="btn btn-sm" onClick={() => { setPoolSelMode(false); setPoolSel(new Set()) }}>取消</button>
              </>
            ) : (
              <>
                <AsyncButton
                  className="btn btn-sm"
                  onClick={onRefreshMarket}
                  pendingText="刷新中…（约20秒）"
                  title="强制重拉东财全市场表，覆盖当天新上市基金"
                >
                  <Icon.refresh className="ico" /> 刷新市场
                </AsyncButton>
                <button className="btn btn-sm btn-danger" onClick={() => setPoolSelMode(true)} disabled={!fundItems.length}>
                  删除
                </button>
              </>
            )}
          </div>
        }
      >
        <div className="muted cell-note" style={{ lineHeight: 1.7, marginBottom: 10 }}>
          按<b>代码 / 名称 / 拼音</b>搜索全市场基金，结果直接带出购买起点、申购状态与费率；点「添加」即加入交易池并自动抓取历史净值。
        </div>
        <div className="row2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onSearch()}
            placeholder="基金代码 / 名称 / 拼音，如 沪深300"
          />
          <AsyncButton className="btn btn-primary" style={{ flex: '0 0 auto' }} onClick={onSearch} pendingText="搜索中…">
            <Icon.search className="ico" /> 搜索
          </AsyncButton>
        </div>
        {sMsg && (
          <div
            className={sMsg.t === 'err' ? 'err' : 'muted'}
            style={{
              padding: '7px 2px 2px',
              color: sMsg.t === 'ok' ? 'var(--down, #1f9e5a)' : undefined,
            }}
          >
            {sMsg.text}
          </div>
        )}
        {results?.items?.length > 0 && (
          <div className="list" style={{ marginTop: 10 }}>
            {results.items.map((f) => {
              const inPool = pooled.has(f.code)
              return (
                <div
                  className="list-item clickable"
                  key={f.code}
                  style={{ padding: '10px 13px' }}
                  role="button"
                  tabIndex={0}
                  title="点击查看详细资讯"
                  onClick={() => openDetail(f.code, f.name)}
                  onKeyDown={rowKeyDown(f.code, f.name)}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, flexWrap: 'wrap' }}>
                        <strong>{f.name || f.code}</strong>
                        <span className="mono muted cell-note">{f.code}</span>
                        <span className="muted cell-note">
                          {f.category}
                          {f.is_qdii ? ' · QDII' : ''}
                        </span>
                      </div>
                      <div
                        className="muted cell-note"
                        style={{ marginTop: 5, display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}
                      >
                        <span>起购 {f.min_purchase != null ? `${f.min_purchase} 元` : '未知'}</span>
                        <span className={`tag ${psClass(f.purchase_status)}`}>{f.purchase_status || '状态未知'}</span>
                        {f.fee_rate != null && <span>费率 {(f.fee_rate * 100).toFixed(2)}%</span>}
                        {f.purchase_status === '限大额' && f.purchase_limit != null && (
                          <span style={{ color: '#8a6d00', fontWeight: 600 }}>单日上限 ¥{f.purchase_limit}</span>
                        )}
                        {f.purchase_status !== '限大额' && f.purchase_limit != null && (
                          <span>日限 {f.purchase_limit} 元</span>
                        )}
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: '0 0 auto' }}>
                      <button
                        className="btn btn-sm"
                        onClick={(e) => { e.stopPropagation(); onBuy(f.code) }}
                        title="直接下单买入这只基金"
                      >
                        <Icon.plus className="ico" /> 购买
                      </button>
                      <AsyncButton
                        className="btn btn-sm btn-primary"
                        disabled={inPool}
                        onClick={(e) => { e.stopPropagation(); onAddFund(f.code, f.name) }}
                        pendingText="添加中…"
                      >
                        {inPool ? '已关注' : '添加'}
                      </AsyncButton>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
        <div style={{ marginTop: 18, borderTop: '1px solid var(--line, #ececec)', paddingTop: 14 }}>
          <div className="topbars-title" style={{ marginBottom: 8 }}>当前交易池（{fundItems.length}）</div>
          <div className="list">
            {fundItems.length === 0 && (
              <EmptyState title="交易池为空" hint="用上面的搜索添加基金，即可在「下单」时选择" />
            )}
            {fundItems.map((f) => (
              <div
                className="list-item clickable"
                key={f.code}
                style={{ padding: '9px 13px' }}
                role="button"
                tabIndex={0}
                title="点击查看详细资讯"
                onClick={() => openDetail(f.code, f.name)}
                onKeyDown={rowKeyDown(f.code, f.name)}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                  {poolSelMode && (
                    <input
                      type="checkbox"
                      aria-label={`选择 ${f.name || f.code}`}
                      checked={poolSel.has(f.code)}
                      onChange={() => togglePool(f.code)}
                      onClick={(e) => e.stopPropagation()}
                      style={{ flex: '0 0 auto', width: 16, height: 16 }}
                    />
                  )}
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <strong style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name || f.code}</strong>
                    <span className="mono muted cell-note" style={{ display: 'block', marginTop: 2 }}>{f.code}</span>
                    <div
                      className="muted cell-note"
                      style={{ marginTop: 5, display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}
                    >
                      <span>起购 {f.min_purchase != null ? `${f.min_purchase} 元` : '未知'}</span>
                      <span className={`tag ${psClass(f.purchase_status)}`}>{f.purchase_status || '状态未知'}</span>
                      {f.purchase_status === '限大额' && f.purchase_limit != null && (
                        <span style={{ color: '#8a6d00', fontWeight: 600 }}>单日上限 ¥{f.purchase_limit}</span>
                      )}
                      {f.purchase_status === '暂停申购' && (
                        <span style={{ color: '#c62828', fontWeight: 600 }}>· 暂不可买入</span>
                      )}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: '0 0 auto' }}>
                    {!f.added && (
                      <span className="tag neutral" style={{ fontSize: 'var(--fs-caption, 12px)', padding: '2px 8px', whiteSpace: 'nowrap' }}>官方</span>
                    )}
                    <button
                      className="btn btn-sm btn-danger"
                      style={{ whiteSpace: 'nowrap' }}
                      onClick={(e) => { e.stopPropagation(); onDeleteFund(f.code) }}
                    >
                      移除
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </Card>

      <Card className="wide"
        title="订单"
        kicker="ORDERS"
        right={
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            {orderSelMode ? (
              <>
                <label className="muted cell-note" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                  <input type="checkbox" checked={allOrderChecked} onChange={(e) => setOrderAll(e.target.checked)} /> 全选
                </label>
                <button className="btn btn-sm btn-danger" onClick={doDeleteOrders} disabled={!orderSel.size}>
                  删除选中（{orderSel.size}）
                </button>
                <button className="btn btn-sm" onClick={doCancelOrders} disabled={!orderSel.size}>
                  取消下单（{orderSel.size}）
                </button>
                <button className="btn btn-sm" onClick={() => { setOrderSelMode(false); setOrderSel(new Set()) }}>取消</button>
              </>
            ) : (
              <button className="btn btn-sm btn-danger" onClick={() => setOrderSelMode(true)} disabled={!orderItems.length}>删除</button>
            )}
          </div>
        }
      >
        {!orderItems.length && <EmptyState title="暂无订单" hint="在上方「交易池」搜索基金点「购买」下单，或去「模拟持仓」赎回" />}
        {orderItems.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                {orderSelMode && <th style={{ width: 34 }} aria-label="选择" />}
                <th>基金</th>
                <th>方向</th>
                <th className="right">金额/份额</th>
                <th>状态</th>
                <th>时间</th>
                <th className="right">操作</th>
              </tr>
            </thead>
            <tbody>
              {orderItems.map((o) => (
                <tr key={o.order_id}>
                  {orderSelMode && (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`选择订单 ${o.order_id}`}
                        checked={orderSel.has(o.order_id)}
                        onChange={() => toggleOrder(o.order_id)}
                        style={{ width: 16, height: 16 }}
                      />
                    </td>
                  )}
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      <strong>{o.fund_name || o.fund_code}</strong>
                      {o.purpose === 'dingtou' && (
                        <span className="tag accent" title="由定投计划产生">定投</span>
                      )}
                    </div>
                    <div className="mono muted cell-note" style={{ marginTop: 2 }}>{o.fund_code}</div>
                  </td>
                  <td>
                    <span className={`tag ${o.side === 'buy' ? 'up' : 'down'}`}>
                      {o.side === 'buy' ? '买入' : '赎回'}
                    </span>
                  </td>
                  <td className="num">{o.amount}</td>
                  <td>
                    <span className={`tag ${o.status === 'confirmed' ? 'accent' : o.status === 'cancelled' ? 'neutral' : 'warn'}`}>
                      {o.status === 'confirmed' ? '已确认' : o.status === 'cancelled' ? '已撤' : '待确认'}
                    </span>
                  </td>
                  <td className="muted cell-note">{time(o.created_at)}</td>
                  <td className="right">
                    {o.status === 'pending' && (
                      <button className="btn btn-sm btn-danger" onClick={() => onCancel(o.order_id)}>撤销</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <OrderModal
        open={orderOpen}
        onClose={() => setOrderOpen(false)}
        onSubmit={placeOrder}
        notify={notify}
        initialCode={orderCode}
        initialSide={orderSide}
        mode={orderSide === 'sell' ? 'redeem' : 'purchase'}
      />

      <ConfirmDialog
        open={!!cancelConfirm}
        title="撤销订单"
        message="撤销后该订单不可恢复。确定撤销？"
        confirmText="撤销"
        onConfirm={confirmCancel}
        onClose={() => setCancelConfirm(null)}
      />
      <ConfirmDialog
        open={!!delConfirm}
        danger
        title="从交易池移除"
        message={`将把 ${delConfirm || ''} 从交易池移除。该基金的历史净值与档案保留，之后重新添加可恢复。确定移除？`}
        confirmText="移除"
        onConfirm={confirmDeleteFund}
        onClose={() => setDelConfirm(null)}
      />
      {/* 注：「添加理由」由 decision_source 决定：人工直接加，AI / 程序化必填。
          通用输入弹窗见 components/PromptDialog.jsx（替代 Electron 不支持的 window.prompt）。 */}

      {/* 基金「详细资讯」弹窗：点击交易页里的基金（**搜索结果**或**交易池**）即弹出。
          详情内的「手动买入 / 手动赎回」接回下单弹窗（对应方向）。 */}
      <FundDetailModal
        open={!!detail}
        code={detail?.code}
        name={detail?.name}
        onClose={() => setDetail(null)}
        onBuy={() => {
          const c = detail?.code
          setDetail(null)
          openOrder(c, 'buy')
        }}
        onSell={() => {
          const c = detail?.code
          setDetail(null)
          openOrder(c, 'sell')
        }}
      />
    </div>
  )
}
