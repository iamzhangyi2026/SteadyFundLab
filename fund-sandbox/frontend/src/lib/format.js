// 金额 / 百分比 / 日期 格式化，全站统一口径。
export const money = (n, d = 2) =>
  n == null || Number.isNaN(Number(n))
    ? '—'
    : '¥' + Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })

export const pct = (n) =>
  n == null || Number.isNaN(Number(n))
    ? '—'
    : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'

// 收益正负 → 涨跌色（涨红跌绿）
export const tone = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : undefined)

export const date = (s) => (s ? String(s).slice(0, 10) : '—')
export const time = (s) => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
export const cls = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : 'neutral')
