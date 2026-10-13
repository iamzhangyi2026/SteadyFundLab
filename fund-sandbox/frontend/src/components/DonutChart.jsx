import ReactECharts from 'echarts-for-react'
import EmptyState from './EmptyState'

// 环形图（资产配置 / 行业暴露共用）。data = [{ name, value }]。
// unit: '' 原样显示（如权重百分比）；'money' 按金额带 ¥ 与千分位。
// 配色为浅色主题下调过的柔和 10 色，循环取模。
const PALETTE = [
  '#3b6ea5', '#4fa3a5', '#5c9e6e', '#c99a3f', '#7a6bb5',
  '#4a7fb5', '#c1784f', '#b55c7e', '#6b9e4f', '#8a8f9c',
]

export default function DonutChart({ data = [], height = 240, unit = '', label = '占比环形图', centerLabel, centerValue }) {
  if (!data.length) return <EmptyState title="暂无数据" />

  const total = data.reduce((s, d) => s + (d.value || 0), 0)
  const fmt = (v) =>
    unit === 'money'
      ? '¥' + Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : `${v}${unit}`

  const option = {
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(255,255,255,0.82)',
      borderColor: 'rgba(15,23,42,0.1)',
      borderWidth: 1,
      textStyle: { color: '#1f2430', fontSize: 12 },
      extraCssText:
        'backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.22); padding: 8px 12px;',
      formatter: (p) => {
        const share = total ? ((p.value / total) * 100).toFixed(2) : '0.00'
        return `${p.name}<br/>${fmt(p.value)}（${share}%）`
      },
    },
    legend: {
      type: 'scroll',
      orient: 'vertical',
      right: 6,
      top: 'middle',
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 9,
      // 图例是看懂环形图的唯一依据，文字用 --text-soft 同色保证可读
      // 行业名很长（如「电力、热力、燃气及水生产和供应业」17 字），
      // 不限宽时图例左缘会侵入圆环区域 → 实测反馈「字压住圆圈」。
      // 限宽后图例宽度固定，不再随名字长度挤压圆环；完整名称见 tooltip / 卡片内 TOP 行业列表。
      textStyle: {
        color: '#4a5160',
        fontSize: 12,
        width: 140,
        overflow: 'truncate',
      },
    },
    series: [
      {
        type: 'pie',
        // center 与 app.css 的 `.donut-center { left }` 是硬编码耦合：
        // 调整此处 radius / center 时须同步改 CSS，否则中心文字会偏离圆环。
        radius: ['38%', '54%'],
        center: ['32%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        labelLine: { show: false },
        emphasis: {
          scaleSize: 6,
          itemStyle: { shadowBlur: 12, shadowColor: 'rgba(0,0,0,0.18)' },
        },
        data: data.map((d, i) => ({
          name: d.name,
          value: d.value,
          itemStyle: { color: PALETTE[i % PALETTE.length] },
        })),
      },
    ],
  }

  const top = [...data].sort((a, b) => (b.value || 0) - (a.value || 0))[0]
  const showCenter = centerLabel || centerValue != null
  return (
    <div
      role="img"
      aria-label={`${label}，共 ${data.length} 项${top ? `，占比最高为 ${top.name}` : ''}`}
      style={{ position: 'relative' }}
    >
      <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />
      {showCenter && (
        <div className="donut-center" aria-hidden="true">
          {centerValue != null && <div className="donut-center-value">{fmt(centerValue)}</div>}
          {centerLabel && <div className="donut-center-label">{centerLabel}</div>}
        </div>
      )}
    </div>
  )
}
