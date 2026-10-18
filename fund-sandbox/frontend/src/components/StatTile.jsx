import AnimatedNumber from './AnimatedNumber'

// 指标瓦片。tone 控制数值颜色：'up'=涨红 'down'=跌绿（遵循 A 股惯例）。
// 传 num + format 时数值以滚动动画呈现（F1/11）；只传 value 时静态显示。
export default function StatTile({ label, value, sub, tone, num, format }) {
  const cls = tone ? `tone-${tone}` : ''
  const inner =
    num != null && format ? (
      <AnimatedNumber value={num} format={format} className={cls} />
    ) : (
      <span className={cls}>{value}</span>
    )
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value status-num">{inner}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  )
}
