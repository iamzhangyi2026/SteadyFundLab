import { useCallback, useEffect, useRef, useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import ConfirmDialog from '../components/ConfirmDialog'
import EmptyState from '../components/EmptyState'
import Modal from '../components/Modal'
import StatStrip from '../components/StatStrip'
import RealFundModal from '../components/RealFundModal'
import { money, pct, date } from '../lib/format'

// B 区（真实仓位 · 人接管 · 隔离区）。
// 本页自带 fetch，不进 A 区共享 store——两区互不干扰。
// 边界定义见 docs/项目/权限边界-人机隔离.md
const API = '/api/real'

async function j(url, opts) {
  const r = await fetch(url, opts)
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(body.detail || body.error || `HTTP ${r.status}`)
  return body
}

export default function RealPositions() {
  const notify = useStore((s) => s.notify)
  const [holdings, setHoldings] = useState([])
  const [summary, setSummary] = useState(null)
  const [snapshots, setSnapshots] = useState([])
  const [ocrOk, setOcrOk] = useState(false)
  const [text, setText] = useState('')
  const [cand, setCand] = useState(null)
  const [busy, setBusy] = useState(false)
  const [detailCode, setDetailCode] = useState(() => new URLSearchParams(window.location.search).get('detail'))
  const [sortDesc, setSortDesc] = useState(true)
  const [plans, setPlans] = useState([])
  const [planFor, setPlanFor] = useState(null)
  const [planDel, setPlanDel] = useState(null)
  const [editFor, setEditFor] = useState(null)
  const [delFor, setDelFor] = useState(null)
  const [planForm, setPlanForm] = useState({ amount: '25', frequency: 'weekly', weekday: '0', day_of_month: '1' })
  const [editForm, setEditForm] = useState({ fund_name: '', shares: '', cost_amount: '', market_amount: '' })
  const [saving, setSaving] = useState(false)
  const [autoSec, setAutoSec] = useState(() => {
    const raw = localStorage.getItem('fund-sandbox.real-autorefresh')
    if (raw == null) return 30
    const v = Number(raw)
    return Number.isFinite(v) && v >= 0 ? v : 30
  })
  const fileRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const h = await j(`${API}/holdings`)
      setHoldings(h.holdings || [])
      setSummary(h.summary || null)
      const s = await j(`${API}/snapshots?limit=30`)
      setSnapshots(s.snapshots || [])
      try {
        const p = await j('/api/dingtou/plans')
        setPlans(p.items || [])
      } catch (e) { void e }
    } catch (e) {
      notify('读取真实仓位失败：' + e.message, 'error')
    }
  }, [notify])

  // 抓最新涨跌幅（后台），完成后刷新列表
  const refreshNav = useCallback(async () => {
    setBusy(true)
    try {
      await j(`${API}/nav/refresh`, { method: 'POST' })
      await load()
    } catch (e) {
      notify('刷新涨跌幅失败：' + e.message, 'error')
    } finally {
      setBusy(false)
    }
  }, [load, notify])

  useEffect(() => {
    load()
    j(`${API}/ocr-status`).then((r) => setOcrOk(!!r.available)).catch(() => setOcrOk(false))
    refreshNav()
  }, [load, refreshNav])

  // 盘中估值定时刷新：只打轻量的 estimates 接口（新浪批量），不动净值
  const [pollOn, setPollOn] = useState(null)
  useEffect(() => {
    if (!autoSec) return undefined
    let timer = null
    let stopped = false
    const tick = async () => {
      if (stopped) return
      let win = null
      try {
        win = await j(`${API}/poll-window`)
      } catch (e) { void e }
      if (stopped) return
      setPollOn(win)
      if (win && !win.active) return          // 非轮询窗口：不拉，等下一轮判定
      try {
        await j(`${API}/estimates/refresh`, { method: 'POST' })
        await load()
      } catch (e) { void e }
    }
    tick()
    timer = setInterval(tick, autoSec * 1000)
    return () => { stopped = true; if (timer) clearInterval(timer) }
  }, [autoSec, load])

  const snap = async () => {
    try {
      await j(`${API}/snapshots`, { method: 'POST' })
      notify('已生成今日快照')
      await load()
    } catch (e) {
      notify('生成快照失败：' + e.message, 'error')
    }
  }

  const changeAuto = (v) => {
    setAutoSec(v)
    try {
      localStorage.setItem('fund-sandbox.real-autorefresh', String(v))
    } catch (e) {
      void e
    }
  }

  const doImportText = async () => {
    if (!text.trim()) return
    setBusy(true)
    try {
      const res = await j(`${API}/imports/text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      setCand(res)
      if (!res.rows?.length) notify('没解析出基金行，请检查文本是否含 6 位基金代码', 'info')
    } catch (e) {
      notify('导入失败：' + e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const doImportImage = async (file) => {
    if (!file) return
    setBusy(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const res = await j(`${API}/imports/image`, { method: 'POST', body: fd })
      setCand(res)
    } catch (e) {
      notify('截图识别失败：' + e.message, 'error')
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const editRow = (i, k, v) => {
    setCand((c) => {
      const rows = c.rows.slice()
      const num = v === '' ? null : Number(v)
      rows[i] = { ...rows[i], [k]: Number.isNaN(num) ? v : num }
      return { ...c, rows }
    })
  }

  const confirm = async () => {
    if (!cand) return
    setBusy(true)
    try {
      const res = await j(`${API}/imports/${cand.import_id}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: cand.rows }),
      })
      notify(`已确认入库（持仓 ${res.holdings_count} 只）`, 'success')
      setCand(null)
      setText('')
      load()
    } catch (e) {
      notify('确认失败：' + e.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const discard = async () => {
    if (cand) {
      try {
        await j(`${API}/imports/${cand.import_id}/discard`, { method: 'POST' })
      } catch (e) {
        void e
      }
    }
    setCand(null)
  }

  // 删除走二次确认（不可逆：库里无软删，删了要重新导入才能回来）
  const del = (h) => setDelFor({ code: h.fund_code, name: h.fund_name || h.fund_code })

  const confirmDelete = async () => {
    const target = delFor
    if (!target) return
    try {
      await j(`${API}/holdings/${target.code}`, { method: 'DELETE' })
      notify('已删除 ' + target.name, 'success')
      setDelFor(null)
      load()
    } catch (e) {
      notify('删除失败：' + e.message, 'error')
      throw e
    }
  }

  // ── 定投计划（dingtou 计划由 A 区沙盒执行；此处只提供人发起入口）──
  const planByCode = {}
  for (const p of plans) planByCode[p.fund_code] = p

  const openPlan = (h) => {
    const plan = planByCode[h.fund_code] || null
    setPlanForm({
      amount: plan ? String(plan.amount) : '25',
      frequency: plan ? plan.frequency : 'weekly',
      weekday: String(plan ? plan.weekday : 0),
      day_of_month: String(plan ? plan.day_of_month : 1),
    })
    setPlanFor({ code: h.fund_code, name: h.fund_name || h.fund_code, plan })
  }

  const submitPlan = async () => {
    const amount = Number(planForm.amount)
    if (!(amount > 0)) { notify('每期金额必须大于 0', 'error'); return }
    setSaving(true)
    try {
      const body = {
        fund_code: planFor.code,
        fund_name: planFor.name,
        amount,
        frequency: planForm.frequency,
        weekday: planForm.frequency === 'weekly' ? Number(planForm.weekday) : 0,
        day_of_month: planForm.frequency === 'monthly' ? Number(planForm.day_of_month) : 1,
        enabled: planFor.plan ? planFor.plan.enabled : 1,
      }
      const r = planFor.plan
        ? await j(`/api/dingtou/plans/${planFor.plan.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          })
        : await j('/api/dingtou/plans', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          })
      if (r.ok === false) throw new Error(r.reason || '保存失败')
      notify(planFor.plan ? '定投计划已更新' : '定投计划已创建', 'success')
      setPlanFor(null)
      load()
    } catch (e) {
      notify('保存定投计划失败：' + e.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const togglePlan = async () => {
    const p = planFor.plan
    try {
      await j(`/api/dingtou/plans/${p.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fund_code: p.fund_code, amount: p.amount, frequency: p.frequency,
          weekday: p.weekday, day_of_month: p.day_of_month,
          fund_name: p.fund_name || null, enabled: p.enabled ? 0 : 1,
        }),
      })
      notify(p.enabled ? '定投已暂停' : '定投已恢复', 'success')
      setPlanFor(null)
      load()
    } catch (e) {
      notify('操作失败：' + e.message, 'error')
    }
  }

  const confirmDeletePlan = async () => {
    const p = planDel
    if (!p) return
    try {
      await j(`/api/dingtou/plans/${p.id}`, { method: 'DELETE' })
      notify('已删除定投计划', 'success')
      setPlanDel(null)
      setPlanFor(null)
      load()
    } catch (e) {
      notify('删除失败：' + e.message, 'error')
      throw e
    }
  }

  // ── 修改持仓（人经界面触发；profit 由后端按市值−成本重算）──
  const openEdit = (h) => {
    setEditForm({
      fund_name: h.fund_name || '',
      shares: h.shares != null ? String(h.shares) : '',
      cost_amount: h.cost_amount != null ? String(h.cost_amount) : '',
      market_amount: h.market_amount != null ? String(h.market_amount) : '',
    })
    setEditFor(h)
  }

  const submitEdit = async () => {
    const num = (v) => (v === '' ? null : Number(v))
    const shares = num(editForm.shares)
    const cost = num(editForm.cost_amount)
    const market = num(editForm.market_amount)
    for (const [label, v] of [['份额', shares], ['成本', cost], ['市值', market]]) {
      if (v != null && (!Number.isFinite(v) || v < 0)) {
        notify(`${label}必须是不小于 0 的数字`, 'error')
        return
      }
    }
    setSaving(true)
    try {
      const r = await j(`${API}/holdings/${editFor.fund_code}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fund_name: editForm.fund_name, shares,
          cost_amount: cost, market_amount: market,
        }),
      })
      if (r.ok === false) throw new Error(r.reason || '保存失败')
      notify('已保存修改', 'success')
      setEditFor(null)
      load()
    } catch (e) {
      notify('保存失败：' + e.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  const items = summary
    ? [
        { label: '总市值', value: money(summary.total_market) },
        { label: '总成本', value: money(summary.total_cost) },
        { label: '总盈亏', value: money(summary.total_profit), tone: summary.total_profit > 0 ? 'up' : summary.total_profit < 0 ? 'down' : undefined },
        { label: '持仓数', value: summary.holdings_count },
      ]
    : []

  const toneCls = (n) => (n > 0 ? 'tone-up' : n < 0 ? 'tone-down' : '')
  // 盘中估算涨幅优先，无估值时退回当日确认涨跌幅
  const chg = (h) => (h.est_change_pct != null ? h.est_change_pct : h.change_pct)
  const sortedHoldings = [...holdings].sort((a, b) => {
    const av = chg(a) == null ? -Infinity : chg(a)
    const bv = chg(b) == null ? -Infinity : chg(b)
    return sortDesc ? bv - av : av - bv
  })

  return (
    <div className="stack">
      <Card title="真实仓位" kicker="人工管理">
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.7 }}>
          真实资金仓位，买卖在外部完成；持仓经截图或文本录入，<b>人工核对后入库</b>。
        </div>
      </Card>

      <StatStrip items={items} />

      <Card
        className="wide"
        title="当前持仓"
        right={
          <span className="tag-row">
            <label className="muted" style={{ fontSize: 12 }}>自动刷新</label>
            <select value={autoSec} onChange={(e) => changeAuto(Number(e.target.value))} style={{ fontSize: 12 }}>
              <option value={0}>关闭</option>
              <option value={15}>15 秒</option>
              <option value={30}>30 秒</option>
              <option value={60}>60 秒</option>
            </select>
            {autoSec > 0 && pollOn && !pollOn.active && (
              <span className="muted" style={{ fontSize: 12 }}>已暂停（{pollOn.reason}）</span>
            )}
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={refreshNav}>刷新涨跌幅</button>
            <button className="btn btn-ghost btn-sm" onClick={load}>刷新</button>
          </span>
        }
      >
        {holdings.length === 0 ? (
          <EmptyState title="暂无真实持仓" hint="用下方「导入真实持仓」上传截图或粘贴文本，核对后入库。" />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>基金名称（{holdings.length}）</th>
                <th className="num">估算净值</th>
                <th className="num">持有额</th>
                <th className="num">持有收益</th>
                <th className="num">持有收益率</th>
                <th className="num" style={{ cursor: 'pointer', userSelect: 'none' }} onClick={() => setSortDesc((v) => !v)}>
                  涨跌幅 {sortDesc ? '▽' : '△'}
                </th>
                <th className="num">估算收益</th>
                <th style={{ minWidth: 132 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {sortedHoldings.map((h) => {
                const c = chg(h)
                const estProfit =
                  h.market_amount != null && c != null ? (h.market_amount * c) / 100 : null
                return (
                  <tr key={h.fund_code}>
                    <td>
                      <button className="link-btn" onClick={() => setDetailCode(h.fund_code)}>
                        {h.fund_name || h.fund_code}
                      </button>
                      <div className="muted" style={{ fontSize: 12 }}>{h.fund_code}</div>
                    </td>
                    <td className="num">
                      {h.est_nav != null ? h.est_nav.toFixed(4) : h.nav != null ? h.nav.toFixed(4) : '—'}
                    </td>
                    <td className="num">{h.market_amount != null ? money(h.market_amount) : '—'}</td>
                    <td className={`num ${toneCls(h.profit)}`}>{h.profit != null ? money(h.profit) : '—'}</td>
                    <td className={`num ${toneCls(h.profit_rate)}`}>
                      {h.profit_rate != null ? pct(h.profit_rate * 100) : '—'}
                    </td>
                    <td className={`num ${toneCls(c)}`}>{c != null ? pct(c) : '—'}</td>
                    <td className={`num ${toneCls(estProfit)}`}>{estProfit != null ? money(estProfit) : '—'}</td>
                    <td>
                      <div className="row-actions">
                        <button
                          className={`btn btn-sm${planByCode[h.fund_code] && !planByCode[h.fund_code].enabled ? ' btn-ghost' : ''}`}
                          onClick={() => openPlan(h)}
                        >
                          {planByCode[h.fund_code]
                            ? (planByCode[h.fund_code].enabled ? '定投中' : '已暂停')
                            : '定投'}
                        </button>
                        <button className="btn btn-sm" onClick={() => openEdit(h)}>修改</button>
                        <button className="btn btn-sm btn-danger" onClick={() => del(h)}>删除</button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      <Card className="wide" title="导入真实持仓" kicker={ocrOk ? 'OCR 可用（本地离线）' : 'OCR 未安装，请用文本录入'}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              disabled={!ocrOk || busy}
              onChange={(e) => doImportImage(e.target.files?.[0])}
            />
            <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>上传持仓截图（本地识别，不联网）</span>
          </div>
          <div>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={4}
              placeholder={'或直接粘贴文本，每行一只基金，含 6 位代码与金额，例如：\n华夏成长混合 040001 份额 5000 市值 12345.67 成本 12000'}
              style={{ width: '100%', fontFamily: 'monospace', fontSize: 13 }}
            />
          </div>
          <div className="tag-row">
            <button className="btn btn-primary btn-sm" disabled={busy || !text.trim()} onClick={doImportText}>
              解析文本
            </button>
          </div>
        </div>

        {cand && (
          <div className="sub-card" style={{ marginTop: 14 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
              识别结果（<b>务必逐条核对</b>，OCR 可能出错；确认后才入库）
            </div>
            <table className="table">
              <thead>
                <tr>
                  <th>代码</th><th>名称</th>
                  <th className="num">份额</th><th className="num">成本</th><th className="num">市值</th>
                </tr>
              </thead>
              <tbody>
                {cand.rows.map((r, i) => (
                  <tr key={i}>
                    <td><input value={r.fund_code || ''} onChange={(e) => editRow(i, 'fund_code', e.target.value)} style={{ width: 72 }} /></td>
                    <td><input value={r.fund_name || ''} onChange={(e) => editRow(i, 'fund_name', e.target.value)} style={{ width: 150 }} /></td>
                    <td><input value={r.shares ?? ''} onChange={(e) => editRow(i, 'shares', e.target.value)} style={{ width: 90 }} /></td>
                    <td><input value={r.cost_amount ?? ''} onChange={(e) => editRow(i, 'cost_amount', e.target.value)} style={{ width: 90 }} /></td>
                    <td><input value={r.market_amount ?? ''} onChange={(e) => editRow(i, 'market_amount', e.target.value)} style={{ width: 90 }} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="tag-row" style={{ marginTop: 10 }}>
              <button className="btn btn-primary btn-sm" disabled={busy} onClick={confirm}>确认入库</button>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={discard}>丢弃</button>
            </div>
          </div>
        )}
      </Card>

      <Card
        className="wide"
        title="历史快照"
        right={(
          <button className="btn btn-ghost btn-sm" disabled={!summary} onClick={snap}>生成快照</button>
        )}
      >
        {snapshots.length === 0 ? (
          <EmptyState title="暂无快照" hint="导入确认或点「生成快照」后会记录当日总览" />
        ) : (
          <table className="table">
            <thead>
              <tr><th>日期</th><th className="num">市值</th><th className="num">成本</th><th className="num">盈亏</th><th className="num">持仓</th></tr>
            </thead>
            <tbody>
              {snapshots.map((s) => (
                <tr key={s.snap_date}>
                  <td>{date(s.snap_date)}</td>
                  <td className="num">{money(s.total_market)}</td>
                  <td className="num">{money(s.total_cost)}</td>
                  <td className={`num ${s.total_profit > 0 ? 'tone-up' : s.total_profit < 0 ? 'tone-down' : ''}`}>{money(s.total_profit)}</td>
                  <td className="num">{s.holdings_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Modal
        open={!!planFor}
        title={planFor?.plan ? '定投计划' : '新建定投计划'}
        onClose={() => setPlanFor(null)}
        width={460}
      >
        {planFor && (
          <div className="stack" style={{ gap: 12 }}>
            <div className="muted" style={{ fontSize: 13 }}>
              {planFor.name}（{planFor.code}）
            </div>
            <div className="field">
              <label>频率</label>
              <select
                value={planForm.frequency}
                onChange={(e) => setPlanForm((f) => ({ ...f, frequency: e.target.value }))}
              >
                <option value="weekly">每周</option>
                <option value="monthly">每月</option>
              </select>
            </div>
            <div className="field">
              <label>每期金额（元）</label>
              <input
                type="number" min="1" step="1" value={planForm.amount} data-autofocus
                onChange={(e) => setPlanForm((f) => ({ ...f, amount: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>扣款日</label>
              {planForm.frequency === 'weekly' ? (
                <select
                  value={planForm.weekday}
                  onChange={(e) => setPlanForm((f) => ({ ...f, weekday: e.target.value }))}
                >
                  <option value="0">周一</option>
                  <option value="1">周二</option>
                  <option value="2">周三</option>
                  <option value="3">周四</option>
                  <option value="4">周五</option>
                </select>
              ) : (
                <select
                  value={planForm.day_of_month}
                  onChange={(e) => setPlanForm((f) => ({ ...f, day_of_month: e.target.value }))}
                >
                  {Array.from({ length: 28 }, (_, i) => (
                    <option key={i + 1} value={i + 1}>{i + 1} 号</option>
                  ))}
                </select>
              )}
            </div>
            {planFor.plan && (
              <div className="tag-row">
                <button className="btn btn-sm" onClick={togglePlan}>
                  {planFor.plan.enabled ? '暂停计划' : '恢复计划'}
                </button>
                <button className="btn btn-sm btn-danger" onClick={() => setPlanDel(planFor.plan)}>
                  删除计划
                </button>
              </div>
            )}
            <button className="btn btn-primary" style={{ width: '100%' }} disabled={saving} onClick={submitPlan}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={!!planDel}
        title="删除定投计划"
        message={planDel
          ? `将删除「${planDel.fund_name || planDel.fund_code}」的定投计划，已持仓份额不受影响。确定删除？`
          : ''}
        danger
        onConfirm={confirmDeletePlan}
        onClose={() => setPlanDel(null)}
      />

      <Modal open={!!editFor} title="修改持仓" onClose={() => setEditFor(null)} width={460}>
        {editFor && (
          <div className="stack" style={{ gap: 12 }}>
            <div className="muted" style={{ fontSize: 13 }}>{editFor.fund_code}</div>
            <div className="field">
              <label>基金名称</label>
              <input
                value={editForm.fund_name} data-autofocus
                onChange={(e) => setEditForm((f) => ({ ...f, fund_name: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>持有份额</label>
              <input
                type="number" min="0" step="0.0001" value={editForm.shares}
                onChange={(e) => setEditForm((f) => ({ ...f, shares: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>成本金额（元）</label>
              <input
                type="number" min="0" step="0.01" value={editForm.cost_amount}
                onChange={(e) => setEditForm((f) => ({ ...f, cost_amount: e.target.value }))}
              />
            </div>
            <div className="field">
              <label>市值（元）</label>
              <input
                type="number" min="0" step="0.01" value={editForm.market_amount}
                onChange={(e) => setEditForm((f) => ({ ...f, market_amount: e.target.value }))}
              />
            </div>
            <button className="btn btn-primary" style={{ width: '100%' }} disabled={saving} onClick={submitEdit}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={!!delFor}
        title="删除真实持仓"
        message={delFor
          ? `将删除「${delFor.name}」（${delFor.code}）的持仓记录。\n外部账户不受影响；删后需要重新导入才能恢复。确定删除？`
          : ''}
        danger
        confirmText="确认删除"
        onConfirm={confirmDelete}
        onClose={() => setDelFor(null)}
      />

      <RealFundModal open={!!detailCode} fundCode={detailCode} onClose={() => setDetailCode(null)} />
    </div>
  )
}
