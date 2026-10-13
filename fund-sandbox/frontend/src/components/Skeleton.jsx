// 骨架屏（F2/5）：loading 态用微光占位替代转圈，避免空白等待感。
// 用法：<Skeleton variant="tile" /> / <Skeleton variant="line" /> / <Skeleton variant="block" />
export default function Skeleton({ variant = 'line', count = 1, className = '' }) {
  if (variant === 'tile') return <div className={`skeleton skeleton-tile ${className}`} />
  if (variant === 'block') return <div className={`skeleton skeleton-block ${className}`} />
  return (
    <div className={className}>
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="skeleton skeleton-line" />
      ))}
    </div>
  )
}
