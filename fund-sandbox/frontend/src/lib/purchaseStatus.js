// 申购状态 → 语义色：可买=ok(teal) / 受限=warn(黄) / 买不了=danger(红) / 未知=neutral(灰)。
// 集中维护，避免 Trade.jsx / Positions.jsx / FundDetailModal 各写一份导致配色漂移。
// 注意：开放申购不用「涨红/跌绿」，是为了和净值涨跌的语义色区分开（同屏撞色会误读）。
export const PS_MAP = { 开放申购: 'ok', 限大额: 'warn', 暂停申购: 'danger', 封闭期: 'danger' }

export const psClass = (s) => PS_MAP[s] || 'neutral'
