// 轻量环形进度（SVG，stroke-dashoffset 驱动）。用于定投「距下次执行」可视化。
// 浅色主题：轨道浅灰、进度用主题强调色；中心可放短文本（如剩余天数）。
export default function ProgressRing({
  value = 0,
  size = 44,
  stroke = 5,
  color = 'var(--accent, #3b6ea5)',
  trackColor = '#eef0f4',
  center,
  label = '进度',
}) {
  const v = Math.min(1, Math.max(0, Number(value) || 0))
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const offset = c * (1 - v)
  const cx = size / 2
  const pctText = `${Math.round(v * 100)}%`
  return (
    // 纯 SVG 对读屏软件等于一张无名图片，补上 progressbar 语义与当前值
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ flex: '0 0 auto' }}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(v * 100)}
      aria-valuetext={center != null ? `${center}（${pctText}）` : pctText}
      aria-label={label}
    >
      <circle cx={cx} cy={cx} r={r} fill="none" stroke={trackColor} strokeWidth={stroke} />
      <circle
        cx={cx}
        cy={cx}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={offset}
        transform={`rotate(-90 ${cx} ${cx})`}
        style={{ transition: 'stroke-dashoffset var(--dur) var(--ease-out)' }}
      />
      {center != null && (
        <text
          x="50%"
          y="50%"
          textAnchor="middle"
          dominantBaseline="central"
          fontSize={13}
          fontWeight={700}
          fill="#4a5160"
        >
          {center}
        </text>
      )}
    </svg>
  )
}
