// 统一线描图标（stroke=currentColor），适配浅色主题与触摸导航。
// 用作侧栏导航与操作按钮，避免引入图标库增加体积。
const base = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

export const Icon = {
  overview: (p) => (
    <svg {...base} {...p}>
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </svg>
  ),
  positions: (p) => (
    <svg {...base} {...p}>
      <path d="M21 3L3 10l7 3 3 7 8-17z" />
    </svg>
  ),
  trade: (p) => (
    <svg {...base} {...p}>
      <path d="M4 7h11l-3-3M20 17H9l3 3" />
    </svg>
  ),
  news: (p) => (
    <svg {...base} {...p}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M7 8h10M7 12h10M7 16h6" />
    </svg>
  ),
  analysis: (p) => (
    <svg {...base} {...p}>
      <path d="M4 19V5M4 19h16M8 16l3-4 3 2 4-6" />
    </svg>
  ),
  intel: (p) => (
    <svg {...base} {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8v4l3 2" />
    </svg>
  ),
  health: (p) => (
    <svg {...base} {...p}>
      <path d="M12 21s-7-4.5-7-10a4 4 0 017-2.5A4 4 0 0119 11c0 5.5-7 10-7 10z" />
    </svg>
  ),
  settings: (p) => (
    <svg {...base} {...p}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19 12a7 7 0 00-.1-1l2-1.5-2-3.5-2.3 1a7 7 0 00-1.7-1L14.5 3h-4l-.4 2.5a7 7 0 00-1.7 1l-2.3-1-2 3.5L3 11a7 7 0 000 2l-2 1.5 2 3.5 2.3-1a7 7 0 001.7 1l.4 2.5h4l.4-2.5a7 7 0 001.7-1l2.3 1 2-3.5-2-1.5a7 7 0 00.1-1z" />
    </svg>
  ),
  refresh: (p) => (
    <svg {...base} {...p}>
      <path d="M21 12a9 9 0 11-3-6.7M21 4v4h-4" />
    </svg>
  ),
  plus: (p) => (
    <svg {...base} {...p}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  play: (p) => (
    <svg {...base} {...p}>
      <path d="M7 5l12 7-12 7V5z" />
    </svg>
  ),
  external: (p) => (
    <svg {...base} {...p}>
      <path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1h5" />
    </svg>
  ),
  // ---- 状态 / 类型图标（16） ----
  pause: (p) => (
    <svg {...base} {...p}>
      <rect x="8" y="5" width="3.5" height="14" rx="1" />
      <rect x="12.5" y="5" width="3.5" height="14" rx="1" />
    </svg>
  ),
  check: (p) => (
    <svg {...base} {...p}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </svg>
  ),
  close: (p) => (
    <svg {...base} {...p}>
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  ),
  warn: (p) => (
    <svg {...base} {...p}>
      <path d="M12 3l9 16H3l9-16z" />
      <path d="M12 10v4M12 17v.5" />
    </svg>
  ),
  info: (p) => (
    <svg {...base} {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8v.5" />
    </svg>
  ),
  bell: (p) => (
    <svg {...base} {...p}>
      <path d="M6 9a6 6 0 0112 0c0 5 2 6 2 6H4s2-1 2-6z" />
      <path d="M10 19a2 2 0 004 0" />
    </svg>
  ),
  fund: (p) => (
    <svg {...base} {...p}>
      <path d="M4 7l8-3 8 3-8 3-8-3z" />
      <path d="M4 12l8 3 8-3M4 17l8 3 8-3" />
    </svg>
  ),
  up: (p) => (
    <svg {...base} {...p}>
      <path d="M12 19V6M6 12l6-6 6 6" />
    </svg>
  ),
  down: (p) => (
    <svg {...base} {...p}>
      <path d="M12 5v13M6 12l6 6 6-6" />
    </svg>
  ),
  download: (p) => (
    <svg {...base} {...p}>
      <path d="M12 3v12M7 11l5 5 5-5M5 21h14" />
    </svg>
  ),
  search: (p) => (
    <svg {...base} {...p}>
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-3.6-3.6" />
    </svg>
  ),
  // ---- 卡片标题装饰图标（16） ----
  pie: (p) => (
    <svg {...base} {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 12V3c4.97 0 9 4.03 9 9h-9z" />
    </svg>
  ),
  wallet: (p) => (
    <svg {...base} {...p}>
      <path d="M3 6a2 2 0 012-2h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2V6z" />
      <path d="M16 11h4v4h-4a2 2 0 110-4z" />
    </svg>
  ),
  chartLine: (p) => (
    <svg {...base} {...p}>
      <path d="M3 18l6-6 4 4 8-9" />
      <path d="M21 10V4h-6" />
    </svg>
  ),
  layers: (p) => (
    <svg {...base} {...p}>
      <path d="M12 2L2 7l10 5 10-5-10-5z" />
      <path d="M2 17l10 5 10-5" />
      <path d="M2 12l10 5 10-5" />
    </svg>
  ),
  rss: (p) => (
    <svg {...base} {...p}>
      <path d="M4 11a9 9 0 019 9" />
      <path d="M4 4a16 16 0 0116 16" />
      <circle cx="5" cy="19" r="1" />
    </svg>
  ),
  // ---- 卡片标题装饰图标（补充：分析 / 资讯 / 设置 / TOP 排行） ----
  // 板块（2x2 网格）
  board: (p) => (
    <svg {...base} {...p}>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </svg>
  ),
  // 热力图（3x3 网格）
  heatmap: (p) => (
    <svg {...base} {...p}>
      <rect x="3" y="3" width="5" height="5" rx="1" />
      <rect x="10" y="3" width="5" height="5" rx="1" />
      <rect x="17" y="3" width="4" height="5" rx="1" />
      <rect x="3" y="10" width="5" height="5" rx="1" />
      <rect x="10" y="10" width="5" height="5" rx="1" />
      <rect x="17" y="10" width="4" height="5" rx="1" />
      <rect x="3" y="17" width="5" height="4" rx="1" />
      <rect x="10" y="17" width="5" height="4" rx="1" />
      <rect x="17" y="17" width="4" height="4" rx="1" />
    </svg>
  ),
  // 行业暴露（基线 + 多根竖条）
  exposure: (p) => (
    <svg {...base} {...p}>
      <path d="M3 20h18" />
      <path d="M5 20v-6M10 20V8M15 20v-9M20 20v-4" />
    </svg>
  ),
  // 持仓穿透（向下箭头钻入层叠条）
  drill: (p) => (
    <svg {...base} {...p}>
      <path d="M12 2v6" />
      <path d="M9 8l3 3 3-3" />
      <rect x="3" y="13" width="18" height="3" rx="1" />
      <rect x="6" y="18" width="12" height="3" rx="1" />
    </svg>
  ),
  // 文档 / 人读叙述
  doc: (p) => (
    <svg {...base} {...p}>
      <path d="M7 3h7l4 4v14a1 1 0 01-1 1H7a1 1 0 01-1-1V4a1 1 0 011-1z" />
      <path d="M14 3v4h4" />
      <path d="M9 12h6M9 16h6" />
    </svg>
  ),
  // 财经资讯 / 信息流（项目符号列表）
  feed: (p) => (
    <svg {...base} {...p}>
      <rect x="4" y="5" width="3" height="3" rx="1" />
      <path d="M10 6.5h10" />
      <rect x="4" y="11" width="3" height="3" rx="1" />
      <path d="M10 12.5h10" />
      <rect x="4" y="17" width="3" height="3" rx="1" />
      <path d="M10 18.5h7" />
    </svg>
  ),
  // 资金规则（调节滑块）
  sliders: (p) => (
    <svg {...base} {...p}>
      <path d="M4 8h10M18 8h2" />
      <circle cx="16" cy="8" r="2" />
      <path d="M4 16h2M10 16h10" />
      <circle cx="8" cy="16" r="2" />
    </svg>
  ),
  // 交易池（靶心）
  target: (p) => (
    <svg {...base} {...p}>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="4" />
      <circle cx="12" cy="12" r="0.8" fill="currentColor" stroke="none" />
    </svg>
  ),
}
