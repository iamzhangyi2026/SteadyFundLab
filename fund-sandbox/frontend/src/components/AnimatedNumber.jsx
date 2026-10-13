import { useEffect, useRef, useState } from 'react'

// 数字滚动（F1/11）：值变化时用 requestAnimationFrame 缓动（easeOutCubic）滚动到新值。
// 尊重 prefers-reduced-motion：直接显示终值，不做动画。
// 用法：<AnimatedNumber value={1234.5} format={(n) => money(n)} className="tone-up" />
export default function AnimatedNumber({ value, format = (n) => String(Math.round(n)), className = '', duration = 700 }) {
  const [display, setDisplay] = useState(value)
  const fromRef = useRef(value)
  const rafRef = useRef(0)

  useEffect(() => {
    const reduce = typeof window !== 'undefined' && window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduce) {
      setDisplay(value)
      fromRef.current = value
      return
    }
    const from = fromRef.current
    const to = value
    if (from === to || Number.isNaN(Number(to))) {
      setDisplay(to)
      return
    }
    const start = performance.now()
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - t, 3)
      setDisplay(from + (to - from) * eased)
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick)
      } else {
        fromRef.current = to
      }
    }
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [value, duration])

  const safe = Number.isFinite(display) ? display : value
  return <span className={className}>{format(safe)}</span>
}
