// 净值实验室：基于真实历史净值序列，纯前端计算「回撤修复」与「定投回测」。
// 数据源 = /api/fund/{code}/nav 的 series（按日期升序，元素 {date, nav, acc_nav}）。
// 设计由来：对齐支付宝基金详情页里评分最高的两个能力——
//   ① 回撤修复（★★★★，直面负面数据，回答「亏了多少、多久能回本」）
//   ② 定投收益（★★★，对比不同投入方式的收益率）
// 但口径必须在界面上写清楚，避免像支付宝那样「术语摆着、用户看不懂」的黑榜问题。

const DAY = 24 * 60 * 60 * 1000

// 取有效净值点：优先累计净值（已把分红加回，衡量真实收益更公平），缺失时回退单位净值
function points(series) {
  return (series || [])
    .map((r) => {
      if (!r || !r.date) return null
      const v = r.acc_nav != null ? r.acc_nav : r.nav
      return v != null && v > 0 ? { date: r.date, v } : null
    })
    .filter(Boolean)
}

// 两个日期之间的自然日天数
function dayDiff(a, b) {
  const t1 = new Date(a).getTime()
  const t2 = new Date(b).getTime()
  if (Number.isNaN(t1) || Number.isNaN(t2)) return null
  return Math.round((t2 - t1) / DAY)
}

// ---------------------------------------------------------------------------
// 回撤修复：定位史上最深那次回撤，回答「最深跌了多少、跌了多久、多久才涨回来」
// ---------------------------------------------------------------------------
export function drawdownRecovery(series) {
  const pts = points(series)
  if (pts.length < 2) return null

  let peak = pts[0].v
  let peakIdx = 0
  let mdd = 0 // 最大回撤（负值，越小越深）
  let troughIdx = 0
  let mddPeakIdx = 0

  for (let i = 0; i < pts.length; i++) {
    const v = pts[i].v
    if (v > peak) {
      peak = v
      peakIdx = i
    }
    const dd = peak ? v / peak - 1 : 0
    if (dd < mdd) {
      mdd = dd
      troughIdx = i
      mddPeakIdx = peakIdx
    }
  }

  const peakPt = pts[mddPeakIdx]
  const troughPt = pts[troughIdx]
  const lastPt = pts[pts.length - 1]

  // 从未发生过回撤（一路向上）：mdd 保持 0
  const flat = mdd >= -0.0001

  // 修复：从谷值往后找第一个回到（或超过）当时峰值的交易日
  let recoveryIdx = -1
  if (!flat) {
    for (let i = troughIdx + 1; i < pts.length; i++) {
      if (pts[i].v >= peakPt.v) {
        recoveryIdx = i
        break
      }
    }
  }

  const recovered = recoveryIdx >= 0
  const recoveryPt = recovered ? pts[recoveryIdx] : null

  return {
    flat,
    maxDrawdown: +(mdd * 100).toFixed(2), // 负数百分比，如 -32.15 表示最深跌 32.15%
    peakDate: peakPt.date,
    peakValue: +peakPt.v.toFixed(4),
    troughDate: troughPt.date,
    troughValue: +troughPt.v.toFixed(4),
    declineDays: flat ? 0 : dayDiff(peakPt.date, troughPt.date),
    recovered,
    recoveryDate: recoveryPt ? recoveryPt.date : null,
    recoveryDays: recoveryPt ? dayDiff(troughPt.date, recoveryPt.date) : null,
    // 未修复时：从谷底到今天已经熬了多少天
    pendingDays: !flat && !recovered ? dayDiff(troughPt.date, lastPt.date) : null,
    // 完整一轮「从峰顶跌下去再涨回来」耗时
    roundTripDays: recovered ? dayDiff(peakPt.date, recoveryPt.date) : null,
    lastDate: lastPt.date,
    lastValue: +lastPt.v.toFixed(4),
  }
}

// ---------------------------------------------------------------------------
// 定投回测：模拟「每期固定金额买入」，并与「同期一把梭」对比收益率
// ---------------------------------------------------------------------------
// 净值只按交易日记录，所以用交易日步进模拟扣款节奏，比按自然日更贴合真实定投
export const FREQ_STEP = { weekly: 5, biweekly: 10, monthly: 21 }
export const FREQ_LABEL = { weekly: '每周', biweekly: '每两周', monthly: '每月' }
export const WINDOW_LABEL = { 365: '近 1 年', 730: '近 2 年', 1095: '近 3 年', 0: '全部历史' }

export function dingtouBacktest(series, { amount = 500, frequency = 'monthly', windowDays = 0 } = {}) {
  const all = points(series)
  if (all.length < 2) return null

  let pts = all
  if (windowDays && windowDays > 0) {
    const last = new Date(all[all.length - 1].date).getTime()
    const start = last - windowDays * DAY
    const sliced = all.filter((p) => new Date(p.date).getTime() >= start)
    // 窗口内数据太少时退回全量，避免算出来 1 期、没有参考价值
    if (sliced.length >= 30) pts = sliced
  }

  const step = FREQ_STEP[frequency] || FREQ_STEP.monthly
  const per = Number(amount) || 0
  if (per <= 0) return null

  let shares = 0
  let invest = 0
  let periods = 0
  for (let i = 0; i < pts.length; i += step) {
    shares += per / pts[i].v
    invest += per
    periods += 1
  }
  if (periods === 0) return null

  const firstV = pts[0].v
  const lastV = pts[pts.length - 1].v

  // 定投：分批买入，期末一次性算总账
  const marketValue = shares * lastV
  const profit = marketValue - invest
  const roi = (marketValue / invest - 1) * 100

  // 对照：期初一把梭投入同样多的钱
  const lumpValue = (invest / firstV) * lastV
  const lumpRoi = (lumpValue / invest - 1) * 100

  return {
    periods,
    invest: +invest.toFixed(2),
    marketValue: +marketValue.toFixed(2),
    profit: +profit.toFixed(2),
    roi: +roi.toFixed(2),
    lumpValue: +lumpValue.toFixed(2),
    lumpRoi: +lumpRoi.toFixed(2),
    startDate: pts[0].date,
    endDate: pts[pts.length - 1].date,
    startValue: +firstV.toFixed(4),
    endValue: +lastV.toFixed(4),
    days: dayDiff(pts[0].date, pts[pts.length - 1].date),
    // 定投相对一把梭的收益差（正数=定投更划算）
    edge: +(roi - lumpRoi).toFixed(2),
  }
}
