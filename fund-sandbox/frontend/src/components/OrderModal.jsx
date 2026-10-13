import { useEffect, useState } from 'react'
import { useStore } from '../store/useStore'
import { money } from '../lib/format'
import Modal from './Modal'

// 人工下单 / 赎回弹窗。交易页与持仓页共用。
// 两种模式：
//   mode='purchase'（默认）：新建「购买订单」，类型下拉只有 买入 / 定投，绝不含赎回。
//   mode='redeem'：对已持仓基金做赎回（卖出份额），仅从持仓操作菜单进入。
// 设计依据：赎回不是购买订单——没买过就没有可赎的份额，故赎回只出现在持仓操作菜单，
//           新建购买订单的下拉里不该出现「赎回」选项。
export default function OrderModal({ open, onClose, onSubmit, notify, initialCode = '', initialSide = 'buy', mode = 'purchase' }) {
  const isRedeem = mode === 'redeem'
  const makeBlank = () => ({
    fund_code: initialCode || '',
    type: 'buy', // purchase: 'buy'(一次性买入) | 'dingtou'(登记定投)
    side: isRedeem ? 'sell' : 'buy',
    amount: '',
    purpose: 'buy',
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
  // 池内基金直接从 funds.data 取状态；池外（搜索买入）基金按 code 实时查轻量接口，
  // 保证「只要是抓取的基金」暂停/限大额都能识别，不漏标。
  const [purchaseInfo, setPurchaseInfo] = useState(null)
  useEffect(() => {
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
  }, [form.fund_code, funds])
  // 赎回「当前持有 / 全部赎回」：从持仓 store 取（
  // 没有它，用户想清仓只能自己去持仓页抄份额数再手填，容易抄错/填错。）
  const positions = useStore((s) => s.positions.data)
  const heldShares = isRedeem
    ? (positions?.items || []).find((p) => p.code === form.fund_code)?.shares ?? null
    : null

  const ps = purchaseInfo?.purchase_status
  const paused = ps === '暂停申购'
  const limited = ps === '限大额'

  // 每次打开都重置，避免上一次残留（含从持仓行带入的基金代码）
  useEffect(() => {
    if (open) setForm(makeBlank())
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
      // 风控边界只是提醒：越界时订单照常提交，但要把提醒明确弹给用户
      // ⚠️ 后端已把拒单改为抛 400（见 `api.py` 的 api_order），
      // 但前端仍应主动判 `ok` —— 万一后端某天又变成「200 + {ok:false}」，
      // 也不至于把"被拒"显示成"下单成功"。宁可多判一次，不可骗用户。
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
          {/* ⚠️ 「当前持有」提示 + 「全部赎回」快捷按钮 */}
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
          {showAdvanced ? '收起高级选项 ▲' : '高级选项（决策来源 / 理由）▼'}
        </button>
      )}
      {!isRedeem && showAdvanced && (
        <>
          <div className="field">
            <label>决策来源</label>
            <select value={form.decision_source} onChange={set('decision_source')}>
              <option value="human">人工</option>
              <option value="rule">规则引擎</option>
              <option value="external">外部 AI</option>
              <option value="llm">LLM</option>
            </select>
          </div>
          <div className="field">
            <label>决策理由（人工可留空）</label>
            <textarea rows={2} value={form.reason} onChange={set('reason')} placeholder="非人工来源必填" />
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
