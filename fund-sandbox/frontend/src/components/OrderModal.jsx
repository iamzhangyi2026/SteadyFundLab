import { useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import { apiGet } from '../store/api'
import { money } from '../lib/format'
import Modal from './Modal'

// 交易页和持仓页共用下单弹窗。赎回只从持仓操作入口发起，避免对无持仓基金提交赎回。
export default function OrderModal({ open, onClose, onSubmit, notify, initialCode = '', initialSide = 'buy', mode = 'purchase' }) {
  const isRedeem = mode === 'redeem'
  const makeBlank = () => ({
    fund_code: initialCode || '',
    type: 'buy', // purchase: 'buy'(一次性买入) | 'dingtou'(登记定投)
    side: isRedeem ? 'sell' : 'buy',
    amount: '',
    decision_source: 'human',
    reason: '',
  })
  const [form, setForm] = useState(makeBlank)
  const [submitting, setSubmitting] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const account = useStore((s) => s.account.data)
  const avail = account?.budget?.available
  const funds = useStore((s) => s.funds.data)
  // 池内基金复用已取状态；池外基金按代码查询，识别暂停申购和限额。
  const [purchaseInfo, setPurchaseInfo] = useState(null)
  useEffect(() => {
    if (!open) return
    const code = form.fund_code
    if (!code) { setPurchaseInfo(null); return }
    const pf = (funds?.items || []).find((f) => f.code === code)
    if (pf) { setPurchaseInfo(pf); return }
    let alive = true
    setPurchaseInfo(null)
    apiGet(`/api/fund/${code}/purchase`)
      .then((d) => { if (alive) setPurchaseInfo(d || null) })
      .catch(() => { if (alive) setPurchaseInfo(null) })
    return () => { alive = false }
  // 不监听 funds 的轮询对象，避免弹窗打开期间每次轮询都重复请求池外代码。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, form.fund_code])
  // 显示当前份额并提供全额赎回快捷填入，减少手工抄写错误。
  const positions = useStore((s) => s.positions.data)
  const heldShares = isRedeem
    ? (positions?.items || []).find((p) => p.code === form.fund_code)?.shares ?? null
    : null

  const ps = purchaseInfo?.purchase_status
  const paused = ps === '暂停申购'
  const limited = ps === '限大额'

  // 每次打开都重置，避免上一次残留（含从持仓行带入的基金代码）
  useEffect(() => {
    if (open) {
      setForm(makeBlank())
      setShowAdvanced(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialCode, initialSide, mode])

  const num = Number(form.amount)
  const overBudget = !isRedeem && avail != null && num > avail

  const submit = async () => {
    if (!form.fund_code || !form.amount) {
      notify('请填写基金与金额/份额', 'error')
      return
    }
    if (overBudget) {
      notify('金额超过可用预算', 'error')
      return
    }
    if (!Number.isFinite(num) || num <= 0) {
      notify('请输入大于 0 的有效金额或份额', 'error')
      return
    }
    const side = isRedeem ? 'sell' : 'buy'
    const purpose = isRedeem ? 'buy' : (form.type === 'dingtou' ? 'dingtou' : 'buy')
    setSubmitting(true)
    try {
      const r = await onSubmit({
        fund_code: form.fund_code,
        side,
        amount: Number(form.amount),
        purpose,
        decision_source: form.decision_source,
        reason: form.reason,
      })
      // 兼容旧版后端的 200 + {ok:false} 响应，避免把被拒订单提示为成功。
      if (r && r.ok === false) {
        notify('下单被拒绝：' + (r.reason || r.reject_reason || '请查看后端日志'), 'error')
      } else if (r && r.warnings && r.warnings.length) {
        notify('⚠ 已触发风控提醒（订单已照常提交）：\n' + r.warnings.join('\n'), 'warning')
        onClose()
      } else {
        notify(isRedeem ? '赎回申请已提交（待 T+1 确认）' : '下单成功（待 T+1 确认）', 'success')
        onClose()
      }
    } catch (e) {
      notify(isRedeem ? '赎回失败：' + e.message : '下单被拒：' + e.message, 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const typeLabel = form.type === 'dingtou' ? '定投（每期金额）' : '买入（金额）'
  const amountLabel = isRedeem ? '份额' : (form.type === 'dingtou' ? '每期金额（元）' : '金额（元）')

  return (
    <Modal open={open} title={isRedeem ? '赎回（卖出份额）' : '新建订单'} onClose={onClose}>
      <div className="field">
        <label>基金</label>
        <input list="order-fund-list" value={form.fund_code} onChange={set('fund_code')} placeholder="基金代码，或从交易池选择" />
        <datalist id="order-fund-list">
          {(funds?.items || []).map((f) => (
            <option key={f.code} value={f.code}>
              {f.name || f.code}{f.purchase_status ? `（${f.purchase_status}）` : ''}
            </option>
          ))}
        </datalist>
      </div>

      {isRedeem ? (
        // 赎回模式：无下拉，直接给出操作说明 + 份额输入（赎回只从持仓操作菜单进入）
        <div className="field">
          <label>操作</label>
          <div
            style={{
              padding: '9px 12px',
              border: '1px solid var(--border, #e2e6ee)',
              borderRadius: 'var(--radius, 10px)',
              background: 'var(--surface-2, #f4f6fa)',
              color: 'var(--text, #1f2430)',
              fontSize: 14,
            }}
          >
            赎回（卖出份额）
          </div>
          {/* 「当前持有」提示 + 「全部赎回」快捷按钮 */}
          {heldShares != null && (
            <div style={{ marginTop: 7, display: 'flex', alignItems: 'center', gap: 9, fontSize: 13 }}>
              <span className="muted">当前持有 {Number(heldShares).toFixed(4)} 份</span>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setForm((f) => ({ ...f, amount: String(heldShares) }))}
              >
                全部赎回
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="field">
          <label>类型</label>
          <select value={form.type} onChange={set('type')}>
            <option value="buy">买入（金额）</option>
            <option value="dingtou">定投（每期金额）</option>
          </select>
        </div>
      )}

      <div className="field">
        <label>{amountLabel}</label>
        <input
          type="number"
          value={form.amount}
          onChange={set('amount')}
          placeholder="0"
          style={overBudget ? { borderColor: '#c62828' } : undefined}
        />
      </div>

      {form.fund_code && num > 0 && (
        <div className="order-preview">
          {isRedeem ? (
            <>
              <div className="row"><span className="k">操作</span><span className="v">赎回（份额）</span></div>
              <div className="row"><span className="k">份额</span><span className="v">{num}</span></div>
            </>
          ) : (
            <>
              <div className="row"><span className="k">类型</span><span className="v">{typeLabel}</span></div>
              <div className="row"><span className="k">{form.type === 'dingtou' ? '每期金额' : '金额'}</span><span className="v">{money(num)}</span></div>
              {avail != null && (
                <div className={`row ${overBudget ? 'warn' : ''}`}>
                  <span className="k">{form.type === 'dingtou' ? '首期后可用预算' : '买入后可用预算'}</span>
                  <span className="v">{money(Math.max(avail - num, 0))}</span>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {!isRedeem && (
        <button type="button" className="link-btn" onClick={() => setShowAdvanced((v) => !v)} style={{ marginBottom: 8 }}>
          {showAdvanced ? '收起高级选项 ▲' : '高级选项（操作理由）▼'}
        </button>
      )}
      {!isRedeem && showAdvanced && (
        <>
          <div className="field">
            <label>操作理由（可选）</label>
            <textarea rows={2} value={form.reason} onChange={set('reason')} placeholder="记录这次操作的依据" />
          </div>
        </>
      )}

      {!isRedeem && avail != null && (
        <div className="muted" style={{ fontSize: 'var(--fs-caption, 13px)', margin: '2px 0 12px' }}>
          当前可用预算 <b style={{ color: 'var(--accent, #3b6ef6)' }}>{money(avail)}</b>，{form.type === 'dingtou' ? '首期金额' : '买入金额'}不可超过此数
        </div>
      )}
      {!isRedeem && paused && (
        <div
          style={{
            margin: '2px 0 12px',
            padding: '8px 11px',
            borderRadius: 'var(--radius, 10px)',
            background: '#fdecea',
            color: '#c62828',
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          ⚠ 该基金当前「暂停申购」，暂不可买入。
        </div>
      )}
      {!isRedeem && limited && purchaseInfo?.purchase_limit != null && (
        <div
          style={{
            margin: '2px 0 12px',
            padding: '8px 11px',
            borderRadius: 'var(--radius, 10px)',
            background: '#fff8e1',
            color: '#8a6d00',
            fontSize: 13,
          }}
        >
          限大额：单日最多买入 {money(purchaseInfo.purchase_limit)}。
        </div>
      )}
      <button
        className="btn btn-primary"
        style={{ width: '100%' }}
        onClick={submit}
        disabled={submitting || overBudget || paused}
      >
        {submitting
          ? '提交中…'
          : paused
            ? '暂停申购·不可买入'
            : overBudget
              ? '超出预算'
              : (isRedeem ? '确认赎回' : '确认下单')}
      </button>
    </Modal>
  )
}
