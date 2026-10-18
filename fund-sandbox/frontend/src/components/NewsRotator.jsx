import { useEffect, useState } from 'react'
import { time } from '../lib/format'

// 资讯头条轮播：自动每 5s 切换一条，指示点可点跳，暂停/播放点按切换。
// 尊重减弱动效偏好（prefers-reduced-motion 时默认暂停）。
// 整块可点（鼠标顺手），同时「阅读全文」是真按钮，键盘 Tab + 回车也能打开，
// 不至于只有鼠标用户能用。
export default function NewsRotator({ items = [], onOpen, className = '', bare = false }) {
  const [idx, setIdx] = useState(0)
  const [paused, setPaused] = useState(
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  )
  const len = items.length

  useEffect(() => {
    if (paused || len <= 1) return
    const t = setInterval(() => setIdx((i) => (i + 1) % len), 5000)
    return () => clearInterval(t)
  }, [paused, len])

  // 条数变化（抓取到新资讯）时把越界的下标拉回来
  useEffect(() => {
    if (idx >= len) setIdx(0)
  }, [len, idx])

  if (!len) return null
  const it = items[idx]

  return (
    <div className={['news-rotator', bare && 'bare', className].filter(Boolean).join(' ')}>
      <div className="nr-head">
        <span className="kicker">头条轮播</span>
        <button
          className="btn btn-sm"
          onClick={(e) => {
            e.stopPropagation() // 父卡片整块可点时，别把「暂停」当成「打开弹窗」
            setPaused((p) => !p)
          }}
          aria-pressed={paused}
          aria-label={paused ? '继续轮播' : '暂停轮播'}
        >
          {paused ? '▶ 播放' : '⏸ 暂停'}
        </button>
      </div>
      <div
        className="nr-body"
        onClick={(e) => {
          e.stopPropagation() // 点正文是「打开原文」，不是「打开弹窗」
          if (it.link) onOpen?.(it.link)
        }}
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
      >
        <h3 className="nr-title">{it.title}</h3>
        {it.summary && <p className="nr-sum">{it.summary}</p>}
        <div className="meta">
          <span>{it.source}</span>
          <span>{time(it.pub_time)}</span>
          {it.link && (
            <button
              className="link-btn"
              onClick={(e) => {
                e.stopPropagation()
                onOpen?.(it.link)
              }}
            >
              阅读全文 ↗
            </button>
          )}
        </div>
      </div>
      {len > 1 && (
        <div className="nr-dots">
          {items.map((_, i) => (
            <button
              key={i}
              className={`nr-dot ${i === idx ? 'on' : ''}`}
              onClick={(e) => {
                e.stopPropagation() // 切轮播条，不触发父卡片点击
                setIdx(i)
              }}
              aria-label={`切到第 ${i + 1} 条`}
              aria-current={i === idx || undefined}
            />
          ))}
        </div>
      )}
    </div>
  )
}
