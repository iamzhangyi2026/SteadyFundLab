import { useEffect, useState } from 'react'
import Modal from './Modal'

// 统一的危险 / 重要操作二次确认弹窗。
// danger=true 时确认按钮用危险样式；confirmText 可自定义。
// 替代散落的 window.confirm 与无确认的危险操作，统一交互与视觉。
export default function ConfirmDialog({
  open, title, message, confirmText = '确认', cancelText = '取消', danger = false, onConfirm, onClose, loadingText = '处理中…',
}) {
  const [loading, setLoading] = useState(false)
  // 弹窗复用同一实例（open=false 时 return null 并不卸载，useState 持久）。
  // 若上次 onConfirm 成功直接 onClose 而未复位 loading，下次打开会残留 true 卡在"处理中…"。
  // 每次打开时强制复位，保证按钮可点。
  useEffect(() => {
    if (open) setLoading(false)
  }, [open])
  if (!open) return null
  const handleConfirm = async () => {
    if (!onConfirm) { onClose(); return }
    try {
      setLoading(true)
      await onConfirm()
      setLoading(false) // 成功路径也复位，避免残留
      onClose()
    } catch {
      // 回调内部已自行 toast 报错；保持弹窗打开，按钮恢复可点，便于重试
      setLoading(false)
    }
  }
  return (
    <Modal open={open} title={title} onClose={onClose} width={420}>
      <p style={{ margin: '0 0 4px', color: 'var(--text-soft, #4a5160)', fontSize: 14, lineHeight: 1.65, whiteSpace: 'pre-line' }}>
        {message}
      </p>
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 18 }}>
        <button className="btn" onClick={onClose} disabled={loading}>取消</button>
        <button
          className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`}
          disabled={loading}
          aria-busy={loading}
          onClick={handleConfirm}
        >
          {loading && <span className="btn-spin" style={{ marginRight: 8 }} />}
          {loading ? loadingText : confirmText}
        </button>
      </div>
    </Modal>
  )
}
