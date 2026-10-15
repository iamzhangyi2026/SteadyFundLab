import { useEffect, useMemo, useState } from 'react'
import { apiGet } from '../store/api'
import Modal from './Modal'
import NetValueChart from './NetValueChart'
import EmptyState from './EmptyState'
import { money, pct, cls, date } from '../lib/format'
import { psClass } from '../lib/purchaseStatus'
import { drawdownRecovery, dingtouBacktest, FREQ_LABEL, WINDOW_LABEL } from '../lib/navlab'

// 基金详情弹窗：净值曲线 + 核心指标 + 回撤修复 + 定投回测。
// 对标支付宝基金详情页里评分最高的两项能力——回撤修复（★★★★）与定投收益（★★★），
// 但补上它的黑榜短板：支付宝把「夏普比率」这类术语直接摆在标签上、
// 解释藏进抽屉弹窗，小白看不懂就划走了。这里每个指标都能就地看懂。

// 指标词典：术语必须能就地看懂
const METRIC_HELP = {
  accNav: '累计净值：把历史分红重新投入后的净值，反映长期真实收益。',
  ret: '对应时间段内的涨幅，如「近1年 +12.30%」＝一年前持有至今的收益。',
  mdd: '最大回撤：历史最高点跌到最低点的最深跌幅。',
  vol: '年化波动率：净值波动的剧烈程度，按年折算。',
  hist: '本库内该基金的净值天数；天数越长，统计越可信。',
}

// 回撤和波动是风险指标，不是当期涨跌——
// 沿用既有约定：不用红绿，否则「跌」的绿色会被读成「安全」。
function StatMini({ label, v, t, helpKey, help, onHelp }) {
  return (
    <div className="tile" style={{ padding: '11px 13px' }}>
      <div className="tile-label" style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <span>{label}</span>
        {helpKey && (
          <button
            type="button"
            onClick={() => onHelp(helpKey)}
            title="这个指标是什么意思？"
            aria-label={`${label} 指标说明`}
            style={{
              width: 15,
              height: 15,
              padding: 0,
              borderRadius: '50%',
              border: '1px solid var(--border, #d7dbe3)',
              background: help === helpKey ? 'var(--accent, #3b6ef6)' : 'transparent',
              color: help === helpKey ? '#fff' : 'var(--text-mute, #5b6675)',
              fontSize: 10,
              lineHeight: 1,
              cursor: 'pointer',
              flex: '0 0 auto',
            }}
          >
            ?
          </button>
        )}
      </div>
      <div
        className={`tile-value status-num ${t === 'up' ? 'tone-up' : t === 'down' ? 'tone-down' : ''}`}
        style={{ fontSize: 'var(--fs-tile, 17px)' }}
      >
        {v}
      </div>
    </div>
  )
}

// 回撤修复：直面负面数据，回答「最深跌多少 / 跌了多久 / 多久才回本」
function DrawdownCard({ dd }) {
  if (!dd) return null
  const title = (
    <div style={{ fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'baseline', gap: 7, flexWrap: 'wrap' }}>
      回撤修复
      <span className="muted cell-note">历史上最深的那次下跌</span>
    </div>
  )

  if (dd.flat) {
    return (
      <div className="sub-card" style={{ marginTop: 12 }}>
        {title}
        <div className="muted cell-note" style={{ lineHeight: 1.7 }}>
          这段历史里净值一路上行，没有出现明显回撤。
        </div>
      </div>
    )
  }

  return (
    <div className="sub-card" style={{ marginTop: 12 }}>
      {title}
      <div className="tile-grid">
        <StatMini label="最深跌幅" v={pct(dd.maxDrawdown)} helpKey="mdd" help={null} onHelp={() => {}} />
        <StatMini label="下跌耗时" v={dd.declineDays != null ? `${dd.declineDays} 天` : '—'} />
        <StatMini
          label={dd.recovered ? '修复耗时' : '未修复时长'}
          v={dd.recovered ? `${dd.recoveryDays} 天` : dd.pendingDays != null ? `${dd.pendingDays} 天` : '—'}
        />
      </div>
      <div className="muted cell-note" style={{ marginTop: 9, lineHeight: 1.75 }}>
        {date(dd.peakDate)} 高点 <b>{dd.peakValue}</b> → {date(dd.troughDate)} 低点 <b>{dd.troughValue}</b>；
        {dd.recovered ? (
          <>
            之后用了 <b>{dd.recoveryDays} 天</b>，在 {date(dd.recoveryDate)} 涨回原位；整轮从峰顶到收复共{' '}
            <b>{dd.roundTripDays} 天</b>。
          </>
        ) : (
          <>
            截至 {date(dd.lastDate)}（最新 {dd.lastValue}）仍未收复，从谷底算起已经熬了{' '}
            <b>{dd.pendingDays} 天</b>。
          </>
        )}
      </div>
    </div>
  )
}

