import { useEffect, useState } from 'react'
import Modal from './Modal'

// 统一的「需要用户输入一段文本」弹窗。
//
// 当前调用方：暂无（人工添加基金不询问理由，后端按 `decision_source` 区分）。
// 保留为通用组件：与 ConfirmDialog 对称，凡需在界面收集一段文本时直接复用，
// **不要回退到 `window.prompt`**（见下方原因）。
//
// ⚠️ 为什么必须自建、不能用 `window.prompt`：
//   Electron **不支持** `prompt()` —— 调用会直接抛
//   `Error: prompt() is and will not be supported.`
//   （Electron 31 + `webPreferences: { sandbox: true }`，与主窗口配置一致）。
//   若调用处没有 try/catch，整个处理函数会**静默中断**、界面毫无反应。
//   本项目约定自建弹窗（ConfirmDialog 替代 window.confirm），
//   本组件与 ConfirmDialog 对称：同样复用 Modal、同样自带 loading 与失败重试。
export default function PromptDialog({
  open,
  title,
  label,
  placeholder = '',
  defaultValue = '',
  confirmText = '确认',
  cancelText = '取消',
  required = true,
  requiredText = '此项为必填',
  onConfirm,
  onClose,
  loadingText = '处理中…',
}) {
  const [value, setValue] = useState(defaultValue)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  // 弹窗实例复用（open=false 时 return null 但不卸载，useState 会残留）。
  // 每次打开复位，避免上次的输入 / 报错 / loading 残留。
  useEffect(() => {
    if (open) {
      setValue(defaultValue)
      setErr('')
      setLoading(false)
    }
  }, [open, defaultValue])

  if (!open) return null

  const handleConfirm = async () => {
    const v = value.trim()
    if (required && !v) {
      setErr(requiredText)
      return
    }
    if (!onConfirm) {
      onClose()
      return
    }
    try {
      setLoading(true)
      await onConfirm(v)
      setLoading(false)
      onClose()
    } catch {
      // 回调内部已自行 toast 报错；保持弹窗打开、按钮恢复可点，便于改完重试
      setLoading(false)
    }
  }

  return (
    <Modal open={open} title={title} onClose={onClose} width={460}>
      {label && (
        <div
          style={{
            marginBottom: 8,
            color: 'var(--text-soft, #4a5160)',
            fontSize: 14,
            lineHeight: 1.65,
          }}
        >
          {label}
        </div>
      )}
      <div className="field" style={{ margin: 0 }}>
        <input
          value={value}
          placeholder={placeholder}
          autoFocus
          disabled={loading}
          aria-label={title}
          onChange={(e) => {
            setValue(e.target.value)
            if (err) setErr('')
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleConfirm()
          }}
        />
      </div>
      {err && (
        <div className="err" style={{ marginTop: 6 }}>
          {err}
        </div>
      )}
      <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 18 }}>
        <button className="btn" onClick={onClose} disabled={loading}>
          {cancelText}
        </button>
        <button
          className="btn btn-primary"
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
