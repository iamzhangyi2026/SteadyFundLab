// 统计小卡：仿运维大屏顶部「列车 1804 / 12 累计公里数」一排指标卡。
// 每卡 = 标签 + 大数字（可带语义色 tone）+ 可选副说明。
// 浅色主题、网格自适应换行；置于 .stack 内传 className="wide" 即占满整行。
export default function StatStrip({ items = [], className = '' }) {
  if (!items.length) return null
  return (
    <div className={`stat-strip ${className}`}>
      {items.map((it, i) => (
        <div className="stat-chip" key={i}>
          <div className="stat-label">{it.label}</div>
          <div className={`stat-value status-num ${it.tone ? 'tone-' + it.tone : ''}`}>{it.value}</div>
          {it.sub && <div className="stat-sub">{it.sub}</div>}
        </div>
      ))}
    </div>
  )
}
