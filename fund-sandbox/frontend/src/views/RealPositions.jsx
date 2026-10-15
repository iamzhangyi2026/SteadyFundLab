import { useCallback, useEffect, useRef, useState } from 'react'
import { useStore } from '../store/useStore'
import Card from '../components/Card'
import EmptyState from '../components/EmptyState'
import StatStrip from '../components/StatStrip'
import RealFundModal from '../components/RealFundModal'
import { money, pct, date } from '../lib/format'

// B 区（真实仓位 · 人接管 · 隔离区）。
// 本页自带 fetch，不进 A 区共享 store——两区互不干扰。
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

  const del = async (code) => {
    try {
      await j(`${API}/holdings/${code}`, { method: 'DELETE' })
      notify('已删除 ' + code, 'success')
      load()
    } catch (e) {
      notify('删除失败：' + e.message, 'error')
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
      <Card title="真实仓位" kicker="人接管 · 隔离区（AI 只读）">
        <div className="muted" style={{ fontSize: 13, lineHeight: 1.7 }}>
          <b>真实资金</b>仓位，与虚拟沙盒隔离。买卖在外部完成，系统不接交易通道；
          持仓经截图或文本录入，<b>人工核对后入库</b>。
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
                <th></th>
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
                    <td><button className="link-btn" onClick={() => del(h.fund_code)}>删除</button></td>
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
              placeholder={'或直接粘贴文本，每行一只基金，含 6 位代码与金额，例如：\n易方达蓝筹精选混合 005827 份额 5000 市值 12345.67 成本 12000'}
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

      <RealFundModal open={!!detailCode} fundCode={detailCode} onClose={() => setDetailCode(null)} />
    </div>
  )
}