// 定投回测：模拟定期定额买入，并与同期一把梭对比
function BacktestCard({ series }) {
  const [amount, setAmount] = useState(500)
  const [freq, setFreq] = useState('monthly')
  const [win, setWin] = useState(730)

  const r = useMemo(
    () => dingtouBacktest(series, { amount: Number(amount) || 0, frequency: freq, windowDays: Number(win) }),
    [series, amount, freq, win],
  )

  return (
    <div className="sub-card" style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'baseline', gap: 7, flexWrap: 'wrap' }}>
        定投回测
        <span className="muted cell-note">用真实净值模拟，和一把梭对比</span>
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
        <div className="field" style={{ flex: '1 1 120px', margin: 0 }}>
          <label>每期金额（元）</label>
          <input type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="500" />
        </div>
        <div className="field" style={{ flex: '1 1 110px', margin: 0 }}>
          <label>频率</label>
          <select value={freq} onChange={(e) => setFreq(e.target.value)}>
            {Object.keys(FREQ_LABEL).map((k) => (
              <option key={k} value={k}>{FREQ_LABEL[k]}</option>
            ))}
          </select>
        </div>
        <div className="field" style={{ flex: '1 1 110px', margin: 0 }}>
          <label>回看区间</label>
          <select value={String(win)} onChange={(e) => setWin(e.target.value)}>
            {Object.keys(WINDOW_LABEL).map((k) => (
              <option key={k} value={k}>{WINDOW_LABEL[k]}</option>
            ))}
          </select>
        </div>
      </div>

      {!r ? (
        <div className="muted cell-note">这段区间的净值数据不够，换个更长的区间试试。</div>
      ) : (
        <>
          <div className="tile-grid">
            <StatMini label="定投期数" v={`${r.periods} 期`} />
            <StatMini label="累计投入" v={money(r.invest, 0)} />
            <StatMini label="期末市值" v={money(r.marketValue, 0)} />
            <StatMini label="定投收益率" v={pct(r.roi)} t={cls(r.roi)} />
            <StatMini label="一把梭收益率" v={pct(r.lumpRoi)} t={cls(r.lumpRoi)} />
            <StatMini
              label="对比一把梭"
              v={pct(r.edge)}
              t={cls(r.edge)}
            />
          </div>
          <div className="muted cell-note" style={{ marginTop: 9, lineHeight: 1.75 }}>
            {date(r.startDate)} 至 {date(r.endDate)}（{r.days} 天，净值 {r.startValue} → {r.endValue}）：
            按 <b>{FREQ_LABEL[freq]}</b> 投 <b>{money(Number(amount) || 0, 0)}</b>，共 {r.periods} 期、
            投入 {money(r.invest, 0)}，期末 {money(r.marketValue, 0)}；
            同样多的钱在期初一把梭，期末是 {money(r.lumpValue, 0)}。
            <br />
            <span style={{ opacity: 0.85 }}>
              口径：两者都用「期末市值 ÷ 总投入」算总回报。定投把成本摊到不同价位，
              遇上先跌后涨或来回震荡的行情更能摊薄成本；但若是先涨后跌，
              定投会在高位买到更多份额，反而不如期初一把梭。
              这里没有绝对优劣——要看这段区间的净值怎么走，上面的数字就是答案。
            </span>
          </div>
        </>
      )}
    </div>
  )
}

