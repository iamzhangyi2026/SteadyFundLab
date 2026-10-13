import ReactECharts from 'echarts-for-react'
import EmptyState from './EmptyState'

// 通用净值/资产曲线图。categories=横轴标签，values=纵轴数值。
// 滚轮缩放 + 拖拽平移、玻璃态 tooltip、净值线轻微发光。
// 轴文字统一 #5b6675 / 12px，对比度满足 AA，日期和刻度清晰可读。
const AXIS_TEXT = '#5b6675'

export default function NetValueChart({
  categories = [],
  values = [],
  height = 260,
  color = '#3b6ea5',
  label = '净值曲线',
}) {
  if (!categories.length || !values.length) {
    return <EmptyState title="暂无曲线数据" hint="净值入库后自动出图" />
  }
  // 把 #rrggbb 转 rgba
  const rgba = (hex, a) => {
    const m = hex.replace('#', '')
    const n = parseInt(m.length === 3 ? m.split('').map((c) => c + c).join('') : m, 16)
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
  }
  const option = {
    grid: { left: 52, right: 18, top: 14, bottom: 40 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(255,255,255,0.82)',
      borderColor: 'rgba(15,23,42,0.1)',
      borderWidth: 1,
      textStyle: { color: '#1f2430', fontSize: 13 },
      extraCssText:
        'backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.22); padding: 8px 12px;',
    },
    xAxis: {
      type: 'category',
      data: categories,
      boundaryGap: false,
      axisLabel: { color: AXIS_TEXT, fontSize: 12, hideOverlap: true },
      axisLine: { lineStyle: { color: '#e6e8ee' } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: AXIS_TEXT, fontSize: 12 },
      splitLine: { lineStyle: { color: '#eef0f4' } },
    },
    dataZoom: [
      {
        type: 'inside',
        filterMode: 'none',
        zoomOnMouseWheel: true,
        moveOnMouseMove: true,
        moveOnMouseWheel: false,
      },
      {
        type: 'slider',
        height: 16,
        bottom: 6,
        borderColor: 'transparent',
        fillerColor: 'rgba(59,110,165,0.12)',
        handleStyle: { color: '#3b6ea5', borderColor: '#3b6ea5' },
        moveHandleStyle: { color: '#3b6ea5' },
        dataBackground: { lineStyle: { color: '#cdd3dc' }, areaStyle: { color: '#eef0f4' } },
        selectedDataBackground: { lineStyle: { color: '#3b6ea5' }, areaStyle: { color: 'rgba(59,110,165,0.12)' } },
        textStyle: { color: AXIS_TEXT, fontSize: 12 },
      },
    ],
    series: [
      {
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: values,
        lineStyle: { width: 2.2, color, shadowColor: rgba(color, 0.5), shadowBlur: 10 },
        itemStyle: { color },
        emphasis: { focus: 'series' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: rgba(color, 0.24) },
              { offset: 1, color: rgba(color, 0) },
            ],
          },
        },
      },
    ],
  }
  // 图表对读屏软件是黑盒，外面包一句总述：这是什么图、区间多少
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  return (
    <div role="img" aria-label={`${label}，共 ${values.length} 个数据点，区间 ${lo} 至 ${hi}`}>
      <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />
    </div>
  )
}
