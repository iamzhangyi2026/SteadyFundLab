import { useEffect, useState } from 'react'
import Modal from './Modal'
import StatStrip from './StatStrip'
import NetValueChart from './NetValueChart'
import DeepAnalysis from './DeepAnalysis'
import { money, pct } from '../lib/format'

// B 区单只基金详情弹窗：持仓 + 指标 + 净值曲线 + 深度分析。
// 数据来自 /api/real/holdings/{code}/detail（B 区独立抓行情 + **代码规则**生成六段式分析）。
const API = '/api/real'

const toneOf = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : undefined)

export default function RealFundModal({ open, fundCode, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open || !fundCode) return undefined
    let alive = true
    setLoading(true)
    setError('')
    setData(null)
    fetch(`${API}/holdings/${fundCode}/detail`)
      .then((r) => r.json().then((b) => ({ ok: r.ok, b })))
      .then(({ ok, b }) => {
        if (!alive) return
        if (!ok) throw new Error(b.detail || '加载失败')
        setData(b)
      })
      .catch((e) => { if (alive) setError(e.message) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, fundCode])

  const h = data?.holding
  const m = data?.metrics || {}
  const series = data?.series || []

  return (
    <Modal
      open={open}
      title={h ? `${h.fund_name || ''} ${h.fund_code}` : fundCode || '基金详情'}
      onClose={onClose}
      width="760px"
    >
      {loading && <div className="muted" style={{ padding: '28px 0', textAlign: 'center' }}>加载中…</div>}
      {!loading && error && (
        <div className="muted" style={{ padding: '28px 0', textAlign: 'center' }}>加载失败：{error}</div>
      )}
      {!loading && !error && data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {h && (
            <StatStrip
              items={[
                { label: '份额', value: h.shares ?? '—' },
                { label: '成本', value: money(h.cost_amount) },
                { label: '市值', value: money(h.market_amount) },
                { label: '涨跌幅', value: h.change_pct != null ? pct(h.change_pct) : '—', tone: toneOf(h.change_pct) },
                { label: '盈亏', value: money(h.profit), tone: toneOf(h.profit) },
              ]}
            />
          )}

          {data.ok ? (
            <>
              <StatStrip
                items={[
                  { label: '最新净值', value: m.nav ?? '—', sub: m.nav_date },
                  { label: '近1月', value: m.ret_1m != null ? pct(m.ret_1m) : '—', tone: toneOf(m.ret_1m) },
                  { label: '近3月', value: m.ret_3m != null ? pct(m.ret_3m) : '—', tone: toneOf(m.ret_3m) },
                  { label: '近6月', value: m.ret_6m != null ? pct(m.ret_6m) : '—', tone: toneOf(m.ret_6m) },
                  { label: '近1年', value: m.ret_1y != null ? pct(m.ret_1y) : '—', tone: toneOf(m.ret_1y) },
                  { label: '最大回撤', value: m.max_drawdown != null ? `${m.max_drawdown.toFixed(2)}%` : '—' },
                  { label: '年化波动', value: m.vol != null ? `${m.vol.toFixed(2)}%` : '—' },
                ]}
              />
              <NetValueChart
                categories={series.map((s) => s.date)}
                values={series.map((s) => s.nav)}
                height={220}
              />
              {/* 深度分析：按 **SOP Step 7 六段式**呈现（结论 / 数据 / 催化 / 分化 / 信号 / 建议）。
                  注意：这里**不是 AI 生成**，而是后端用代码规则算出来的（B 区为隔离区，
                  不调外部大模型），所以标题就叫「深度分析」，不挂 AI 名头。
                  本区拿不到的段落（如无资讯源的催化事件）如实显示说明文字，不编造。 */}
              <DeepAnalysis sections={data.sections} title="深度分析" />
              {!data.sections?.conclusion && (
                <div className="sub-card">
                  <div className="muted" style={{ marginBottom: 6 }}>深度分析</div>
                  <div style={{ lineHeight: 1.8, fontSize: 13, whiteSpace: 'pre-line' }}>{data.analysis}</div>
                </div>
              )}
            </>
          ) : (
            <div className="muted">{data.error || '暂无分析数据'}</div>
          )}
        </div>
      )}
    </Modal>
  )
}