export default function FundDetailModal({ open, code, name, onClose, onBuy, onSell }) {
  const [nav, setNav] = useState(null)
  const [loading, setLoading] = useState(false)
  const [help, setHelp] = useState(null)

  useEffect(() => {
    if (!open || !code) return
    let alive = true
    setLoading(true)
    setNav(null)
    setHelp(null)
    apiGet(`/api/fund/${code}/nav`)
      .then((d) => { if (alive) setNav(d) })
      .catch(() => { if (alive) setNav(null) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, code])

  const series = nav?.series || []
  const stats = nav?.stats || {}
  const dd = useMemo(() => drawdownRecovery(series), [series])

  // 申购状态：详情页必须标（暂停/限大额随时变），直接读 nav 接口挂的字段（后端按 code 取全市场购买表）
  const ps = nav?.purchase_status
  const paused = ps === '暂停申购'
  const limited = ps === '限大额'

  const toggleHelp = (k) => setHelp((h) => (h === k ? null : k))

  return (
    <Modal open={open} title={`${name || code || ''}${code ? `（${code}）` : ''}`} onClose={onClose} width={760}>
      {loading && <div className="loading">加载净值数据…</div>}
      {!loading && !nav && (
        <EmptyState title="暂无净值数据" hint="这只基金还没有入库的历史净值，先在「交易」页添加并抓取" />
      )}
      {!loading && nav && (
        <div>
          <NetValueChart
            categories={series.map((d) => d.date)}
            values={series.map((d) => d.acc_nav)}
            height={230}
            label={`${name || code} 累计净值曲线`}
          />

          {ps && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
              <span className={`tag ${psClass(ps)}`}>
                {ps}
              </span>
              {limited && nav?.purchase_limit != null && (
                <span style={{ color: '#8a6d00', fontWeight: 600, fontSize: 13 }}>单日上限 ¥{nav.purchase_limit}</span>
              )}
              {paused && <span style={{ color: '#c62828', fontWeight: 600, fontSize: 13 }}>暂不可买入</span>}
              {nav?.redeem_status && nav.redeem_status !== '开放赎回' && (
                <span className="muted cell-note">赎回：{nav.redeem_status}</span>
              )}
            </div>
          )}

          <div className="tile-grid" style={{ marginTop: 12 }}>
            <StatMini label="最新累计净值" v={stats.latest_acc_nav ?? '—'} helpKey="accNav" help={help} onHelp={toggleHelp} />
            <StatMini label="近1月" v={pct(stats.ret_1m)} t={cls(stats.ret_1m)} helpKey="ret" help={help} onHelp={toggleHelp} />
            <StatMini label="近3月" v={pct(stats.ret_3m)} t={cls(stats.ret_3m)} />
            <StatMini label="近6月" v={pct(stats.ret_6m)} t={cls(stats.ret_6m)} />
            <StatMini label="近1年" v={pct(stats.ret_1y)} t={cls(stats.ret_1y)} />
            <StatMini label="最大回撤" v={pct(stats.max_drawdown)} helpKey="mdd" help={help} onHelp={toggleHelp} />
            <StatMini label="年化波动" v={pct(stats.annual_vol)} helpKey="vol" help={help} onHelp={toggleHelp} />
            <StatMini label="历史长度" v={stats.history_days != null ? `${stats.history_days} 天` : '—'} helpKey="hist" help={help} onHelp={toggleHelp} />
            <StatMini label="数据截至" v={date(stats.latest_date)} />
          </div>
          {help && (
            <div
              className="sub-card"
              style={{ marginTop: 8, background: 'var(--accent-soft, #eef3fe)', lineHeight: 1.75 }}
            >
              <b style={{ marginRight: 6 }}>指标说明</b>
              {METRIC_HELP[help]}
            </div>
          )}

          <DrawdownCard dd={dd} />
          <BacktestCard series={series} />

          <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
            <button className="btn btn-primary" onClick={() => onBuy && onBuy()} disabled={paused}>
              {paused ? '暂停申购·不可买入' : '手动买入'}
            </button>
            <button className="btn btn-danger" onClick={() => onSell && onSell()}>
              手动赎回
            </button>
          </div>
          {!paused && limited && nav?.purchase_limit != null && (
            <div className="muted cell-note" style={{ color: '#8a6d00', marginTop: 4 }}>
              限大额：单日最多买入 {money(nav.purchase_limit)}。
            </div>
          )}
          {paused && (
            <div className="muted cell-note" style={{ color: '#c62828', marginTop: 4 }}>
              ⚠ 该基金当前「暂停申购」，暂不可买入（赎回不受影响）。
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
