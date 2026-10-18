// 空状态插画（F3/17）：列表为空时给一个轻量线描插画 + 文案，避免「一片空白」的冷清感。
// action：空态里的下一步操作（按钮）。没有出口的空态就是死路，用户只能干看着。
export default function EmptyState({ title = '暂无数据', hint, icon, action }) {
  return (
    <div className="empty-state">
      <svg width="72" height="72" viewBox="0 0 72 72" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="empty-art" aria-hidden>
        <rect x="14" y="20" width="44" height="34" rx="6" />
        <path d="M14 30h44" />
        <circle cx="21" cy="25" r="1.6" fill="currentColor" stroke="none" />
        <circle cx="27" cy="25" r="1.6" fill="currentColor" stroke="none" />
        <path d="M24 42h16M24 48h10" opacity="0.55" />
        {icon}
      </svg>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  )
}
