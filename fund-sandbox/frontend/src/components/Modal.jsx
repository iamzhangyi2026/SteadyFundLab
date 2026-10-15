import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

// 通用弹窗。承担三件事：语义（role/aria）、键盘（Esc 关闭 + Tab 焦点循环）、
// 打开期间锁定页面滚动，避免背景跟着滚。
export default function Modal({ open, title, onClose, children, width }) {
  const panelRef = useRef(null)
  const lastActiveRef = useRef(null)

  // Esc 关闭 + Tab 焦点陷阱（只在弹窗打开时挂载监听）
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose?.()
        return
      }
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const nodes = Array.from(panel.querySelectorAll(FOCUSABLE)).filter(
        (n) => n.offsetParent !== null || n === document.activeElement
      )
      if (!nodes.length) return
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [open, onClose])

  // 锁滚动 + 记住/归还焦点
  useEffect(() => {
    if (!open) return
    lastActiveRef.current = document.activeElement
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // 优先聚焦标注了 data-autofocus 的元素，否则聚焦面板本身（避免焦点落在「×」上）
    const panel = panelRef.current
    const target = panel?.querySelector('[data-autofocus]') || panel
    target?.focus?.()
    return () => {
      document.body.style.overflow = prevOverflow
      lastActiveRef.current?.focus?.()
    }
  }, [open])

  if (!open) return null

  // 渲染到 body：`.page` 带 transform 动画，会成为 position:fixed 的包含块，
  // 弹窗遮罩会被限制在页面区域内、随窗口变小而错位。挂到 body 下即始终相对视口。
  return createPortal(
    <div className="modal-overlay" onMouseDown={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
        ref={panelRef}
        style={width ? { maxWidth: width } : undefined}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="modal-head">
          <h3>{title}</h3>
          <button className="icon-btn" onClick={onClose} aria-label="关闭弹窗">
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body
  )
}
